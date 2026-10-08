# Auto-dictation: start listening when a text field gets focus

*Design spec, 2026-10-07. Approved in conversation; implementation plan in
`docs/plans/2026-10-07-2010-auto-dictation-implementation.md`.*

## Goal

Dictate without touching a hotkey or button: click or tab into any text
field and Koli listens. It stays out of the way when you don't speak, and
gets out of the way the moment you type.

## Behaviour (agreed)

| Situation | What Koli does |
|-----------|----------------|
| A text field gets keyboard focus, feature on | Starts a recording silently. The floating bar shows "Listening". No start sound. |
| No speech for `silence_timeout_seconds` (default 5) since the start | Cancels quietly: no transcription, no sound. |
| Speech, then `silence_timeout_seconds` of silence | Stops and delivers like any recording (paste, complete sound, history). With phrase delivery on, anything not yet pasted is pasted; if everything was already pasted, the session just ends quietly. |
| You pause for `phrase_pause_seconds` (default 1) after speaking | Pastes what you said so far, with a trailing space, silently, and keeps listening. The 5 s limit then counts from that pause. |
| You type while it is listening | Cancels and discards the audio, quietly. Phrases already pasted stay. |
| Focus leaves the field (another field, a button, another window) | Cancels and discards, quietly: once focus has moved, a paste would land wherever you clicked. Another text field then starts its own recording. |
| Focus stays in the same field after a stop or cancel | Nothing. It arms again only when focus moves to a field (the same one counts after focus has been elsewhere). |
| Password field | Never starts. |
| Koli's own window | Never starts. |
| Per-app rule says `auto_dictation: false` | Never starts in that app. Rules can also say `true` to override nothing (reserved for a future "only these apps" mode; today the default is already on everywhere). |
| Hotkeys paused, model loading, transcribing, or a recording already running | Never starts. |
| Hotkey pressed during an auto recording | Normal hotkey behaviour: stop and deliver, or cancel. |
| Feature off (default) | Nothing is hooked. No background thread, no COM objects. |

Default: **off**. Turning it on applies live, no restart.

## Architecture

```
platform/windows/input_watch.py          auto_dictation.py            state_manager.py
┌───────────────────────────────┐   on_focus(info)   ┌──────────────┐  begin_auto_recording()  ┌───────────┐
│ hook thread: message pump     │ ─────────────────► │ Coordinator  │ ───────────────────────► │ pipeline  │
│   • WinEvent EVENT_OBJECT_FOCUS│                    │ (pure Python)│  cancel_auto_recording() │           │
│   • WH_KEYBOARD_LL            │ ─────────────────► │              │ ───────────────────────► │           │
│ probe thread: UI Automation   │   on_typing()      └──────────────┘                          └───────────┘
└───────────────────────────────┘
```

### `platform/windows/input_watch.py` (mirror: `platform/macos/input_watch.py`, no-op)

API, mirrored on both platforms:

```python
def start(on_focus: Callable[[dict], None], on_typing: Callable[[], None]) -> bool
def stop() -> None
def is_running() -> bool
```

- **Hook thread.** Owns a Windows message loop. Installs a `SetWinEventHook`
  for `EVENT_OBJECT_FOCUS` (out of context) and a `WH_KEYBOARD_LL` hook.
  The focus callback only enqueues the hwnd and a timestamp (UI Automation
  must not be called from inside a WinEvent callback). `stop()` posts
  `WM_QUIT`, unhooks, and joins the thread.
- **Probe thread.** Initialises COM, drains the queue, debounces (last event
  wins, 120 ms), then asks UI Automation for the focused element and calls
  `on_focus(info)` with:

  ```python
  {'id': tuple,        # UIA runtime id, falls back to (hwnd,)
   'editable': bool,   # Edit / Document / editable ComboBox, enabled, not read-only
   'password': bool,
   'hwnd': int,        # the element's native window, else the focus hwnd
   'root_pid': int,    # process of the top-level window (WebView2 content
                       #  runs in a child process; the root tells us it is Koli)
   'exe': str}         # root window's exe, lower-case basename
  ```

  Editable means: control type `Edit` (50004) or `Document` (50030), or
  `ComboBox` (50003) with the Value pattern not read-only; `IsEnabled`; and
  `ValueIsReadOnly` not true. Any probe failure reports `editable: False`
  and logs at debug level with the reason, so the Live console shows why a
  field did not trigger.
- **Typing filter**, in the keyboard hook. A key counts as typing unless:
  it is injected (`LLKHF_INJECTED`, which covers Koli's own paste, Enter and
  live typing); it is a modifier (Shift, Ctrl, Alt, Win, left or right); Ctrl,
  Alt or Win is held (shortcuts and hotkeys); or it is a navigation or
  function key (arrows, Home, End, PageUp/Down, Insert, F1–F24, Caps/Num/
  Scroll Lock, PrintScreen, Pause). Everything else, including Backspace,
  Delete, Enter, Tab and Space, is typing. `on_typing()` is called at most
  once per 250 ms.
- Nothing in this module knows about Koli state. Dependency: `comtypes`
  (already pulled in by `pycaw`; now listed explicitly for Windows).

### `auto_dictation.py` — the coordinator

Pure Python, no OS calls, constructed with the state manager, the config
manager, the app rules, and a watcher module (injected so tests use a fake).

```python
class AutoDictation:
    def __init__(self, state_manager, config_manager, app_rules, watcher=None, own_pid=None, clock=time.monotonic)
    def start() -> bool          # hooks if enabled; idempotent
    def stop() -> None           # unhooks; idempotent
    def apply_settings() -> None # re-reads auto_dictation.*; starts or stops
    # callbacks from the watcher
    def on_focus(info: dict) -> None
    def on_typing() -> None
    # hooks from the state manager
    def on_recording_ended() -> None
```

Policy in `on_focus`, in order, each one logged at debug level when it says no:

1. Same field as the last focus report: ignore (re-arm rule). Same means
   the same runtime id, or the same native window with one bounding
   rectangle inside the other. Chromium apps (Cursor, VS Code, chat boxes)
   re-create a box's DOM node on re-render and hand out a new runtime id for
   the same field, and focus can land on an inner or outer node of one box;
   the rectangle is what stays put. (`rect` is part of the focus report.)
2. Remember the new id as "last focused" (even if it is not editable, so
   returning to a field after a button click counts as a fresh focus).
3. `editable` false, or `password` true: no.
4. `root_pid` is Koli's own: no.
5. `exe` in the state manager's non-text exe list: no.
6. Per-app rule for the foreground matches and has `auto_dictation: false`: no.
7. State manager `can_start_recording()` false, or paused: no.
8. Arm a 300 ms start timer (a later focus report cancels it, so tabbing
   through a form does not start a recording per field). When it fires,
   re-check 7 and call `state_manager.begin_auto_recording()`.

Before step 3, if an auto-started recording is in progress, any focus
change cancels it (`cancel_auto_recording('the text field lost focus')`);
the new element is then judged on its own.

`on_typing`: if the state manager reports an auto-started recording in
progress, `state_manager.cancel_auto_recording()`. Otherwise nothing: typing
during a hotkey recording is the user's business.

### State manager changes

- `begin_auto_recording()`: like `_begin_recording` but with
  `auto_started=True`: skips the start sound, passes
  `silence_timeout=auto_dictation.silence_timeout_seconds` to the recorder,
  publishes `mode: 'auto'`. Logs a warning once per session if the recorder
  has no continuous VAD (then only typing or a hotkey can end the recording).
- `cancel_auto_recording(reason)`: `cancel_active_recording(quiet=True)`, only
  when the active recording is auto-started.
- `handle_vad_event(SILENCE_TIMEOUT)`: if auto-started and the detector
  reports `had_speech` false, cancel quietly instead of transcribing.
- `_auto_started` is cleared whenever a recording ends (stop, cancel, max
  duration), and `_publish_state` reports `mode: 'auto'` while it is set.

### Recorder and VAD

- `AudioRecorder.start_recording(silence_timeout: float | None = None)`:
  applies the override to the continuous detector for this recording and
  restores the configured value otherwise.
- `ContinuousVoiceDetector.set_silence_timeout(seconds)` recomputes the frame
  budget; `had_speech` becomes true on the first speech frame and resets with
  `reset()`.

### Settings

`config.defaults.yaml`:

```yaml
auto_dictation:
    # Start listening when a text field gets keyboard focus, in any app ...
    enabled: false
    silence_timeout_seconds: 5
```

Schema: both in the General section. `enabled` is a live toggle with the
help text naming the typing and silence rules and "Windows only for now".
`silence_timeout_seconds` is a number, 1–60, applied at the next recording.
Applier: `auto_dictation.` prefix calls `AutoDictation.apply_settings()`.

Per-app rules gain `auto_dictation: true | false` (documented in the defaults
header, added to `RULE_KEYS`, and a tri-state on the Apps screen's rule
editor).

### UI

Floating bar: while `state.mode === 'auto'`, the recording bubble shows a
"Listening" badge instead of the lock or command badge. Live screen label:
"Listening (focused field)". Nothing else changes.

### Wiring and shutdown

`main.py` builds the coordinator after the hotkey listener and hands it to
the state manager (`set_auto_dictation`), starts it if enabled, and stops it
in `shutdown_app`. The app-window pause (`set_paused`) does not unhook; the
coordinator simply refuses to start while paused.

## Found while testing (2026-10-08)

Two faults outside this feature only showed up because a focus-started
recording ends on its own, on the VAD's thread, where a hotkey recording
ends on the listener thread:

- **Microphone fallback kept the old VAD block size.** When the configured
  mic fails to open and recording moves to the default input, the block
  size now follows the new rate (`AudioRecorder._configure_for_device_rate`).
- **ctranslate2 aborts the process when a thread that used CUDA exits.** Its
  thread-exit callback calls `set_device_index`, which threw on a two-GPU
  machine (`device_index: 1`); a C++ exception there has nowhere to go. All
  engine calls now run on one persistent thread (`whisper_engine.ENGINE_THREAD`).
  Crash analysis: WinDbg's `cdb` on `%LOCALAPPDATA%\CrashDumps`, and
  `crashes/faults.log` written by `faulthandler` from now on.

## Out of scope

- macOS implementation (no-op stub; the Accessibility focus notification and
  the existing event tap are the planned signals).
- An "only in these apps" mode. The per-app `auto_dictation: true` key is
  accepted now so rules written for it keep working later.
- Restarting after a paste for paragraph-after-paragraph dictation; the
  existing Continuous dictation setting covers that with a hotkey.

## Testing

Unit tests (fake watcher, fake state manager, fake clock):

- focus on editable field → start after the debounce; a second focus report
  within the debounce cancels the first.
- same element again → no start; different element then back → start.
- password, non-editable, own pid, excluded exe, rule `auto_dictation: false`,
  paused, busy → no start, each.
- typing during an auto recording → cancel; during a hotkey recording → nothing.
- focus moves to a button or another window → cancel; to another field →
  cancel, then start again there.
- silence timeout with no speech → quiet cancel; with speech → transcription.
- `set_silence_timeout` changes the frame budget; `had_speech` tracks speech.
- Windows typing filter: injected, modifiers, Ctrl-held, F-keys, arrows are
  not typing; letters, Backspace, Enter, Space are.
- Schema coverage test stays green; rule key accepted by the rules service.

Real-app test (the user, before commit): Chrome address bar and a Gmail
compose, a Slack or Discord message box, Notepad, a password field in a
browser, the Koli settings window, typing mid-listen, 5 s of silence, and a
hotkey press during an auto recording.
