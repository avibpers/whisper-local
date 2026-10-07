# New Desktop UI: Floating Bar + App Window

As a *Whisper Local user* I want **a real desktop app: a small floating bar while I work, and a full window when I open it, with simple and advanced modes** so I can see what the app hears and does, and change any setting instantly without editing YAML or restarting.

Mockup (source of truth for layout and copy): https://claude.ai/artifact/L6ujxPkwgG1MSAEu7wZ2Rt
Boards: Floating bar states · Live (light/dark) · Simple Home · Simple Settings · Advanced Settings · History & insights · Vocabulary · Commands & AI · Apps & profiles · Diagnostics.

## Background

### The problem

- The UI today is scattered: a pystray menu, a Tk level pill, five separate Tk windows (settings, history, cheat sheet, add-word, fallback), a console banner, interactive console prompts for GPU setup, and 29 CLI flags. A new user has to discover all of it.
- The Tk settings window covers about 45 of the 109 settings. The rest need hand-edited YAML.
- Many changes need "restart the app" (model, device, audio driver, hotkeys in practice). The user is told to restart instead of the app doing it.
- There is no way to watch what the engine is doing: no live waveform, no voice-detection view, no pipeline trace, no in-app log.
- Most settings use engine words (model, beam, VAD, compute type). A non-technical user cannot pick sensible values.

### Goals

1. Two states. **Closed** = a floating bar, always on top, click-through outside its own buttons. **Open** = an app window with sidebar navigation.
2. **Simple mode** (default for new users): plain-language questions that set many technical settings at once. **Advanced mode**: every setting, the live log and tools.
3. **Instant apply**: no Save button anywhere. Settings that need a component reload do it in the background while dictation keeps working.
4. **Live view**: waveform, input level, voice-detection probability, silence countdown, foreground app and matched rule, last-dictation pipeline trace, streaming console log.
5. **Light and dark** themes, light by default.
6. **Every existing option** reachable from the UI. Enforced by a test, not by memory.
7. **Development runs from the CLI.** A compiled installer with a GPU choice comes later (Phase 13) and reuses the same UI.

### Current architecture

```
main.py (main thread):  app.run_event_loop(shutdown_event)   # wait loop on Windows
├─ HotkeyListener (global-hotkeys thread) ──► StateManager
├─ AudioRecorder (PortAudio callback thread): level, VAD, streaming, pre-roll
├─ StateManager: pipeline on worker threads, owns LevelOverlay + FallbackWindow
│    └─ _update_ui_state(state) ─► SystemTray.update_state + TerminalTitle
├─ SystemTray (pystray, run_detached): all menus
├─ LevelOverlay (own Tk root on a daemon thread)
└─ Separate processes: --settings, --history, --doctor, --stats, --selftest (Tk or console)
Config: ConfigManager.update_user_setting(section, key) → writes user_settings.yaml
        Only `postprocess` hot-reloads; model/device changes are partly live via the tray.
```

### Target architecture

```
main.py (main thread):  ui.host.run(shutdown_event)            # pywebview GUI loop
├─ UiHost (pywebview, in-process)
│    ├─ Main window   web/index.html   (hidden = "closed" state, never destroyed)
│    ├─ Floating bar  web/bar.html     (frameless, transparent, on-top, no-activate)
│    └─ Bridge (js_api) ◄── calls ── JS      EventBus ── pushes ──► JS (batched ~20 Hz)
├─ EventBus: state, level, vad, pipeline, delivered, notify, log, task, settings, foreground
├─ SettingsService: schema (all keys) → validate → write → apply (live | background | next launch) → undo
├─ TaskManager: background jobs with progress (model download/switch, GPU install, mic restart…)
├─ Domain services: history, insights, vocabulary, commands, transforms, rules, profiles,
│                   models, diagnostics, system   (thin wrappers over existing modules)
├─ Existing engine, unchanged in behaviour: HotkeyListener, AudioRecorder, StateManager,
│   WhisperEngine, ClipboardManager, text_postprocess, voice_commands, app_rules…
├─ FloatingBar adapter (same interface as LevelOverlay, so StateManager barely changes)
└─ SystemTray: slim menu (Open, Pause, Profile, Quit), still useful as a fallback
```

## Decisions that need your OK before Phase 1

Your global instructions say to ask before breaking the file structure. These are the structural choices:

| # | Decision | Recommendation | Why | Alternatives considered |
|---|---|---|---|---|
| D1 | UI technology | **pywebview** (WebView2 on Windows, WKWebView on macOS), in the same Python process | The mockup is HTML/CSS and ports almost 1:1. Small dependency. Direct access to StateManager, no IPC. | PySide6/Qt (rewrite the mockup, 150 MB+), Tauri/Electron with a Python sidecar (two runtimes, IPC), keep Tk (cannot reach the design) |
| D2 | Frontend stack | **Preact + htm, vendored, no build step** | Components and reactive state for ~10 screens, but nothing to compile. `pip install -e .` and run. Works offline. | Vanilla JS (too much DOM plumbing), React/Svelte + Vite (needs Node in dev and CI) |
| D3 | New folders | `src/whisper_key/ui/` (host, bridge, bar, events, tasks, web assets), `src/whisper_key/settings/` (schema, service, appliers, simple mode), `src/whisper_key/services/` (screen data) | Keeps the 70 existing modules untouched in place. New code is grouped by role. | Flat files next to existing modules (would add ~25 files to an already flat package) |
| D4 | Fonts | Bundle **Geist + Geist Mono** woff2 with their OFL licence | The app must work offline. The mockup loads them from Google Fonts. | System fonts (Segoe UI) |
| D5 | Single instance | Launching again **opens the existing window** via local IPC. Takeover stays behind `--replace`. | With a window, "launch again" means "show me the app". Today it kills and replaces the running instance. | Keep takeover |
| D6 | Legacy Tk UI | Keep behind `--legacy-ui` for one release, then delete | Safety net while the new UI matures | Delete immediately |
| D7 | Branch base and Hebrew | **Decided: branch from master, independent of `feat/hebrew-support`.** The UI supports Hebrew (RTL text, Hebrew in language lists, Hebrew models and phrases when they exist in config) but does not depend on the Hebrew branch. Better language detection is a later phase. | The schema and screens read whatever keys and models the config has, so the Hebrew branch merges in either order. | Land the Hebrew branch first (rejected) |

## How to run during development (CLI only)

```powershell
cd S:\whisper-local-new-ui
py -3.13 -m venv .venv
.venv\Scripts\pip install -e .

.venv\Scripts\whisper-local                    # new UI: floating bar + tray; window opens on first run
.venv\Scripts\whisper-local --open             # also open the main window at start
.venv\Scripts\whisper-local --open --page settings/audio   # open straight on a screen/section
.venv\Scripts\whisper-local --ui-dev           # DevTools, reload web files on change, verbose UI log
.venv\Scripts\whisper-local --test --open      # separate test instance (own mutex), safe next to your daily one
.venv\Scripts\whisper-local --legacy-ui        # old Tk windows (until Phase 11)
.venv\Scripts\whisper-local --replace          # old behaviour: kill the running instance and take over
```

Frontend-only work without Python: open `src/whisper_key/ui/web/index.html?mock=1` in Chrome or Edge. `js/mock-api.js` serves fixture data and fakes live events (waveform, logs, task progress). This is how screens get built and reviewed quickly.

## Implementation Plan

Milestones: **M1** after Phase 4 (you can run it and see the Live screen and the bar), **M2** after Phase 6 (every setting editable, instant apply), **M3** after Phase 8 (every screen), **M4** after Phase 10 (Simple mode + welcome), **M5** after Phase 12 (release-ready from CLI). Phases 13 and 14 come later.

0. Preparation and spikes — **done 2026-10-07**, results below
- [x] Work from master (109 keys in `config.defaults.yaml`); the Hebrew branch can merge before or after without blocking anything here
- [x] Add `pywebview>=6.2` to dependencies for Windows and macOS (pulls `pythonnet` on Windows); add `ui/web/**` to package-data
- [x] **Spike A, process model:** pass
- [x] **Spike B, floating bar window:** pass with changes (no transparency; native rounded corners; focus restore after load; Win32 positioning)
- [x] **Spike C, push throughput:** pass on CPU; memory over budget, budget revised
- [x] **Spike D, custom title bar:** fail → native title bar
- [x] **Spike E, WebView2 detection:** pass
- [x] **Decision gate:** bar stays in WebView2 (no Tk fallback needed)
- [ ] Spike B follow-ups to verify in Phase 4 on real hardware: 125/150/200% DPI, second monitor, Windows 10 square corners

#### Phase 0 results (Windows 11, pywebview 6.2.1, pythonnet 3.2.0, WebView2 154, Python 3.13)

| Spike | Result | Consequence for the build |
|---|---|---|
| A · one process | pywebview owns the main thread; pystray, global-hotkeys and a PortAudio stream keep running (audio frames never stopped); hide/show works; JS round-trip works; teardown 10 ms | In-process design confirmed. First-ever launch took ~8 s before the window was ready (WebView2 profile creation); later launches ~2 s. Show a "starting" state in the bar, never block dictation on it |
| B · bar shape | `transparent=True` is **not honoured on Windows**; a WinForms transparency key makes the whole WebView2 window invisible; a window region does not clip WebView2 content | The bar is a **rounded rectangle using Windows 11 native corners** (~8 px, antialiased, thin border), not a fully round pill. The page fills the window with the bar colour. Windows 10 gets square corners. Mockup to be adjusted to match |
| B · size | Default `min_size` (200×100) clamps the bar height | Create the bar with `min_size=(40, 20)` |
| B · focus | WebView2 takes focus **twice**: at window creation and when the page finishes loading. `window.resize/move` from pywebview also activate it. After a one-time restore, Win32 `SetWindowPos(SWP_NOACTIVATE)` resizes/moves and JS updates never took focus again | Bar is created once at startup and never reloaded; after its `loaded` event the previous foreground window is restored; all bar positioning goes through `SetWindowPos(SWP_NOACTIVATE)`; extended style `WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE`, without `WS_EX_APPWINDOW` (no taskbar button) |
| B · click-through | Not needed | The window is exactly the bar's size; it grows only while showing a state or the quick-actions popover |
| C · push | 240/240 batches at 24 Hz (level + VAD + 3 log lines each); CPU 0.04% idle, 0.11% while pushing (share of the whole machine) | Push design confirmed; 20–25 Hz is fine |
| C · memory | Python 20 MB → **215 MB with the bar alone** (WebView2 starts 6 helper processes) → 262 MB with the main window → 238 MB after closing the main window | WebView2 has a fixed ~200 MB cost as soon as any webview exists; the main window adds only ~45 MB. Keep the main window hidden (instant reopen) rather than destroying it. New budget: closed state ≤ +250 MB over today, open ≤ +300 MB |
| D · frameless window | With `frameless=True` every point answers `HTCLIENT`: no edge resizing (WebView2 covers the window) | **Native Windows title bar** for the main window (Windows 11 snap layouts and resizing for free; dark/light caption via `DWMWA_USE_IMMERSIVE_DARK_MODE`). The mockup's title-bar row (search, task chip, status, theme) becomes the top toolbar inside the window |
| E · WebView2 | Runtime 154 found in the registry; pywebview picked `edgechromium` | Startup check reads the same registry keys; if missing, message + `--legacy-ui` fallback |

1. Live telemetry backbone (backend only, no visible change)
- [ ] `ui/events.py`: thread-safe `EventBus` with `publish(topic, payload)` / `subscribe(topic, fn)`; per-topic throttling (latest-wins for `level`/`vad`, batching for `log`)
- [ ] `ui/log_stream.py`: `logging.Handler` with a 2,000-record ring buffer; publishes `log` events; `tail(n)` for late subscribers. Respects `logging.log_transcriptions` exactly like the file handler
- [ ] `StateManager._update_ui_state` also publishes `state` (idle / recording / processing / model_loading / paused / command / rephrase)
- [ ] `AudioRecorder`: publish `level` while recording or mic-testing (RMS already computed in `_audio_callback`); add `start_level_preview()`/`stop_level_preview()` for "Test my microphone" without recording
- [ ] `ContinuousVoiceDetector`: keep `last_probability`; publish `vad` {probability, speaking, silence_s, onset, offset, timeout_s}
- [ ] `WhisperEngine` + `WhisperEngineCpp`: keep `last_info` {language, language_probability, seconds, model, device, beam} after each transcription
- [ ] `text_postprocess.postprocess(text, cfg, trace=None)`: when a list is passed, append each step that changed the text (`inline_formatting`, `corrections`, `style:casual`…). No behaviour change when `trace` is None
- [ ] `StateManager._transcription_pipeline`: publish `pipeline` events per stage (captured → whisper → postprocess → delivered/suppressed/copy-only/fallback/failed) with raw and final text, app, rule id, method, auto-send. Text is only included when the UI is the consumer (never written to logs unless `log_transcriptions`)
- [ ] Route every `system_tray.notify(...)` call through `notify(kind, message, actions)` on the bus; the tray and the bar both subscribe
- [ ] Foreground watcher: publish `foreground` {exe, title, matched_rule} when it changes (polled 2 Hz only while the main window is visible)
- [ ] `ui/tasks.py`: `TaskManager.run(label, fn, cancellable)` → publishes `task` {id, label, progress, status, error}; tasks report progress through a callback
- [ ] Unit tests for bus throttling, log ring buffer, trace capture, pipeline event order with fake components

2. UI host and bridge
- [ ] `ui/host.py`: create both windows, run the pywebview loop on the main thread, watch `shutdown_event`, hide-instead-of-close for the main window, persist window size/position/maximized to `ui.window`
- [ ] `ui/bridge.py`: one `Bridge` class exposed as `js_api`; every method returns `{ok, data | error}` and never raises into JS; long work goes through `TaskManager`
- [ ] Event push: subscribe to the bus, batch per animation frame (~16–50 ms), call `window.evaluate_js("wl.receive(batch)")` only for windows that are visible (bar: always; main: when shown)
- [ ] `main.py`: `--open`, `--page`, `--ui-dev`, `--legacy-ui`, `--replace`; build the app as today, then hand the main thread to `UiHost` instead of `app.run_event_loop`
- [ ] `ui/instance_ipc.py` + `instance_manager.py`: second launch sends "open [page]" to the running instance over a local named pipe with an auth key, then exits. `--replace` keeps today's takeover
- [ ] Tray: add "Open Whisper Local" (default item, double-click) and route it to `UiHost.show(page)`
- [ ] Native dialogs through the bridge (`create_file_dialog` for files/folders); external links open in the default browser via an allow-list; no navigation away from local pages
- [ ] Security: load only packaged local files; strict Content-Security-Policy in both HTML files; no remote scripts, fonts or images; bridge validates every path and value
- [ ] Crash isolation: a JS error is logged (forwarded to `log`) and never stops dictation; if the webview dies, dictation, hotkeys and tray keep running and the tray offers "Reopen window"
- [ ] Tests: bridge contract tests with fake services (every method returns the envelope, bad input → error not exception)

3. Frontend foundation
- [ ] `web/css/tokens.css`: light and dark tokens copied from the mockup (`--bg --panel --sunk --line --line2 --ink --ink2 --mute --acc --rec --ok --warn` + soft variants); `[data-theme]` on `<html>`; `system` follows `prefers-color-scheme`
- [ ] `web/css/components.css`: one definition each for button, chip, card, toggle, segmented, select, input, range, kbd, tag, table, row, toast, modal, popover. Check for duplicates before adding any class
- [ ] Use logical CSS properties (`margin-inline`, `text-align: start`) everywhere so a Hebrew UI later is cheap; user text gets `dir="auto"`
- [ ] Bundle fonts: `web/fonts/Geist-*.woff2`, `GeistMono-*.woff2`, `OFL.txt`
- [ ] Vendor `preact`, `preact/hooks`, `@preact/signals`, `htm` into `web/vendor/` with versions and licences noted
- [ ] `js/api.js`: promise wrapper over `window.pywebview.api`; `wl.receive(batch)` dispatcher; switches to `js/mock-api.js` with `?mock=1`
- [ ] `js/store.js`: signals for `state`, `settings`, `schema`, `tasks`, `logs`, `pipeline`, `ui.mode`, `ui.theme`; settings writes are optimistic with rollback on error
- [ ] `js/router.js`: hash routes `#/live`, `#/history`, `#/vocabulary`, `#/commands`, `#/apps`, `#/settings/<section>`, `#/diagnostics`, `#/home` (simple), `#/simple-settings`
- [ ] Shell: native Windows title bar (themed light/dark), a top toolbar inside the window (search, task chip, status chip, theme toggle), sidebar with mode switch, content area. Close (the native X) = hide to bar
- [ ] Global search (`Ctrl+K`): searches setting labels/help from the schema, screens, commands, history; Enter jumps to the row and highlights it
- [ ] Toasts ("Saved · Beam size 5 → 6 · Undo") and in-page modals for every confirmation. **No `alert()`, `confirm()` or `prompt()`** anywhere
- [ ] Keyboard: every control reachable by Tab, visible focus ring, Esc closes popovers/modals, shortcuts listed in Diagnostics
- [ ] `js/mock-api.js` + `web/fixtures/*.json`: realistic data for every screen and a fake event generator

4. Walking skeleton — **Milestone 1: you can run it and test**
- [ ] Live screen (Advanced) wired to real data: recording timer and max, waveform from `level`, input level meter, VAD bar with onset/offset markers, silence countdown, device chips (name, host API, sample rates, pre-roll, noise suppression state)
- [ ] Live → Context card: microphone picker (live switch), app + window title, matched rule, language, model + device + precision, delivery method, effective prompt
- [ ] Live → quick controls: language segmented, auto-paste, continuous, translate; Cancel / Stop & paste buttons
- [ ] Live → Last dictation: four-step trace, heard vs typed text (`dir="auto"`), Copy, Paste again, Fix a word everywhere
- [ ] Live → Today: dictations, words, time saved, average latency
- [ ] Live → Console: streaming log, level filter (All/Info/Warnings/Debug), text filter, Pause, Clear, Open app.log; auto-scroll that stops when you scroll up
- [ ] Floating bar basic states: resting, hover hint, recording (waveform, timer, Esc, Stop), transcribing, delivered, failed
- [ ] `ui/floating_bar.py`: implements the `LevelOverlay` interface (`start, show_recording, show_processing, hide, flash_success, flash_failure(msg), set_streaming_text, set_position, shutdown`) so `StateManager.attach_components` just constructs a different class
- [ ] Theme toggle persists to `ui.theme`
- [ ] **Your manual test:** dictate in Notepad and Slack with the window open and closed; watch Live update; switch mic from Live

5. Settings engine (instant apply)
- [ ] `settings/schema.py`: one `Setting` entry per key: `path, type, default, choices/min/max/unit, label, help, section, mode (simple|advanced|hidden), apply (live|background|next_launch|internal), validator, applier`. Labels and help text come from the mockup
- [ ] **Coverage test:** walk `config.defaults.yaml` and fail if any leaf key (109 on master, 111 once the Hebrew branch lands, plus new `ui.*` and `server.*`) has no schema entry, or a schema entry points at a missing key. This is what guarantees no option is skipped
- [ ] `ConfigManager`: add `get_path(path)` / `set_path(path, value)` for nested keys (`audio.noise_suppression.enabled`, `logging.file.enabled`, `postprocess.smart_formatting.times`…); guard `config` with a lock; keep writing overrides-only YAML
- [ ] Write debouncing: coalesce rapid changes (slider drags, typing) and write `user_settings.yaml` at most every 300 ms; flush on exit
- [ ] `settings/service.py`: `set(path, value, source)` → coerce → validate → write → apply → publish `settings` → return `{applied, task_id, warnings}`; `undo()` (stack of 50); `reset_section(section)`; `reset_all(keep_hotwords=True)`
- [ ] `settings/appliers.py` live appliers (no restart, no reload):
  - [ ] engine attributes: `language`, `task`, `beam_size`, `initial_prompt`, `hotwords` (joined), `log_transcriptions`
  - [ ] hotkeys: `HotkeyListener.change_hotkey_config` for all hotkey keys; `double_tap_*` rebuilds the tap latch; conflict check reuses `_resolve_hotkey_conflicts`; warn on Alt+Shift / Ctrl+Shift chords
  - [ ] clipboard: new `ClipboardManager.apply_config(dict)` for all ten clipboard keys
  - [ ] audio feedback: rebuild `AudioFeedback` (cheap) for all eight keys
  - [ ] recorder: `max_duration`, `pause_media_on_record`, `continuous_mode`, noise suppression and whisper mode configs (read per recording)
  - [ ] VAD: thresholds, min speech, precheck, realtime, timeout apply from the next recording
  - [ ] logging: re-run handler setup for `logging.*` (level, file on/off, filename, console on/off and level)
  - [ ] overlay: `overlay.enabled/position/click_through` → bar show/hide/move
  - [ ] postprocess: write in memory too (it already hot-reloads per delivery)
  - [ ] `voice_commands.enabled` (also registers/unregisters the command hotkey), `audit.enabled`, `update_check.enabled`, `terminal_title.*`, `system_tray.tooltip`, `streaming.deliver_to_cursor`, `whisper.prompt_from_selection`, `whisper.models` / `streaming.models` (registry rebuild)
- [ ] Background appliers through `TaskManager` (the old component keeps working until the new one is ready, then an atomic swap under `StateManager._state_lock`; if busy recording/processing, queue until idle, reusing the `_pending_model_change` pattern):
  - [ ] `whisper.model` (download with progress if not cached, then load and swap)
  - [ ] `whisper.device`, `whisper.compute_type`, `whisper.backend` (build a new engine object off-thread, warm it up, swap). If CUDA libraries are missing, fail the task with an "Install GPU support" action instead of hanging
  - [ ] `audio.input_device`, `audio.host`, `audio.channels`, `audio.dtype` (restart the capture stream; reuse `request_audio_device_change` / `set_audio_host`)
  - [ ] `streaming.streaming_enabled`, `streaming.streaming_model` (download + init the streaming manager, attach to the recorder)
  - [ ] `system_tray.enabled` (start/stop the tray)
  - [ ] `server.enabled/host/port` (start/stop the local API server thread)
- [ ] Next launch only: `console.start_hidden` (shown as "Applies next time you open the app"; this is not a restart prompt)
- [ ] Internal, not edited directly: `onboarding.gpu`, `onboarding.gpu_class` (driven by the GPU setup action)
- [ ] New config sections in `config.defaults.yaml`: `ui:` {mode, theme, open_on_start, window, welcome_done} and `server:` {enabled, host, port}
- [ ] Profiles and app rules: activating a profile goes through `SettingsService` so each change is applied the same way (fixes today's partial `activate_profile`)
- [ ] Tests: applier dispatch per key with fakes; background swap keeps the old engine until ready; queued change while processing; undo restores value and re-applies; debounce writes once

6. Advanced Settings screen (11 sections, schema-driven)
- [ ] Generic row renderer from the schema: bool → toggle, enum → segmented (≤5) or select, number → input with unit or range, list[str] → tag input, hotkey → key-capture field, text → input/textarea, path → file picker. Shows "Applies in background" badges and live task progress inline
- [ ] General: language (full list incl. Hebrew), output transcribe/translate, recording mode, auto-paste, continuous dictation, start on login
- [ ] Microphone & audio: microphone + Test + level meter, audio driver, channels, sample format, longest recording, pause music, noise suppression (+ install the optional `noise` extra as a task) + strength, whisper mode + max boost; voice detection: skip silent, start/end thresholds, shortest speech, stop on silence + seconds
- [ ] Model & engine: models table grouped (multilingual, Hebrew, English-only, yours) with size, on-disk/download/progress, In use, show-in-menus toggle (`enabled`), delete from disk; add from Hugging Face / local folder; engine; device with detected GPU; precision; beam size; context prompt; use selection as context; favored words link; live preview, type as you speak, preview model
- [ ] Hotkeys: all eight actions with key capture and conflict/layout-switch warnings; double-tap + window ms; link to transform shortcuts
- [ ] Delivery & clipboard: method, paste shortcut, pre-paste delay, restore clipboard + delay, type: also copy, Enter delay + per-100-chars, macOS key timing
- [ ] Writing & formatting: style + your styles (CRUD for `postprocess.styles`), capitalize, end punctuation, drop final period, lowercase, filler words + list, stutters, spoken lists + numbered/bullets; voice punctuation on, keep English cues, absorb punctuation, cue table editor (`inline_formatting_replacements`)
- [ ] Smart editing: scratch-that + phrases, backtrack + cues, times, emails, URLs
- [ ] Local AI (Ollama): reachability chip, polish toggle, address + Test, model (list from Ollama when reachable), timeout, polish instruction
- [ ] Floating bar & sounds: show bar, position (six), click-through, three sound toggles, sound files table with Play and Choose
- [ ] Privacy & logs: history count + Clear (modal), log text to app.log, audit trail, log level, file logging + name, console output + level, update check, Open data folder
- [ ] System: tray icon + tooltip, hide console at start, terminal title (three prefixes), local API (host, port, on/off), GPU setup, settings export/import, model export/import (+ keep in place), reset all (keeps words), uninstall (guided modal)
- [ ] Section search highlight, per-section "Reset this section", deep links `#/settings/<section>/<key>`
- [ ] **Your manual test (M2):** change every section; confirm nothing asks for a restart; switch model and mic while dictating

7. Floating bar, complete
- [ ] All states from the mockup: resting, hover, recording, hands-free locked, live preview (LTR and RTL), transcribing with elapsed time, delivered (Copy, Again), copy-only app (link to the rule), no speech / mic muted, command mode, rephrase, paused (Resume), background task with progress, no text field (Open → shows transcript in the main window instead of the old Tk fallback window)
- [ ] Map events to states: `state`, `pipeline`, `notify(kind)`, `task`, `streaming_text`
- [ ] Quick actions popover (⋯ or right-click): profile chips, language chips + More, paste last, copy last, transform selection (submenu), add word, auto-paste / continuous / hotkeys toggles, recent dictations (click = copy), bar position picker, Open app, Quit
- [ ] Bar buttons must not break delivery: remember the previous foreground window and restore it before "Paste again"
- [ ] Window sized exactly to the current state (no click-through needed); positioned only via `SetWindowPos(SWP_NOACTIVATE)`; animations under 150 ms inside the page; respects reduced-motion
- [ ] Positions: six presets; per-monitor DPI; stays on the primary monitor (follow-cursor is a later feature)
- [ ] Optional: hide while a fullscreen app is in front

8. Data screens
- [ ] **History & insights** (`services/history.py`, `services/insights.py`)
  - [ ] Refactor `stats.show_stats()` into `compute_stats()` (dict) + the existing printer
  - [ ] Totals: dictations, words, time saved, speaking pace, streak; words per day (14 days); where you dictate; language split
  - [ ] Transcript list: search, app filter, period filter, paging; `dir="auto"` per row; Copy; Fix a word everywhere (writes `postprocess.replacements`, existing logic)
  - [ ] Suggest words to add (`suggest_hotwords_from_history`), Export .txt/.md/.csv (`export_transcripts`), Clear history (modal)
- [ ] **Vocabulary** (`services/vocabulary.py`)
  - [ ] Favored words (`whisper.hotwords`): tags, add, remove, suggestions from history, import from a folder (non-interactive split of `vocab_import.import_vocab`: scan → preview list with checkboxes → merge)
  - [ ] Corrections (`postprocess.corrections`) CRUD
  - [ ] Replacements (`postprocess.replacements`): find, replace, whole word, match case, pattern; invalid regex shown inline
  - [ ] Snippets (`postprocess.snippets`): trigger, expansion (multi-line), anywhere/alone, `${date}` `${time}` `${clipboard}` hint
- [ ] **Commands & AI** (`services/commands.py`)
  - [ ] Write API in `defaults_merge.py` for layered files: save override, add own entry, disable shipped entry, delete own entry; keeps the overrides-only format
  - [ ] Voice commands list grouped by action (keys / type / run / AI), origin badge (shipped / changed / yours), enable toggle, global on/off, "Try a phrase" box (`match_command`)
  - [ ] Command editor: trigger or pattern, ordered steps (keys / type / run / AI rephrase / wait ≤ 60 s), always-ask toggle (`confirm`), run-command warning
  - [ ] Transforms: list with description, shortcut capture, model override, timeout, instruction editor, enable; Ollama status
- [ ] **Apps & profiles** (`services/rules.py`, profiles)
  - [ ] Rules list in order with drag reorder, enable toggle, "matching now" badge from `foreground`, "Rule for this app" (prefilled from foreground)
  - [ ] Rule editor: applies to (exe/title tags), auto-send default/always/never, paste default/paste/copy-only, suppress, language, output, extra prompt, style, the six formatting overrides (from style / on / off)
  - [ ] Profiles: cards with summary chips, Use, Active badge, edit (overrides editor), new, delete own
- [ ] **Diagnostics** (`services/diagnostics.py`)
  - [ ] Refactor `doctor.run_doctor()` into `collect_checks()` → list of {name, status, detail, action} + the existing printer
  - [ ] Health check list with actions (Install GPU libraries, How to start Ollama…), Run again
  - [ ] Self-test as a streamed task (mic, load model, transcribe sample, clipboard) — refactor `selftest` into steps
  - [ ] Report a bug: `bundle_logs` returns the zip path; Save-as dialog; "What's inside" modal
  - [ ] Your shortcuts (replaces the cheat sheet window), files & folders (data, log, model cache, settings, crash reports), version + Check for updates

9. Simple mode — **part of Milestone 4**
- [ ] `settings/simple_mode.py`: pure functions `to_patch(question, answer, context)` and `from_settings(settings) → answers` (returns "custom" when values don't match a preset). Unit-tested both ways
- [ ] Simple Home: big keys for the record hotkey, "Try it here" practice box, last thing you said (Copy, Paste again, A word was wrong?), three tips, today line
- [ ] Simple Settings: Microphone & sound card first (picker, Test, live meter, reduce background noise, I speak quietly, pause music, stop when quiet, play sounds), then talking (language, how to start + change keys, where text goes), speed or accuracy, how text looks + say punctuation + teach words, app (start with computer, show the bar)
- [ ] "Recommended for this computer": quick on-device benchmark (reuse selftest's transcribe step on a bundled 3 s clip) picks the Balanced model
- [ ] Mode switch in the sidebar (Simple | Advanced) on every screen; default `simple` for new installs, `advanced` when an upgrading user already has non-trivial overrides
- [ ] In Simple, hide Commands & AI, Apps & profiles, Diagnostics and the console; a "Switch to Advanced" card at the bottom of Simple Settings

10. First-run welcome (the CLI-era stand-in for the installer's questions)
- [ ] Three steps in the main window: 1) language, 2) microphone test, 3) try it once in the practice box
- [ ] When a GPU is detected: "Use your graphics card for faster dictation?" with download size; runs the existing `onboarding._install_gpu_packages` as a background task with streamed pip output; replaces the blocking console prompt (`run_gpu_onboarding`)
- [ ] Model download happens during the welcome with progress, not silently on first dictation
- [ ] Sets `ui.welcome_done`; reachable later from Diagnostics ("Run setup again")
- [ ] Replaces `first_run.py` window and the console welcome banner (`onboarding_tutorial`)

11. Retire the legacy UI — **done 2026-10-07, except the deletions, which wait one release (D6)**
- [ ] Remove after one release: `settings_ui.py`, `history_window.py`, `cheat_sheet.py`, `fallback_window.py`, `first_run.py`, `level_overlay.py`, `window_launcher.py`, the add-word dialog in `dictionary.py`, `onboarding_tutorial.py` (kept behind `--legacy-ui` for this release, per D6)
- [x] CLI flags `--settings`, `--history`, `--cheat-sheet` open the new window on that page (`--stats` opens History); `--window` stays until the deletions, since `--legacy-ui` still uses it
- [x] Slim the tray menu: Open, Pause hotkeys, Profile, Recent, Quit; everything else lives in the app (the full menu stays with `--legacy-ui`)
- [x] Update `docs/project-index.md`, `docs/platform-abstraction.md`, README (features; screenshots still to take), `docs/faq.md`, `docs/troubleshooting.md` (WebView2), CHANGELOG
- [x] Keep `DocumentationStandardTests` green: every new module gets its 2–4 line header comment

12. Hardening and QA — **Milestone 5: done on Windows 2026-10-07, results below; the items left open need hardware or CI**
- [x] Performance budget (revised after Phase 0): idle CPU < 0.5%, recording < 3%; memory over today ≤ +250 MB with only the bar, ≤ +300 MB with the window open; measure and record
- [ ] DPI 100/125/150/200%, two monitors, taskbar on any edge (placement maths tested for scaling, every taskbar edge and small screens; not yet tried on a real second monitor or at 200%)
- [x] Accessibility: keyboard-only pass on every screen, focus order, screen-reader labels, 4.5:1 contrast in both themes
- [x] Hebrew: RTL transcripts in Live, History, bar preview; Hebrew phrases in tag inputs and tables
- [ ] Failure paths: Ollama down, no microphone, mic unplugged mid-recording, model download interrupted, disk full, invalid hand-edited YAML, WebView2 missing (done: Ollama down, interrupted background change, invalid YAML, WebView2 missing; open: no microphone, unplug mid-recording, disk full, which need the hardware)
- [x] Offline: no network calls from the UI (enforced by the page CSP `connect-src 'self'` and a test that pages load no remote URLs; the only outbound calls are the user's own actions: model download, update check, Ollama probe)
- [ ] `python -m unittest tests.test_smoke` green on Windows, macOS, Linux CI (UI modules import without pywebview present) — green on Windows (329 tests); macOS and Linux CI not run from here
- [ ] Optional: Playwright smoke run against `?mock=1` in CI (every route renders, no console errors) — done by hand this session, not wired into CI

#### Phase 11–12 results (Windows 11, 24 cores, 5120×1440 at 100%, WebView2 154)

Memory is Task Manager's "private working set" over the whole process tree, WebView2 included.

| Case | Memory | Over the old UI | Budget |
|---|---|---|---|
| Old UI (`--legacy-ui`) | 68 MB | — | — |
| Floating bar only | 291 MB | +223 MB | ≤ +250 MB |
| App window open | 314 MB | +246 MB | ≤ +300 MB |

CPU, as a share of the whole machine (one core = 4.2%):

| Case | Before tuning | After tuning |
|---|---|---|
| Idle, bar only | 0.14% | 0.14% |
| Idle, window open | 0.13% | 0.13% |
| Recording, bar only | — | 0.41% |
| Recording, Live screen open | 4.9% | 1.24% |

Tuning: level bars and meters move with CSS transforms instead of width and
height, the recording clock redraws on its own, and the pulsing dots step
instead of animating every frame. Most of what is left while recording is
Python sending levels to two pages 20 times a second.

Bugs found by running the real app and fixed:
- A GPU load failure opened a blocking console menu even with the app window; now CPU fallback plus an Install notice.
- A first start counted as an upgrade (the app saves the audio driver by itself), so new users skipped the welcome steps.
- `--open` forced the Live screen even in Simple mode and before the welcome steps.
- `--quit` ignored `--test` and hard-killed the process; it now asks the app to quit over the instance channel.
- A broken hand edit of `user_settings.yaml` reset the running app to defaults (hotkeys too) and saved them over the file.
- The quick-actions menu was cut off by a fixed 640 px cap; the bar could also start off screen on small displays.
- Dialogs didn't take or return focus; two colours were under 4.5:1.

#### Audit of Phases 1–10 against the code (2026-10-07)

About 85 of the 110 checklist items match the plan as written; the rest were
built differently on purpose (a small store instead of Preact signals, one
mock data file instead of fixtures, up/down buttons instead of drag to reorder
rules) or had gaps. Gaps closed after the audit:
- The bar and Live show **hands-free** when a double-tap locks recording, the
  bar shows elapsed time while transcribing, and "nothing to type" has its own
  message.
- A failed GPU switch offers **Install GPU support** in the window.
- Noise suppression without its package now warns and offers **Install**
  (Settings and Simple mode), instead of silently doing nothing.
- A maximized window opens maximized again.
- Diagnostics lists the window's own shortcuts.

Still open, by choice or for later:
- **First launch shows nothing while the model downloads** (about 141 MB for
  `base`): the engine loads before the UI. The installer (Phase 13) downloads
  the model during install; until then the console shows the progress.
- "Recommended for this computer" in Simple mode is a fixed choice (Balanced),
  not a benchmark.
- The Live Context card has no "effective prompt" row, and Today shows speaking
  pace instead of average latency.
- Ctrl+K searches screens and settings, not commands or past dictations.
- `doctor` and `selftest` are reused by wrapping them, not refactored.
- The bar doesn't hide while a fullscreen app is in front (optional item).
- Tests don't yet cover the applier dispatch order, the background swap queue
  or the save debounce.

13. Production packaging and installer (later — not part of the CLI phase)
- [ ] Build a frozen app (PyInstaller or Nuitka) instead of pyapp's first-run Python download, so install works offline and starts fast
- [ ] Windows installer (Inno Setup): install location, Start menu + optional desktop shortcut, start on login, **GPU acceleration page** (detect NVIDIA/AMD, show download size, install CUDA/ROCm runtime or skip), download a model now or import an offline model, language preselect, WebView2 runtime bootstrapper when missing
- [ ] Uninstaller entry in Apps & Features that calls the existing uninstall logic (keeps models unless asked)
- [ ] Code signing to remove the SmartScreen warning; checksums in the release
- [ ] Auto-update channel (opt-in, matches the privacy pledge)
- [ ] winget and Homebrew manifests updated to the installer

Out of scope for this plan, next phase: language work (better automatic language detection, mixed Hebrew/English, per-language punctuation and fillers shipped by default, Hebrew UI translation).

14. macOS port (later)
- [ ] Spike: pywebview's Cocoa loop together with pystray and the Quartz hotkey tap on the main thread
- [ ] Bar window as a non-activating `NSPanel`; menu-bar icon instead of tray
- [ ] Accessibility and microphone permission screens in the welcome flow

## Implementation Details

### Instant apply: how one change flows

```
JS toggle ──► api.settings.set("audio.input_device", 3)
  Bridge.settings_set ──► SettingsService.set(path, value, source="ui")
     1. coerce + validate (schema)            → error returns to JS, toggle rolls back
     2. ConfigManager.set_path + debounced write of user_settings.yaml
     3. applier:
          live        → apply now                         → {"applied": "live"}
          background  → TaskManager.run("Switching microphone", fn)
                                                          → {"applied": "background", "task_id": 7}
          next_launch → nothing now                       → {"applied": "next_launch"}
     4. EventBus.publish("settings", {path, old, new})     → toast "Saved · Undo", other windows update
     5. undo stack push
TaskManager ──► "task" events ──► title-bar chip, bar state, inline row progress
```

Background swap rule: the running component is never torn down first. Build the replacement off-thread, warm it up, then swap the reference under `StateManager._state_lock`. If a recording or transcription is in progress, the swap waits until idle. On failure the old component stays and the task shows the error with an action.

### Bridge surface (JS → Python)

| Area | Methods |
|---|---|
| app | `bootstrap()` (version, platform, schema, settings, state, devices, models, profiles, tasks, last 200 logs), `open_url(url)`, `quit()` |
| window | `minimize()`, `toggle_maximize()`, `close_to_bar()`, `open_page(route)` |
| dictation | `start()`, `stop(send_enter)`, `cancel()`, `set_paused(bool)`, `paste_last()`, `copy_last()`, `recent()` |
| settings | `get(path)`, `set(path, value)`, `undo()`, `reset_section(name)`, `reset_all()`, `export(dir)`, `import(dir)` |
| audio | `hosts()`, `devices(host)`, `test_start()`, `test_stop()`, `preview_sound(name)` |
| models | `list()`, `download(key)`, `delete(key)`, `add_hf(repo, label)`, `add_folder(path, label)`, `set_visible(key, bool)`, `export(dest)`, `import(src, keep_in_place)` |
| gpu | `detect()`, `install()` (task) |
| history | `query(text, app, period, offset, limit)`, `export(fmt, path)`, `clear()`, `fix_everywhere(frm, to)`, `suggest_words()` |
| insights | `summary()`, `daily(days)`, `apps()`, `languages()` |
| vocabulary | `words()`, `add_word(w)`, `remove_word(w)`, `scan_folder(path)`, `merge_words(list)`, corrections / replacements / snippets `list/save/delete` |
| commands | `list()`, `save(cmd)`, `disable(trigger)`, `delete(trigger)`, `test_phrase(text)`, `set_enabled(bool)` |
| transforms | `list()`, `save(t)`, `delete(name)`, `apply(name)` |
| rules | `list()`, `save(rule)`, `reorder(ids)`, `delete(id)`, `from_foreground()` |
| profiles | `list()`, `activate(name)`, `save(p)`, `delete(name)` |
| ai | `probe()`, `models()` |
| diagnostics | `checks()`, `selftest()` (task), `bundle(path)`, `shortcuts()`, `open_path(kind)` |
| system | `autostart_get()`, `autostart_set(bool)`, `server_status()`, `check_update()`, `uninstall_plan()`, `uninstall(remove_models)` |
| tasks | `list()`, `cancel(id)` |

Events (Python → JS): `state`, `level`, `vad`, `streaming_text`, `pipeline`, `notify`, `log`, `task`, `settings`, `foreground`, `devices_changed`.

### Every setting: where it lives and how it applies

Legend: **L** live now · **N** live from the next recording · **B** background task · **X** next launch · **I** internal. **S** = also shown in Simple mode (through a plain-language question).

| Keys | Screen › section | Apply | Simple |
|---|---|---|---|
| `whisper.language` | General | L | S: What language do you speak? |
| `whisper.task` | General (Output) | L | — |
| `hotkey.recording_mode` | General | L | S: How do you want to start talking? |
| `clipboard.auto_paste` | General | L | S: Where should the text go? |
| `audio.continuous_mode` | General | L | — |
| (OS) start on login | General / System | L | S: Start when the computer starts |
| `audio.input_device` | Microphone & audio | B | S: Microphone |
| `audio.host` | Microphone & audio | B | — |
| `audio.channels`, `audio.dtype` | Microphone & audio | B | — |
| `audio.max_duration` | Microphone & audio | N | — |
| `audio.pause_media_on_record` | Microphone & audio | N | S: Pause my music while I talk |
| `audio.noise_suppression.enabled` | Microphone & audio | N (+B install extra) | S: Reduce background noise |
| `audio.noise_suppression.strength` | Microphone & audio | N | — |
| `audio.whisper_mode.enabled` | Microphone & audio | N | S: I speak quietly |
| `audio.whisper_mode.max_gain` | Microphone & audio | N | — |
| `vad.vad_precheck_enabled`, `vad.vad_onset_threshold`, `vad.vad_offset_threshold`, `vad.vad_min_speech_duration` | Microphone & audio › Voice detection | N | — |
| `vad.vad_realtime_enabled`, `vad.vad_silence_timeout_seconds` | Microphone & audio › Voice detection | N | S: Stop listening when I go quiet |
| `whisper.model` | Model & engine | B | S: Speed or accuracy? |
| `whisper.device`, `whisper.compute_type` | Model & engine | B | S: (set by Speed or accuracy) |
| `whisper.backend` | Model & engine | B | — |
| `whisper.models` (+ `enabled` per model) | Model & engine (table) | L | — |
| `whisper.beam_size` | Model & engine | L | — |
| `whisper.initial_prompt` | Model & engine | L | — |
| `whisper.prompt_from_selection` | Model & engine | N | — |
| `whisper.hotwords` | Vocabulary › Favored words | L | S: Names and words it gets wrong |
| `streaming.streaming_enabled`, `streaming.streaming_model` | Model & engine › Live preview | B | — |
| `streaming.deliver_to_cursor` | Model & engine › Live preview | N | — |
| `streaming.models` | Model & engine › Live preview | L | — |
| `hotkey.recording_hotkey` | Hotkeys | L | S: Change keys |
| `hotkey.stop_key`, `hotkey.auto_send_key`, `hotkey.cancel_combination`, `hotkey.command_hotkey`, `hotkey.rephrase_hotkey`, `hotkey.pause_hotkey`, `hotkey.paste_last_hotkey` | Hotkeys | L | — |
| `hotkey.double_tap_to_lock`, `hotkey.double_tap_window_ms` | Hotkeys | L | — |
| `clipboard.delivery_method`, `clipboard.paste_hotkey`, `clipboard.paste_pre_paste_delay`, `clipboard.paste_preserve_clipboard`, `clipboard.paste_clipboard_restore_delay`, `clipboard.type_also_copy_to_clipboard`, `clipboard.type_auto_enter_delay`, `clipboard.type_auto_enter_delay_per_100_chars`, `clipboard.macos_key_simulation_delay` | Delivery & clipboard | L | — |
| `postprocess.style` | Writing & formatting | L | S: How should your text look? |
| `postprocess.styles` | Writing & formatting › Your styles | L | — |
| `postprocess.capitalize_first`, `postprocess.ensure_punctuation`, `postprocess.strip_trailing_period`, `postprocess.lowercase` | Writing & formatting | L | S: (set by text look) |
| `postprocess.strip_filler_words`, `postprocess.remove_repeated_words`, `postprocess.filler_words` (exists once the Hebrew branch lands) | Writing & formatting | L | S: (set by text look) |
| `postprocess.list_formatting`, `postprocess.list_style` | Writing & formatting | L | — |
| `postprocess.inline_formatting` | Writing & formatting › Voice punctuation | L | S: Say punctuation out loud |
| `postprocess.inline_formatting_extend`, `postprocess.inline_formatting_absorb_punctuation`, `postprocess.inline_formatting_replacements` | Writing & formatting › Voice punctuation | L | — |
| `postprocess.voice_editing`, `postprocess.voice_editing_phrases` (exists once the Hebrew branch lands) | Smart editing | L | — |
| `postprocess.backtrack.enabled`, `postprocess.backtrack.cues` | Smart editing | L | — |
| `postprocess.smart_formatting.times`, `.emails`, `.urls` | Smart editing | L | — |
| `postprocess.corrections` | Vocabulary › Corrections | L | — |
| `postprocess.replacements` | Vocabulary › Replacements | L | S: "A word was wrong?" on Home |
| `postprocess.snippets` | Vocabulary › Snippets | L | — |
| `postprocess.ollama.enabled`, `.endpoint`, `.model`, `.timeout`, `.prompt` | Local AI (Ollama) | L | — |
| `overlay.enabled` | Floating bar & sounds | L | S: Show the small bar on screen |
| `overlay.position`, `overlay.click_through` | Floating bar & sounds (+ bar quick actions) | L | — |
| `audio_feedback.enabled` | Floating bar & sounds | L | S: Play sounds |
| `audio_feedback.transcription_complete_enabled`, `audio_feedback.ready_enabled` | Floating bar & sounds | L | — |
| `audio_feedback.start_sound`, `.stop_sound`, `.cancel_sound`, `.transcription_complete_sound`, `.ready_sound` | Floating bar & sounds › Sound files | L | — |
| `logging.log_transcriptions`, `logging.level`, `logging.file.enabled`, `logging.file.filename`, `logging.console.enabled`, `logging.console.level` | Privacy & logs | L | — |
| `audit.enabled` | Privacy & logs | L | — |
| `update_check.enabled` | Privacy & logs | L | — |
| `voice_commands.enabled` | Commands & AI (header switch) | L | — |
| `system_tray.enabled` | System | B | — |
| `system_tray.tooltip` | System | L | — |
| `console.start_hidden` | System | X | — |
| `terminal_title.idle`, `.recording`, `.processing` | System | L | — |
| `onboarding.gpu`, `onboarding.gpu_class` | System › GPU setup (action) | I | Welcome step |
| new `server.enabled`, `server.host`, `server.port` | System › Local API | B | — |
| new `ui.mode`, `ui.theme`, `ui.open_on_start`, `ui.window`, `ui.welcome_done` | Sidebar switch, title bar, internal | L / I | mode switch |

Other user files and data:

| Source | Where in the UI |
|---|---|
| `commands.yaml` (layered over shipped) | Commands & AI › Voice commands + editor |
| `transforms.yaml` | Commands & AI › Transforms |
| `app_rules.yaml` | Apps & profiles › App rules |
| `profiles.yaml` (+ `active`) | Apps & profiles › Profiles, sidebar profile picker, bar quick actions |
| `transcripts.jsonl`, `stats.jsonl` | History & insights, Live › Today, Simple Home |
| `audit.log`, `app.log`, `crashes/` | Privacy & logs, Live › Console, Diagnostics › Files |
| Hugging Face cache | Model & engine table (status, delete), Diagnostics › Files |

Every CLI flag keeps working; this is where each one lives in the UI:

| Flag | UI |
|---|---|
| `--doctor` | Diagnostics › Health check |
| `--selftest` | Diagnostics › Self-test |
| `--bundle-logs` | Diagnostics › Report a bug |
| `--cheat-sheet` | Diagnostics › Your shortcuts |
| `--settings` | opens `#/settings` |
| `--history`, `--stats`, `--export-transcripts` | History & insights (+ Export) |
| `--add-word`, `--remove-word`, `--list-dictionary`, `--import-vocab` | Vocabulary › Favored words |
| `--setup` | Welcome flow (Diagnostics › Run setup again) |
| `--enable-autostart`, `--disable-autostart` | General / System › Start on login |
| `--export-settings`, `--import-settings` | System › Settings backup |
| `--export-model`, `--import-model`, `--keep-in-place` | System › Model for an offline computer |
| `--serve`, `--serve-host`, `--serve-port` | System › Local API |
| `--uninstall` | System › Uninstall (guided modal) |
| `--quit` | Bar quick actions › Quit, tray › Quit |
| `--version` | Diagnostics header |
| `--test` | CLI only (separate test instance) |
| `--transcribe-system` | CLI only for now (experimental); candidate for a later "Transcribe what I hear" feature |
| `--window` | removed in Phase 11 (internal Tk launcher) |

Tray items today → new home: Profile, Language, Model, Audio host/device, Overlay position, Transforms, Recent, Auto-paste → bar quick actions and Live; Settings…, Edit settings/commands/transforms file…, Open config folder/log/model cache → Settings and Diagnostics › Files; Run diagnostics/self-test, View stats, Transcript history, Hotkey cheat sheet, Bundle logs, Add word → their screens; Show Console → Live › Console; Restart → no longer needed (background reloads); Exit → Quit.

### Simple mode mappings

| Question | Answer | Settings patch |
|---|---|---|
| What language do you speak? | Detect automatically / a language | `whisper.language: auto` / code. Choosing Hebrew also offers the Hebrew model and adds the Hebrew punctuation cues |
| How do you want to start talking? | Hold the keys / Press once | `hotkey.recording_mode: push_to_talk` / `toggle` |
| Change keys | key capture | `hotkey.recording_hotkey` |
| Where should the text go? | Type it where I'm writing / Just copy it | `clipboard.auto_paste: true` / `false` |
| Speed or accuracy? | Fastest / Balanced / Most accurate | `whisper.model`, `whisper.device`, `whisper.compute_type` from the table below |
| Microphone | picker | `audio.input_device` (`audio.host` stays automatic) |
| Reduce background noise | on/off | `audio.noise_suppression.enabled` (installs the extra first if needed) |
| I speak quietly | on/off | `audio.whisper_mode.enabled` |
| Pause my music while I talk | on/off | `audio.pause_media_on_record` |
| Stop listening when I go quiet | Never / 10 s / 30 s / 1 min | `vad.vad_realtime_enabled: false` / `true` + `vad.vad_silence_timeout_seconds: 10/30/60` |
| Play sounds | on/off | `audio_feedback.enabled` |
| How should your text look? | Exactly as I said it | `style: verbatim`, `strip_filler_words: false`, `remove_repeated_words: false` |
| | Clean | `style: formal`, `strip_filler_words: true`, `remove_repeated_words: true` |
| | Relaxed | `style: casual`, `strip_filler_words: true`, `remove_repeated_words: true` |
| Say punctuation out loud | on/off | `postprocess.inline_formatting` |
| Names and words it gets wrong | word list | `whisper.hotwords` |
| Start when the computer starts | on/off | autostart (Run key / LaunchAgent) |
| Show the small bar on screen | on/off | `overlay.enabled` |

Speed or accuracy presets (starting point, confirmed by the on-device benchmark in Phase 9):

| Language | Hardware | Fastest | Balanced | Most accurate |
|---|---|---|---|---|
| English | CPU | `base.en`, int8 | `small.en`, int8 | `large-v3-turbo`, int8 |
| English | NVIDIA GPU | `small.en`, float16 | `large-v3-turbo`, float16 | `large`, float16 |
| Hebrew (ivrit.ai models configured) | CPU | `small`, int8 | `hebrew-large-v3-turbo`, int8 | `hebrew-large-v3`, int8 |
| Hebrew (ivrit.ai models configured) | NVIDIA GPU | `hebrew-large-v3-turbo`, float16 | `hebrew-large-v3-turbo`, float16 | `hebrew-large-v3`, float16 |
| Hebrew (no ivrit.ai models) | any | same as "Other / auto" | | |
| Other / auto | CPU | `base`, int8 | `small`, int8 | `large-v3-turbo`, int8 |
| Other / auto | NVIDIA GPU | `small`, float16 | `large-v3-turbo`, float16 | `large`, float16 |

When current values match no preset, Simple shows the answer as "Custom (set in Advanced)" and does not overwrite it until the user picks an answer.

### Floating bar adapter (keeps StateManager changes small)

**Before** (`state_manager.attach_components`):
```python
self.level_overlay = LevelOverlay(
    level_provider=self.audio_recorder.get_current_level,
    click_through=overlay_cfg.get('click_through', True),
    position=overlay_cfg.get('position', 'bottom-center'),
)
```

**After:**
```python
overlay_cls = LevelOverlay if self.legacy_ui else FloatingBar
self.level_overlay = overlay_cls(
    level_provider=self.audio_recorder.get_current_level,
    click_through=overlay_cfg.get('click_through', True),
    position=overlay_cfg.get('position', 'bottom-center'),
)
```

`FloatingBar` implements the same methods by publishing bar events; the extra states (command, rephrase, paused, task, copy-only, no text field) come from the `state`, `notify` and `task` events, not from new StateManager calls.

### Main loop handover

**Before** (`main.py`):
```python
system_tray.apply_console_settings()
app.run_event_loop(shutdown_event)
```

**After:**
```python
system_tray.apply_console_settings()
if args.legacy_ui or not ui_available():
    app.run_event_loop(shutdown_event)
else:
    UiHost(services, bus, settings, open_page=args.page if args.open else None,
           dev=args.ui_dev).run(shutdown_event)   # blocks on the main thread
```

### Scope: files

New (needs your OK, decision D3):

| Path | Purpose |
|---|---|
| `src/whisper_key/ui/host.py` | pywebview windows and main-thread loop |
| `src/whisper_key/ui/bridge.py` | `js_api` surface |
| `src/whisper_key/ui/events.py` | EventBus with throttling |
| `src/whisper_key/ui/log_stream.py` | log handler + ring buffer |
| `src/whisper_key/ui/tasks.py` | background TaskManager |
| `src/whisper_key/ui/floating_bar.py` | LevelOverlay-compatible bar adapter |
| `src/whisper_key/ui/instance_ipc.py` | "open the running app" pipe |
| `src/whisper_key/settings/schema.py` | one entry per setting |
| `src/whisper_key/settings/service.py` | set / validate / apply / undo |
| `src/whisper_key/settings/appliers.py` | live and background appliers |
| `src/whisper_key/settings/simple_mode.py` | Simple question ↔ settings mapping |
| `src/whisper_key/services/{history,insights,vocabulary,commands,rules,models,diagnostics,system}.py` | screen data, thin wrappers |
| `src/whisper_key/ui/web/index.html`, `bar.html` | the two windows |
| `src/whisper_key/ui/web/css/{tokens,components}.css` | design tokens, shared components |
| `src/whisper_key/ui/web/js/{api,mock-api,store,router,app}.js` | runtime |
| `src/whisper_key/ui/web/js/components/*.js` | toggle, segmented, key capture, tag input, table editor, toast, modal, popover |
| `src/whisper_key/ui/web/js/screens/*.js` | live, home, history, vocabulary, commands, apps, settings, simple-settings, diagnostics, welcome |
| `src/whisper_key/ui/web/{vendor,fonts,fixtures}/` | Preact/htm, Geist fonts + OFL, mock data |

Modified:

| File | Change |
|---|---|
| `main.py` | new flags; UiHost handover; legacy fallback |
| `state_manager.py` | bus publishes; FloatingBar; pipeline events; profile activation via SettingsService; atomic engine/recorder swap |
| `whisper_engine.py`, `whisper_engine_cpp.py` | `last_info`; settable `beam_size`/`hotwords` |
| `audio_recorder.py` | level publish; mic test preview; apply_config |
| `voice_activity_detection.py` | `last_probability`; vad events |
| `hotkey_listener.py` | rebuild for double-tap changes; conflict warnings |
| `clipboard_manager.py`, `audio_feedback.py`, `terminal_title.py` | `apply_config` |
| `config_manager.py`, `config.defaults.yaml` | `get_path/set_path`, lock, debounce; `ui:` and `server:` sections |
| `text_postprocess.py` | optional `trace` |
| `stats.py`, `doctor.py`, `selftest.py`, `bundle_logs.py`, `vocab_import.py` | structured, non-interactive entry points beside the printers |
| `defaults_merge.py`, `voice_commands.py`, `transforms.py`, `app_rules.py`, `profiles.py` | write API for layered files |
| `model_registry.py` | list with cache status, download with progress, delete |
| `onboarding.py` | GPU install as a non-interactive task |
| `local_server.py` | run in-process on a thread with the shared engine |
| `instance_manager.py` | open-existing instead of takeover; `--replace` |
| `system_tray.py` | Open item; subscribe to notify; slim menu (Phase 11) |
| `pyproject.toml` | `pywebview` dependency; package-data `ui/web/**` |
| `tests/test_smoke.py` | new test classes listed in each phase |
| docs | project-index, platform-abstraction, README, faq, troubleshooting, CHANGELOG |

Removed in Phase 11: `settings_ui.py`, `history_window.py`, `cheat_sheet.py`, `fallback_window.py`, `first_run.py`, `level_overlay.py`, `window_launcher.py`, `onboarding_tutorial.py`, add-word dialog code in `dictionary.py`.

## Success Criteria

- [ ] `whisper-local` starts with the floating bar and tray; `--open` shows the window; closing the window leaves the bar and dictation running
- [ ] Launching `whisper-local` again while it runs opens the existing window instead of replacing the process
- [ ] Live shows waveform, input level, VAD probability with thresholds, silence countdown, foreground app and matched rule, and the four-step trace of the last dictation, all updating in real time
- [ ] The console shows new log lines within 250 ms, filters by level and text, and pauses without losing lines
- [ ] Every setting in `config.defaults.yaml` (plus `ui.*` and `server.*`) is editable in Advanced; the schema coverage test fails if a key is added to `config.defaults.yaml` without a UI entry
- [ ] No setting asks for an app restart. Model, device, precision, engine, microphone, audio driver, channels, format, live preview, tray and API server switch in the background with progress, while dictation keeps working on the old setting
- [ ] Every change shows "Saved" with Undo; Undo restores and re-applies the previous value
- [ ] Simple mode shows only the plain-language questions; each answer produces exactly the patch in the mapping table; unmatched values show as Custom and are not overwritten
- [ ] The floating bar shows all 15 states, never takes keyboard focus from the app being typed into (including at startup), and has no taskbar button
- [ ] History, Vocabulary, Commands & AI, Apps & profiles and Diagnostics read and write the same files the CLI uses; edits made in YAML by hand show up in the UI
- [ ] Light theme on first run; dark theme toggles instantly and persists
- [ ] No `alert()`, `confirm()` or `prompt()` in the codebase (grep test)
- [ ] Works with no network connection; the UI makes no network calls
- [ ] Idle CPU under 0.5%, recording under 3%
- [ ] Smoke suite green on all three CI platforms; UI modules import without pywebview installed

## Risks

| Risk | Mitigation |
|---|---|
| pywebview transparent / no-activate windows unreliable on Windows | Confirmed in Spike B: no transparency, focus taken at create/load. Resolved by native rounded corners, one-time focus restore, Win32 no-activate positioning |
| WebView2 memory (~200 MB fixed) | Measured in Phase 0; budget revised; keep one browser environment shared by both windows; revisit a Tk bar only if users complain |
| Clicking the bar steals focus and "Paste again" lands in the bar | Create the bar with `WS_EX_NOACTIVATE`; restore the previous foreground window before any paste |
| WebView2 runtime missing on some Windows 10 machines | Detect at start; message with download link; automatic `--legacy-ui` fallback; installer bootstraps it (Phase 13) |
| pythonnet / pywebview lag behind new Python versions | Pin tested versions; CI on 3.11–3.13 |
| Hot-swapping the engine races with a transcription | Swap only under `_state_lock` when idle; queue otherwise (existing pattern) |
| Writing YAML on every slider move | 300 ms debounce, flush on exit |
| UI pushes too many events and costs CPU | Throttle per topic, push only to visible windows, measure in Spike C |
| A JS bug hides settings | Schema-driven rows plus the coverage test; mock-mode Playwright smoke |
| Big-bang change breaks daily use | `--legacy-ui` for one release; `--test` instance for side-by-side use |
| macOS main-thread conflicts (pywebview + pystray + Quartz tap) | macOS is its own later phase with its own spike |
| Simple presets pick a model too slow for the machine | On-device benchmark picks Balanced; the background switch can be undone |

## References

- Mockup canvas: https://claude.ai/artifact/L6ujxPkwgG1MSAEu7wZ2Rt
- Earlier plans: `docs/plans/2026-02-03-first-run-onboarding.md`, `docs/plans/2026-03-16-gpu-onboarding.md`
- `docs/platform-abstraction.md` (mirrored platform API contract)
- `docs/AUDIT.md` (multi-Tk-root decision, threading notes)
- pywebview docs: windows, `js_api`, `evaluate_js`, frameless and transparent windows
