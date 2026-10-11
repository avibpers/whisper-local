# Meeting mode: transcribe calls as they happen, with who said what

*Design spec, 2026-10-11. Approved in conversation; implementation plan in
`docs/plans/2026-10-11-meeting-mode-implementation.md`.*

## Goal

When a call is running in Discord, Zoom, Teams or a browser tab (Google Meet
and the web versions of the others), Koli notices, listens to what comes out
of the speakers and to the microphone, and shows the conversation in a dark
translucent panel as it happens, each turn labelled with its speaker. The
meeting is saved as a transcript with speakers and times, so later steps
(questions during the call, summaries, notes) have something to work on.

This step builds four things: **detection**, **live transcription**,
**speaker labels**, and **meeting sessions** (when a meeting starts, pauses,
resumes and ends). Everything else is listed under Out of scope.

Languages in this step: Hebrew and English, mixed in one meeting. The
English-only streaming engine is not used; meetings run Whisper on short
chunks, so text appears a few seconds behind speech.

## Behaviour (agreed)

| Situation | What Koli does |
|-----------|----------------|
| A listed app opens an audio session (Discord, Zoom, Teams, Slack) | It is a *source*. Its rule decides: `auto` starts a meeting once speech is heard, `ask` offers one, `off` ignores it. |
| A browser has sound and a tab whose title matches a listed site ("Meet - abc-defg-hij", "Microsoft Teams", ...) | Same, with the tab as the source. Found through the tab strip, so the tab can be behind others. The Meet code from the title is the meeting code. |
| Source present, loopback hears about 3 s of speech (setting) | The meeting starts: panel appears, capture and transcription run. A stray ping never starts one. |
| Rule `ask` | The panel appears small: "Discord call detected. Transcribe?" with Start and Not now. Not now ignores that source until it goes away. |
| Start meeting from the tray, the hotkey or the Meetings screen | Starts at once, with whichever source is active, or "manual" when none. |
| Source goes away (session closed, tab closed, app quit) | Meeting **pauses**. Panel header says Paused. Nothing is captured. |
| Same app, and the same code when there is one, back within `gap_minutes` (default 10) | **Resumes** the paused meeting. Panel shows "Resumed: <title>" for a few seconds with a "Start new instead" button. |
| Different app, different code, or after the gap | A **new** meeting. Panel shows "New meeting" with a "Continue previous instead" button (only while the previous one is still within its gap). |
| Paused longer than the gap | The meeting **ends** and is saved. Panel shows "Saved, 42 min" briefly, then hides. |
| End meeting (panel, tray, hotkey) | Ends and saves now. |
| New meeting (panel) | Ends the current one and starts a fresh one with the same source. |
| Pause (panel) | Stops capture until Resume. Recorded as a gap with reason `paused`. |
| Exclude me (panel toggle) | Microphone chunks are dropped while on. Recorded as a gap with reason `excluded` on the mic stream only. The header shows "You're excluded". |
| A dictation recording (hotkey or auto-dictation) runs during a meeting | Those microphone seconds go to the dictation only, never into the meeting. The loopback keeps going. |
| A dictation is being transcribed | The meeting's chunks wait. Dictation keeps its latency. |
| Transcription falls behind (slow machine, big model) | Nothing is dropped. The header shows "25 s behind". |
| Loopback chunk transcribed | A voiceprint is taken; it joins the nearest known speaker or becomes "Speaker N". The turn shows with that speaker's colour. |
| Microphone chunk transcribed | Speaker "You", always the same colour. |
| Click a speaker name (panel or Meetings screen) | Rename for this meeting. Stored in the meeting file. |
| Screen sharing in the meeting app | The panel is hidden from capture when `hide_from_sharing` is on (default). The panel's header menu toggles it for this meeting. |
| `soundcard` (the loopback extra) is missing | The first start reports a problem with an Install button through the extras installer. Meeting mode stays off until it is installed. |
| Speaker-embedding model not downloaded | Downloaded on the first meeting as a background task with progress. Until it is ready, loopback turns are labelled "Speaker ?"; those earlier turns are not relabelled. |
| `--legacy-ui` | Meeting mode is off (the panel needs the app window's host). |
| `meeting.enabled: false` | Nothing is hooked: no detector thread, no loopback stream, no COM objects. Manual start still works from the Meetings screen (it starts the pieces for that meeting only). |

Default: **enabled, detection `auto`**. All of it applies live, no restart.

## Architecture

```
platform/windows/audio_sessions.py   platform/windows/browser_tabs.py
   active(): [{pid, exe, state}]        tabs(): [{exe, title, media}]
            |                                    |
            +------------------+-----------------+
                               v
              meeting/detector.py  (thread, 1 s tick)
              sources now: [{key, app, kind, title, code}]
                               | on_sources(list)
                               v
              meeting/session.py  ----------------> meeting/store.py
              SessionManager (pure logic)           <data>/meetings/<id>.json
                idle - offered - live - paused - ended
                resume-or-new, gaps, exclude me, rename
                               | start/stop capture        ^ turns
                               v                           |
              meeting/capture.py                  meeting/speakers.py
              LoopbackStream (soundcard thread)   SpeakerTracker: embedding ->
              MicTap (AudioRecorder hook)          nearest centroid or new
              Chunker (ten-vad, <= 15 s)                   ^
                               | chunks {audio, stream, t0, t1}
                               v                           |
              meeting/transcriber.py ----------------------+
              worker: waits for dictation idle, Whisper engine, turn out
                               | publish('meeting', ...) / publish('meeting_turn', ...)
                               v
              ui/host.py (3rd window: meeting.html) . services/meetings.py . bridge
              ui/web/meeting.html + js/meeting.js + css/meeting.css
              ui/web/js/screens/meetings.js (app window)
```

### Components

**`meeting/detector.py`: MeetingDetector.** A daemon thread. Each tick,
`audio_sessions.active()` gives the processes with an *active* audio session
(an open stream, which Discord keeps for the whole call, silent or not; a
notification ping opens none that lasts). Each exe is matched against the
meeting rules (see Settings). A browser exe (`chrome.exe`, `msedge.exe`,
`brave.exe`) is matched through its tabs instead: `browser_tabs.tabs()` (UI
Automation, cached 3 s) returns each tab's accessible name, which Chrome and
Edge build as `"<title> - <media state>"` (`Audio playing`, `Microphone in
use`, `Camera and microphone in use`). A tab whose title matches a rule's
title patterns is a source; a Meet code (`[a-z]{3}-[a-z]{4}-[a-z]{3}`) in the
title becomes `code`. The detector reports the current set of sources to the
session manager every tick and only logs changes. A source's `key` is
`exe` for native apps and `exe + tab title without the media suffix` for
tabs. Firefox: window title only (no tab strip), noted as a limitation.

**`meeting/session.py`: SessionManager + Meeting.** Pure Python, no I/O
except through the store and callbacks it is given (`start_capture`,
`stop_capture`, `publish`, `now`, `schedule`). Holds at most one *current*
meeting (live or paused) and the rules above. `on_sources(sources)` drives
detection transitions; `on_speech()` from the loopback chunker confirms a
start; `start()`, `end()`, `new()`, `pause()`, `resume()`,
`set_excluded(bool)`, `rename_speaker(id, name)`, `add_turn(turn)` are the
commands. Every state change publishes `meeting` with the full panel state.
A paused meeting owns a timer (through `schedule`) that ends it after the gap.

**`meeting/capture.py`.** `LoopbackStream`: a thread that opens
`soundcard.get_microphone(default_speaker.name, include_loopback=True)
.recorder(samplerate=16000, channels=1)` and reads 100 ms blocks until
stopped; reopens on error up to 3 times, then reports a problem. `MicTap`: a
callable registered on `AudioRecorder` through a new `add_tap(fn)` /
`remove_tap(fn)` pair, called from the audio callback with each chunk
(resampled to 16 kHz when the device runs at another rate). It drops chunks
while `state_manager.is_transcription_recording()` or exclude-me is on.
`Chunker`: one per stream, feeds a ten-vad detector; it closes a chunk at a
pause of 0.6 s or more, at 15 s, or when the stream stops, and discards
chunks with under 0.3 s of speech. Chunks carry the stream name and
wall-clock start and end.

**`meeting/transcriber.py`: MeetingTranscriber.** One daemon worker and a
queue. Before each chunk it waits while the state manager is recording or
processing (`is_idle()` false), polling at 100 ms, then calls
`whisper_engine.transcribe_audio(audio, language=...)` with the meeting
language setting (`auto` by default) and the active profile's hotwords. The
engine's own thread already serialises calls. The result becomes a turn
`{start, end, stream, speaker, text, lang}`; empty results are dropped. The
queue depth in seconds is published as `behind_s` on the meeting state.
`meeting.model`, when set, swaps the engine the same way the model picker
does before the meeting's first chunk and back after it ends; the Settings
help says this only makes sense on a GPU.

**`meeting/speakers.py`: SpeakerTracker.** Wraps
`sherpa_onnx.SpeakerEmbeddingExtractor` with a model registered in
`config.defaults.yaml` under `meeting.speaker_models` (3D-Speaker ERes2Net,
about 25 MB, from the sherpa-onnx releases) and fetched through
`ModelRegistry` like the streaming models. `label(audio) -> speaker_id`
computes the embedding, cosine-compares it with each known speaker's running
centroid, joins the best match above `SIMILARITY` (0.55 to start; tuned on
real calls), otherwise creates `Speaker N`. Centroids are kept in the meeting
file so a resumed meeting keeps its speakers. "You" is never computed: mic
turns are labelled by the transcriber. Pure numpy apart from the extractor,
so the clustering is unit-tested with synthetic vectors.

**`meeting/store.py`.** `<data>/meetings/<yyyy-mm-dd-hhmmss>.json`, one per
meeting, written on every change but at most every 5 s (a dirty flag and a
timer), and once more on end. `list()` reads only the header of each file.
`load(id)`, `save(meeting)`, `delete(id)`, `export(id, fmt)` for `txt` and
`md` (speaker-tagged lines, like Read.ai's text view).

```json
{
  "id": "2026-10-11-143012",
  "title": "Discord: Team sync",
  "source": {"app": "discord.exe", "kind": "native", "title": "", "code": ""},
  "started_at": "2026-10-11T14:30:12+03:00",
  "ended_at": "2026-10-11T15:12:40+03:00",
  "language": "auto",
  "speakers": [
    {"id": "you", "name": "You", "colour": 0},
    {"id": "s1", "name": "Ilona", "colour": 1, "centroid": [0.01, "..."], "count": 42}
  ],
  "turns": [
    {"start": 3.2, "end": 7.9, "stream": "loopback", "speaker": "s1", "text": "...", "lang": "en"}
  ],
  "gaps": [{"start": 600.0, "end": 660.0, "reason": "excluded"}]
}
```
Times in `turns` and `gaps` are seconds from `started_at`.

**Platform modules** (mirrored; macOS stubs return empty lists or do nothing):
- `platform/windows/audio_sessions.py`: `active() -> [{pid, exe, state}]`
  through pycaw's session enumerator; `state` is active, inactive or expired.
- `platform/windows/browser_tabs.py`: `tabs() -> [{exe, title, media}]` for
  every top-level Chrome, Edge or Brave window, through UI Automation's tab
  strip (control type TabItem under the window), with a 3 s cache. Runs on
  the detector thread with its own COM client, the way `input_watch` does.
- `ui/win32.py`: `exclude_from_capture(hwnd, on)` via
  `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`. No-op elsewhere.

**UI host.** A third `_PageChannel('meeting', MEETING_TOPICS)` and window:
`meeting.html`, `frameless=True, on_top=True, focus=False, resizable=False,
shadow=False, hidden=True`, made a tool window and no-activate like the bar,
opacity from `meeting.panel.opacity`, excluded from capture per setting.
Placement reuses `bar_origin` with the panel's own position setting; a drag
on its header uses the bar's drag path with a `which` argument; a corner
handle resizes through `meeting_resize(w, h)`. The page reports its size
when collapsed so the pill fits.

**Panel page (`meeting.html`, `js/meeting.js`, `css/meeting.css`).** Preact +
htm like the bar. States: `offered`, `live`, `paused`, `ended` (brief), and
the `excluded`, `resumed` or `new` notices and `behind_s` inside `live`.
Shows the last turns that fit, newest at the bottom, auto-scroll unless the
user scrolled up, a dim placeholder for the chunk in flight, speaker colours
from a fixed palette of 8 on the dark theme, "You" always colour 0. Buttons:
Exclude me, Pause or Resume, New meeting, open in app, collapse, end, and a
header menu with "Hide from screen sharing" and "Rename speakers".

**Meetings screen (`js/screens/meetings.js`).** Left: the live meeting on top
(same controls as the panel), past meetings below (date, title, source,
duration, speakers). Right: the transcript with speaker colours, click a name
to rename, Export (txt or md), Delete. Added to the app's screen list and
search.

**Services and bridge.** `services/meetings.py` wraps the session manager
and store: `list`, `get`, `start`, `end`, `new`, `pause`, `resume`,
`set_excluded`, `rename_speaker`, `export`, `delete`,
`set_hidden_from_sharing`. Bridge methods `meeting_*` call it. The bridge's
`rules_*` already edit app rules; the Apps screen gains a "Meeting" control
(auto, ask, off, inherit) on each rule.

**Events.** `meeting` `{state, id, title, source, elapsed_s, paused,
excluded, behind_s, speakers, notice}` and `meeting_turn` `{id, turn}` (and
`{id, pending: {stream, speaker}}` for the placeholder) are added to
`events.TOPICS`, to the panel's topic set, and to the main window.

**Wiring.** `main.py` builds `MeetingService` after the state manager and
engine exist and hands it to the host; the service starts the detector when
`meeting.enabled` is on and the UI host is running. Tray: "Start meeting" and
"End meeting". Hotkey `meeting_toggle`, unbound by default, in the hotkeys
section.

## Settings

New section `meeting:` in `config.defaults.yaml`, schema section
`meetings` ("Meetings: calls in Discord, Zoom, Teams and the browser"):

| Key | Default | Control | Apply |
|-----|---------|---------|-------|
| `meeting.enabled` | true | toggle | live |
| `meeting.detect` | auto | segmented auto / ask / off | live |
| `meeting.include_me` | true | toggle | live |
| `meeting.gap_minutes` | 10 | number 1 to 60 | live |
| `meeting.start_speech_seconds` | 3 | number 1 to 10 | live |
| `meeting.language` | auto | select (auto, he, en, ...) | next chunk |
| `meeting.model` | "" (dictation model) | select from models | background |
| `meeting.speaker_model` | eres2net | select | background |
| `meeting.speaker_models` | table | model_table | internal |
| `meeting.panel.opacity` | 0.85 | range 0.4 to 1 | live |
| `meeting.panel.position` | right-edge | select (bubble positions) | live |
| `meeting.panel.custom` | null | internal | live |
| `meeting.panel.width` / `height` | 380 / 420 | number | live |
| `meeting.panel.collapsed` | false | internal | live |
| `meeting.panel.hide_from_sharing` | true | toggle | live |
| `overlay.hide_from_sharing` | false | toggle | live |
| `hotkeys.meeting_toggle` | "" | hotkey | live |

App rules gain `meeting: auto | ask | off` (absent means inherit
`meeting.detect`). Shipped rules in `app_rules.defaults.yaml`: `discord.exe`,
`zoom.exe`, `ms-teams.exe` and `teams.exe`, `slack.exe` (native); browser
rules with `match: [chrome.exe, msedge.exe, brave.exe]` and `window_title:
["Meet - "]`, `["Microsoft Teams"]`, `["Zoom Meeting", "Zoom Workplace"]`,
`["Discord"]`. For meeting detection, `window_title` is matched against tab
titles, not the window title. The Apps screen shows the control.

Simple mode gets one question, "Transcribe my calls automatically?", mapping
to `meeting.detect`.

## Out of scope (later steps)

LLM questions during a call, summaries and notes; learning voices across
meetings; reading speaker names from the meeting app; per-app loopback
(capturing only the meeting app's audio); Firefox tab detection; macOS
loopback (needs a virtual audio device); a slower second pass that
re-clusters speakers and cleans text; search across meetings.

## Found while building (2026-10-11)

- The panel's dragged spot is stored as `meeting.panel.custom_x` /
  `custom_y`, like the bubble's, not as one `panel.custom` key.
- `meeting.model` is a text field (a model key), not a picker: the Settings
  page has no generic model picker outside the Model screen.
- Rules that carry only a `meeting` key are skipped by delivery matching
  (`app_rules.is_meeting_only`), so the shipped "discord.exe holds calls"
  rule never claims a Discord window from the user's own chat rule.
- The voiceprint model file is 40 MB, not 26.
- Two meetings started in the same second get ids ending `-2`, `-3`.
- The bundled PortAudio (19.7.0) lists no loopback devices, which is why the
  loopback stream uses `soundcard`; the macOS stubs are loaded by path in the
  tests because the macOS platform package needs `fcntl`.
- The browser mock (`?mock=1&meeting`) drives a fake live meeting for the
  panel and the Meetings screen.
- `meeting.model` exists in the defaults but is hidden: swapping the engine
  for meetings is not wired yet.
- Without the loopback package, meeting mode stays off: the detector does
  not start and a manual start reports the problem (with the Install row on
  the Meetings screen) instead of running mic-only.
- Speaker names are renamed on the Meetings screen; the panel never takes
  keyboard focus, so it cannot host a text field.
- An idle listener (a loopback stream with a speech-only chunker) runs
  while a source waits for speech; the meeting's own streams replace it.
- Chunks carry their meeting's id, so a turn transcribed after its meeting
  ended still lands in that meeting's file; ending a meeting drops nothing.
- Capture callbacks run outside the session lock (queued as effects), and
  the chunker's callbacks outside its own lock.

## Testing

Unit tests (no hardware, `tests/test_meeting.py`):
- SessionManager: auto start after speech; ask flow; pause on source gone;
  resume within the gap with the same key and code; new on a different key,
  a different code, or after the gap; end after the gap; manual start, end,
  new; exclude-me gaps.
- Chunker: cuts at pauses, at 15 s, on stop; drops short speech; wall-clock
  times.
- SpeakerTracker clustering with synthetic unit vectors: same speaker joins,
  far vector creates a new one, centroid moves, resume restores centroids.
- Detector decisions from fake session and tab lists: native match, browser
  tab match behind other tabs, Meet code parsing, media-suffix stripping,
  `off` ignored.
- Store: write, list headers, load, export formats, delete.
- Schema has an entry for every new key (the existing schema test).
- Transcriber: waits while a fake state manager is busy, labels mic turns
  "You", drops empty text, publishes `behind_s`.

Real tests (the user): a Discord call; a Meet in Chrome with the tab behind
others; a screen share with the panel hidden; dictating mid-meeting with
Exclude me on and off; a reconnect within 10 minutes resumes; a second call
after lunch starts new.
