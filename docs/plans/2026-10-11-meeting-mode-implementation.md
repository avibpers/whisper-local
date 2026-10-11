# Meeting Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Notice a call in Discord, Zoom, Teams or a browser tab, transcribe the speakers and the microphone as it happens, label who said what, and show it in a dark translucent panel while saving the meeting for later.

**Architecture:** A detector thread turns per-process audio sessions and browser tab names into "sources"; a pure session manager turns sources into one meeting at a time (offered, live, paused, ended) with the resume-or-new rule; two capture streams (loopback through `soundcard`, a tap on the existing microphone recorder) are cut into chunks by ten-vad and transcribed by the shared Whisper engine on a low-priority worker; loopback chunks get a speaker label from a sherpa-onnx embedding clustered as it arrives. A third pywebview window draws the panel; a Meetings screen in the app window lists saved meetings.

**Tech Stack:** Python 3.11+, numpy, ten-vad, faster-whisper (existing engine), sherpa-onnx `SpeakerEmbeddingExtractor`, `soundcard` (optional extra, WASAPI loopback), pycaw (audio sessions), comtypes UI Automation (tab strip), pywebview/WebView2, Preact + htm.

**Spec:** `docs/design/meeting-mode.md`

**Worktree:** `S:\Koli-meeting-mode`, branch `feat/meeting-mode`. Run every command with `git -C S:/Koli-meeting-mode ...` and absolute paths. Tests:

```
cd S:/Koli-meeting-mode && PYTHONUTF8=1 PYTHONPATH=S:/Koli-meeting-mode/src S:/Koli/.venv/Scripts/python.exe -m unittest tests.test_meeting -v
```

and the whole suite with `-m unittest discover -s tests -q` (498 tests pass at the start; `PYTHONUTF8=1` is required or seven tests fail on console encoding).

## Global Constraints

- Every module starts with a 2–4 line header comment; non-trivial functions get a one-line "why" comment; related code is grouped under `# ── Section ──` headings (CLAUDE.md).
- No backward compatibility shims; break old formats freely.
- Platform modules are mirrored: every new `platform/windows/x.py` gets a `platform/macos/x.py` with the same functions as no-ops, and both are imported in `platform/__init__.py`.
- `soundcard` stays an optional extra (`pip install koli[loopback]`); the base install must import every new module without it.
- Every new key in `config.defaults.yaml` needs a `settings/schema.py` entry (the existing `SettingsSchemaTests` fail otherwise). The `hotkey` config section is singular: `hotkey.meeting_hotkey`.
- The English-only streaming recogniser is never used for meetings.
- Event topics are registered in `ui/events.py` `TOPICS`.
- Speaker "You" has id `you` and colour 0; other speakers get colours 1–7.
- Times inside a meeting file are seconds from `started_at`; `started_at` is an ISO timestamp with timezone.
- The panel never takes focus (tool window + no-activate, like the bubble).
- Commit after every task with a conventional message and the Claude co-author trailer.

## Review Focus

1. A Discord notification ping (a short audio session, no speech) must not start a meeting. Pinned by `SessionTests.test_source_without_speech_does_not_start` (Task 2).
2. A Google Meet tab open in Chrome with no call running (no active audio session) must not become a source. Pinned by `DetectorTests.test_browser_tab_without_audio_session_is_not_a_source` (Task 3).
3. The loopback device disappearing mid-meeting (headset unplugged) must report a problem and keep the meeting live with the microphone. Pinned by `CaptureTests.test_loopback_stream_reports_after_retries` (Task 4).
4. Whisper returning nothing for a chunk (silence, music) must produce no turn and no "Speaker ?" placeholder left behind. Pinned by `TranscriberTests.test_empty_transcription_makes_no_turn` (Task 6).
5. A source coming back exactly after the gap timer fired must start a new meeting, and the previous one must already be saved and ended. Pinned by `SessionTests.test_source_back_after_gap_starts_new_meeting` (Task 2).

---

## File Structure

New:
- `src/koli/meeting/__init__.py` — package marker (empty docstring line).
- `src/koli/meeting/store.py` — meeting files: new, save (throttled), load, list, delete, export.
- `src/koli/meeting/session.py` — the state machine (`SessionManager`), pure.
- `src/koli/meeting/detector.py` — `find_sources()` (pure) and `MeetingDetector` (thread).
- `src/koli/meeting/capture.py` — `Chunker` (pure), `LoopbackStream`, `MicTap`, `speech_prob_function()`.
- `src/koli/meeting/speakers.py` — `SpeakerTracker` (pure clustering), `make_embedder()`.
- `src/koli/meeting/transcriber.py` — `MeetingTranscriber` worker.
- `src/koli/services/meetings.py` — `MeetingService`: composes everything; what the bridge, tray and hotkey call.
- `src/koli/platform/windows/audio_sessions.py`, `src/koli/platform/macos/audio_sessions.py`
- `src/koli/platform/windows/browser_tabs.py`, `src/koli/platform/macos/browser_tabs.py`
- `src/koli/ui/web/meeting.html`, `src/koli/ui/web/js/meeting.js`, `src/koli/ui/web/css/meeting.css`
- `src/koli/ui/web/js/screens/meetings.js`
- `tests/test_meeting.py`

Modified:
- `src/koli/audio_recorder.py` — `add_tap` / `remove_tap`, taps called from `_audio_callback`.
- `src/koli/state_manager.py` — taps survive `rebuild_audio_recorder`; `toggle_meeting()`; `meetings` attribute.
- `src/koli/whisper_engine.py`, `src/koli/whisper_engine_cpp.py` — `transcribe_audio(audio, language=None)`.
- `src/koli/model_registry.py` — speaker models.
- `src/koli/app_rules.py`, `src/koli/services/rules.py`, `src/koli/app_rules.defaults.yaml` — `meeting` key.
- `src/koli/config.defaults.yaml`, `src/koli/settings/schema.py`, `src/koli/settings/appliers.py`, `src/koli/settings/simple_mode.py`
- `src/koli/hotkey_listener.py`, `src/koli/main.py`, `src/koli/system_tray.py`, `src/koli/services/system.py`
- `src/koli/ui/events.py`, `src/koli/ui/host.py`, `src/koli/ui/bridge.py`, `src/koli/ui/win32.py`
- `src/koli/ui/web/js/app.js`, `src/koli/ui/web/js/ui.js`, `src/koli/ui/web/js/screens/apps.js`, `src/koli/ui/web/js/screens/simple.js`, `src/koli/ui/web/js/mock-api.js`, `src/koli/ui/web/js/mock-data.json` (regenerated)
- `CHANGELOG.md`, `README.md`, `docs/project-index.md`, `docs/roadmap/roadmap.md`, `docs/roadmap/completed.md`, `pyproject.toml` (package data already covers `ui/web/**/*`; add `meeting` package automatically via `find`).

---

### Task 1: Meeting store

**Files:**
- Create: `src/koli/meeting/__init__.py`, `src/koli/meeting/store.py`
- Test: `tests/test_meeting.py`

**Interfaces:**
- Produces: `new_meeting(meeting_id, title, source, started_at, language='auto') -> dict`; `new_id(now) -> str`; `header(meeting) -> dict`; `export_text(meeting, fmt) -> str`; `class MeetingStore(folder=None)` with `path(id)`, `save(meeting) -> Path`, `load(id) -> dict`, `list() -> list`, `delete(id) -> bool`, `export(id, fmt) -> str`; `class ThrottledSaver(store, interval=5.0)` with `mark(meeting)`, `flush()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_meeting.py`:

```python
# tests/test_meeting.py
# Meeting mode: the store, the session state machine, detection, chunking,
# speaker clustering and the transcriber, all without audio hardware.
import datetime
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

TZ = datetime.timezone(datetime.timedelta(hours=3))


def at(minute, second=0):
    return datetime.datetime(2026, 10, 11, 14, minute, second, tzinfo=TZ)


class StoreTests(unittest.TestCase):
    def setUp(self):
        from koli.meeting.store import MeetingStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MeetingStore(Path(self.tmp.name))

    def test_new_meeting_has_you_and_no_turns(self):
        from koli.meeting.store import new_id, new_meeting
        m = new_meeting(new_id(at(30, 12)), 'Discord', {'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': ''}, at(30, 12))
        self.assertEqual(m['id'], '2026-10-11-143012')
        self.assertEqual(m['speakers'], [{'id': 'you', 'name': 'You', 'colour': 0}])
        self.assertEqual(m['turns'], [])
        self.assertEqual(m['started_at'], '2026-10-11T14:30:12+03:00')
        self.assertIsNone(m['ended_at'])

    def test_save_load_list_delete(self):
        from koli.meeting.store import new_meeting
        m = new_meeting('2026-10-11-143012', 'Discord', {'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': ''}, at(30, 12))
        m['turns'].append({'start': 3.2, 'end': 7.9, 'stream': 'loopback', 'speaker': 's1', 'text': 'hello', 'lang': 'en'})
        m['speakers'].append({'id': 's1', 'name': 'Speaker 1', 'colour': 1, 'centroid': [1.0, 0.0], 'count': 1})
        self.store.save(m)
        self.assertEqual(self.store.load(m['id'])['turns'][0]['text'], 'hello')
        heads = self.store.list()
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0]['id'], m['id'])
        self.assertEqual(heads[0]['turns'], 1)
        self.assertEqual(heads[0]['speakers'], ['You', 'Speaker 1'])
        self.assertNotIn('centroid', str(heads[0]))
        self.assertTrue(self.store.delete(m['id']))
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.store.delete(m['id']))

    def test_list_is_newest_first_and_skips_broken_files(self):
        from koli.meeting.store import new_meeting
        for mid in ('2026-10-10-090000', '2026-10-11-090000'):
            self.store.save(new_meeting(mid, 'x', {'app': 'zoom.exe', 'kind': 'native', 'title': '', 'code': ''}, at(0)))
        (Path(self.tmp.name) / '2026-10-12-090000.json').write_text('{not json', encoding='utf-8')
        self.assertEqual([h['id'] for h in self.store.list()], ['2026-10-11-090000', '2026-10-10-090000'])

    def test_export_formats(self):
        from koli.meeting.store import export_text, new_meeting
        m = new_meeting('2026-10-11-143012', 'Team sync', {'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': ''}, at(30, 12))
        m['speakers'].append({'id': 's1', 'name': 'Ilona', 'colour': 1})
        m['turns'] += [{'start': 3.0, 'end': 5.0, 'stream': 'loopback', 'speaker': 's1', 'text': 'Hi', 'lang': 'en'},
                       {'start': 6.0, 'end': 8.0, 'stream': 'mic', 'speaker': 'you', 'text': 'Hello', 'lang': 'en'}]
        txt = export_text(m, 'txt')
        self.assertIn('[00:03] Ilona: Hi', txt)
        self.assertIn('[00:06] You: Hello', txt)
        md = export_text(m, 'md')
        self.assertTrue(md.startswith('# Team sync'))
        self.assertIn('**Ilona**', md)
        with self.assertRaises(ValueError):
            export_text(m, 'pdf')

    def test_throttled_saver_writes_once_until_flushed(self):
        from koli.meeting.store import ThrottledSaver, new_meeting
        m = new_meeting('2026-10-11-143012', 'x', {'app': 'zoom.exe', 'kind': 'native', 'title': '', 'code': ''}, at(0))
        saver = ThrottledSaver(self.store, interval=60.0)
        saver.mark(m)
        self.assertTrue(self.store.path(m['id']).exists())   # the first mark writes at once
        m['turns'].append({'start': 1.0, 'end': 2.0, 'stream': 'mic', 'speaker': 'you', 'text': 'a', 'lang': 'en'})
        saver.mark(m)
        self.assertEqual(self.store.load(m['id'])['turns'], [])   # the next one waits
        saver.flush()
        self.assertEqual(len(self.store.load(m['id'])['turns']), 1)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd S:/Koli-meeting-mode && PYTHONUTF8=1 PYTHONPATH=S:/Koli-meeting-mode/src S:/Koli/.venv/Scripts/python.exe -m unittest tests.test_meeting.StoreTests -v`
Expected: errors with `ModuleNotFoundError: No module named 'koli.meeting'`.

- [ ] **Step 3: Write the store**

`src/koli/meeting/__init__.py`:

```python
# meeting/__init__.py
# Meeting mode: detect a call, transcribe both sides, label the speakers, keep
# the meeting. See docs/design/meeting-mode.md.
```

`src/koli/meeting/store.py`:

```python
# meeting/store.py
# One JSON file per meeting under <data>/meetings/: who spoke, what they said
# and when, plus the voiceprints that keep speaker labels stable when a
# meeting resumes. Written throttled while a meeting runs, so a crash loses
# seconds, not the meeting.

import datetime
import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from ..utils import get_user_app_data_path

logger = logging.getLogger(__name__)

FOLDER = 'meetings'
SAVE_INTERVAL = 5.0
EXPORT_FORMATS = ('txt', 'md')


# ── Shapes ───────────────────────────────────────────────────────────────────

def meetings_dir() -> Path:
    return Path(get_user_app_data_path()) / FOLDER


def new_id(now: datetime.datetime) -> str:
    return now.strftime('%Y-%m-%d-%H%M%S')


# `source` is {'app', 'kind', 'title', 'code'} as the detector reports it.
def new_meeting(meeting_id: str, title: str, source: dict, started_at: datetime.datetime, language: str = 'auto') -> dict:
    return {
        'id': meeting_id,
        'title': title,
        'source': dict(source),
        'started_at': started_at.isoformat(timespec='seconds'),
        'ended_at': None,
        'language': language,
        'speakers': [{'id': 'you', 'name': 'You', 'colour': 0}],
        'turns': [],
        'gaps': [],
    }


def started_ts(meeting: dict) -> float:
    return datetime.datetime.fromisoformat(meeting['started_at']).timestamp()


# How long the meeting ran: to its end, or to its last turn while it is open.
def duration_s(meeting: dict) -> float:
    if meeting.get('ended_at'):
        return datetime.datetime.fromisoformat(meeting['ended_at']).timestamp() - started_ts(meeting)
    return float(meeting['turns'][-1]['end']) if meeting['turns'] else 0.0


# What the list shows: never the turns or the voiceprints.
def header(meeting: dict) -> dict:
    return {
        'id': meeting['id'], 'title': meeting['title'], 'source': meeting['source'],
        'started_at': meeting['started_at'], 'ended_at': meeting.get('ended_at'),
        'duration_s': round(duration_s(meeting)),
        'speakers': [s['name'] for s in meeting['speakers']],
        'turns': len(meeting['turns']),
    }


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def export_text(meeting: dict, fmt: str) -> str:
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"Unknown export format: {fmt}")
    names = {s['id']: s['name'] for s in meeting['speakers']}
    lines = []
    if fmt == 'md':
        lines += [f"# {meeting['title']}", '', f"{meeting['started_at'][:16].replace('T', ' ')} · {_clock(duration_s(meeting))}", '']
    else:
        lines += [meeting['title'], meeting['started_at'][:16].replace('T', ' '), '']
    for turn in meeting['turns']:
        who = names.get(turn['speaker'], turn['speaker'])
        who = f"**{who}**" if fmt == 'md' else who
        lines.append(f"[{_clock(turn['start'])}] {who}: {turn['text']}")
    return '\n'.join(lines) + '\n'


# ── Files ────────────────────────────────────────────────────────────────────

class MeetingStore:
    def __init__(self, folder: Optional[Path] = None):
        self.folder = Path(folder) if folder else meetings_dir()
        self._lock = threading.Lock()

    def path(self, meeting_id: str) -> Path:
        return self.folder / f"{meeting_id}.json"

    # Written to a temp file first, so a crash mid-write never leaves half a
    # meeting behind.
    def save(self, meeting: dict) -> Path:
        path = self.path(meeting['id'])
        with self._lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(meeting, ensure_ascii=False, indent=1), encoding='utf-8')
            os.replace(tmp, path)
        return path

    def load(self, meeting_id: str) -> dict:
        with self._lock:
            return json.loads(self.path(meeting_id).read_text(encoding='utf-8'))

    # Newest first. A file that can't be read is skipped, not fatal.
    def list(self) -> list:
        out = []
        with self._lock:
            files = sorted(self.folder.glob('*.json'), reverse=True) if self.folder.exists() else []
            for path in files:
                try:
                    out.append(header(json.loads(path.read_text(encoding='utf-8'))))
                except Exception as e:
                    logger.warning(f"Skipping unreadable meeting {path.name}: {e}")
        return out

    def delete(self, meeting_id: str) -> bool:
        with self._lock:
            try:
                self.path(meeting_id).unlink()
                return True
            except FileNotFoundError:
                return False

    def export(self, meeting_id: str, fmt: str) -> str:
        return export_text(self.load(meeting_id), fmt)


# The first mark writes at once (so the file exists from the start); later
# marks wait until `interval` has passed since the last write, then write the
# newest version. flush() writes now, for the end of a meeting.
class ThrottledSaver:
    def __init__(self, store: MeetingStore, interval: float = SAVE_INTERVAL):
        self.store = store
        self.interval = interval
        self._lock = threading.Lock()
        self._pending = None
        self._timer = None
        self._last_write = 0.0

    def mark(self, meeting: dict) -> None:
        import time
        with self._lock:
            self._pending = meeting
            due = self._last_write + self.interval - time.monotonic()
            if due <= 0:
                self._write_locked()
                return
            if self._timer is None:
                self._timer = threading.Timer(due, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._pending is not None:
                self._write_locked()

    def _write_locked(self) -> None:
        import time
        meeting, self._pending = self._pending, None
        try:
            self.store.save(meeting)
        except Exception as e:
            logger.error(f"Could not save meeting {meeting.get('id')}: {e}")
        self._last_write = time.monotonic()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: the same command. Expected: `OK` for the five `StoreTests`.

- [ ] **Step 5: Commit**

```bash
git -C S:/Koli-meeting-mode add src/koli/meeting/__init__.py src/koli/meeting/store.py tests/test_meeting.py
git -C S:/Koli-meeting-mode commit -m "feat(meeting): meeting store with throttled saves and txt/md export

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Session state machine

**Files:**
- Create: `src/koli/meeting/session.py`
- Test: `tests/test_meeting.py`

**Interfaces:**
- Consumes: Task 1 (`MeetingStore`, `ThrottledSaver`, `new_meeting`, `new_id`, `started_ts`, `header`).
- Produces: `class SessionManager(store, *, start_capture, stop_capture, publish, settings, now=None, schedule=None)`; commands `on_sources(sources)`, `on_speech(seconds)`, `start(source=None)`, `offer_accept()`, `offer_decline()`, `end()`, `new()`, `pause()`, `resume()`, `set_excluded(on)`, `rename_speaker(speaker_id, name)`, `add_turn(turn)`, `ensure_speaker(speaker)`, `continue_previous()`, `split_resumed()`, `set_behind(seconds)`; queries `state_payload() -> dict`, `current() -> dict|None`. Sources are `{'key', 'app', 'kind', 'title', 'code', 'mode'}` with mode `auto|ask`. `settings(path, default)` reads `meeting.gap_minutes`, `meeting.start_speech_seconds`, `meeting.language`, `meeting.include_me`. `schedule(seconds, fn) -> cancel()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meeting.py` (above `if __name__`):

```python
class FakeClock:
    def __init__(self, start):
        self.t = start
        self.timers = []   # (due, fn, alive)

    def now(self):
        return self.t

    def schedule(self, seconds, fn):
        entry = [self.t + datetime.timedelta(seconds=seconds), fn, True]
        self.timers.append(entry)
        return lambda: entry.__setitem__(2, False)

    def advance(self, seconds):
        self.t += datetime.timedelta(seconds=seconds)
        for entry in list(self.timers):
            if entry[2] and entry[0] <= self.t:
                entry[2] = False
                entry[1]()


def discord(mode='auto'):
    return {'key': 'discord.exe', 'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': '', 'mode': mode}


def meet(code='abc-defg-hij', mode='auto'):
    return {'key': f'chrome.exe|Meet - {code}', 'app': 'chrome.exe', 'kind': 'browser', 'title': f'Meet - {code}', 'code': code, 'mode': mode}


class SessionTests(unittest.TestCase):
    def setUp(self):
        from koli.meeting.session import SessionManager
        from koli.meeting.store import MeetingStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MeetingStore(Path(self.tmp.name))
        self.clock = FakeClock(at(30))
        self.captures = []
        self.events = []
        self.settings = {'meeting.gap_minutes': 10, 'meeting.start_speech_seconds': 3, 'meeting.language': 'auto', 'meeting.include_me': True}
        self.sm = SessionManager(self.store, start_capture=lambda m: self.captures.append(('start', m['id'])),
                                 stop_capture=lambda: self.captures.append(('stop',)),
                                 publish=lambda topic, payload: self.events.append((topic, payload)),
                                 settings=lambda path, default=None: self.settings.get(path, default),
                                 now=self.clock.now, schedule=self.clock.schedule)

    def live(self, source=None):
        source = source or discord()
        self.sm.on_sources([source])
        self.sm.on_speech(3.5)
        self.assertEqual(self.sm.state, 'live')
        return self.sm.current()

    def test_source_without_speech_does_not_start(self):
        self.sm.on_sources([discord()])
        self.sm.on_speech(1.0)
        self.assertEqual(self.sm.state, 'idle')
        self.assertEqual(self.captures, [])
        self.sm.on_sources([])          # the ping ended
        self.sm.on_sources([discord()])
        self.sm.on_speech(1.0)          # speech counts from this appearance only
        self.assertEqual(self.sm.state, 'idle')
        self.sm.on_speech(2.5)
        self.assertEqual(self.sm.state, 'live')
        self.assertEqual(self.captures, [('start', self.sm.current()['id'])])

    def test_auto_start_names_and_saves_the_meeting(self):
        m = self.live()
        self.assertEqual(m['title'], 'Discord')
        self.assertEqual(m['source']['app'], 'discord.exe')
        self.assertTrue(self.store.path(m['id']).exists())
        self.assertEqual(self.events[-1][0], 'meeting')
        self.assertEqual(self.events[-1][1]['state'], 'live')

    def test_ask_mode_offers_then_starts_on_accept(self):
        self.sm.on_sources([discord(mode='ask')])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'offered')
        self.assertEqual(self.captures, [])
        self.sm.offer_accept()
        self.assertEqual(self.sm.state, 'live')

    def test_declined_offer_is_not_asked_again_until_source_goes_away(self):
        self.sm.on_sources([discord(mode='ask')])
        self.sm.on_speech(4.0)
        self.sm.offer_decline()
        self.assertEqual(self.sm.state, 'idle')
        self.sm.on_sources([discord(mode='ask')])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'idle')
        self.sm.on_sources([])
        self.sm.on_sources([discord(mode='ask')])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'offered')

    def test_source_gone_pauses_and_same_source_resumes(self):
        m = self.live()
        self.sm.add_turn({'t0': started_at(m) + 5, 't1': started_at(m) + 8, 'stream': 'loopback', 'speaker': 's1', 'text': 'hi', 'lang': 'en'})
        self.sm.on_sources([])
        self.assertEqual(self.sm.state, 'paused')
        self.assertEqual(self.captures[-1], ('stop',))
        self.clock.advance(120)
        self.sm.on_sources([discord()])
        self.assertEqual(self.sm.state, 'live')
        self.assertEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.state_payload()['notice'], 'resumed')
        self.assertEqual(self.sm.current()['gaps'], [{'start': 0.0, 'end': 120.0, 'reason': 'paused'}])

    def test_source_back_after_gap_starts_new_meeting(self):
        m = self.live()
        self.sm.on_sources([])
        self.clock.advance(10 * 60 + 1)
        self.assertEqual(self.sm.state, 'idle')
        saved = self.store.load(m['id'])
        self.assertIsNotNone(saved['ended_at'])
        self.clock.advance(5)
        self.sm.on_sources([discord()])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'live')
        self.assertNotEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.state_payload()['notice'], 'new')

    def test_different_code_within_gap_starts_new_and_can_continue_previous(self):
        m = self.live(meet('abc-defg-hij'))
        self.sm.on_sources([])
        self.clock.advance(60)
        self.sm.on_sources([meet('zzz-zzzz-zzz')])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'live')
        self.assertNotEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.state_payload()['notice'], 'new')
        self.assertTrue(self.sm.state_payload()['can_continue'])
        self.sm.add_turn({'t0': started_at(self.sm.current()) + 1, 't1': started_at(self.sm.current()) + 2, 'stream': 'mic', 'speaker': 'you', 'text': 'late', 'lang': 'en'})
        new_id = self.sm.current()['id']
        self.sm.continue_previous()
        self.assertEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.current()['turns'][-1]['text'], 'late')
        self.assertFalse(self.store.path(new_id).exists())

    def test_same_code_resumes_even_from_another_browser_window(self):
        m = self.live(meet('abc-defg-hij'))
        self.sm.on_sources([])
        self.clock.advance(30)
        self.sm.on_sources([meet('abc-defg-hij')])
        self.assertEqual(self.sm.current()['id'], m['id'])

    def test_resumed_meeting_can_be_split_into_a_new_one(self):
        m = self.live()
        base = started_at(m)
        self.sm.add_turn({'t0': base + 1, 't1': base + 2, 'stream': 'mic', 'speaker': 'you', 'text': 'first', 'lang': 'en'})
        self.sm.on_sources([])
        self.clock.advance(60)
        self.sm.on_sources([discord()])
        self.sm.add_turn({'t0': base + 70, 't1': base + 72, 'stream': 'mic', 'speaker': 'you', 'text': 'second', 'lang': 'en'})
        self.sm.split_resumed()
        self.assertNotEqual(self.sm.current()['id'], m['id'])
        self.assertEqual([t['text'] for t in self.sm.current()['turns']], ['second'])
        self.assertEqual([t['text'] for t in self.store.load(m['id'])['turns']], ['first'])
        self.assertIsNotNone(self.store.load(m['id'])['ended_at'])

    def test_manual_start_without_source_and_end(self):
        self.sm.start()
        self.assertEqual(self.sm.state, 'live')
        self.assertEqual(self.sm.current()['source']['kind'], 'manual')
        self.assertEqual(self.sm.current()['title'], 'Meeting')
        self.sm.end()
        self.assertEqual(self.sm.state, 'idle')
        self.assertEqual(self.captures, [('start', self.store.list()[0]['id']), ('stop',)])
        self.assertEqual(self.events[-2][1]['state'], 'ended')
        self.assertEqual(self.events[-1][1]['state'], 'idle')

    def test_new_ends_current_and_starts_fresh_with_same_source(self):
        m = self.live()
        self.sm.new()
        self.assertEqual(self.sm.state, 'live')
        self.assertNotEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.current()['source'], m['source'])
        self.assertIsNotNone(self.store.load(m['id'])['ended_at'])

    def test_pause_and_exclude_record_gaps(self):
        m = self.live()
        self.sm.pause()
        self.assertEqual(self.sm.state, 'paused')
        self.assertTrue(self.sm.state_payload()['paused_by_user'])
        self.clock.advance(10)
        self.sm.resume()
        self.assertEqual(self.sm.state, 'live')
        self.clock.advance(5)
        self.sm.set_excluded(True)
        self.clock.advance(20)
        self.sm.set_excluded(False)
        self.assertEqual(m['gaps'], [{'start': 0.0, 'end': 10.0, 'reason': 'paused'},
                                     {'start': 15.0, 'end': 35.0, 'reason': 'excluded'}])

    def test_user_pause_is_not_resumed_by_the_source(self):
        self.live()
        self.sm.pause()
        self.sm.on_sources([discord()])
        self.assertEqual(self.sm.state, 'paused')

    def test_turns_speakers_and_rename(self):
        m = self.live()
        base = started_at(m)
        self.sm.ensure_speaker({'id': 's1', 'name': 'Speaker 1', 'colour': 1, 'centroid': [1.0], 'count': 1})
        self.sm.add_turn({'t0': base + 3.25, 't1': base + 7.9, 'stream': 'loopback', 'speaker': 's1', 'text': 'hi', 'lang': 'en'})
        self.assertEqual(m['turns'], [{'start': 3.25, 'end': 7.9, 'stream': 'loopback', 'speaker': 's1', 'text': 'hi', 'lang': 'en'}])
        self.assertEqual(self.events[-1], ('meeting_turn', {'id': m['id'], 'turn': m['turns'][0]}))
        self.sm.rename_speaker('s1', 'Ilona')
        self.assertEqual(m['speakers'][1]['name'], 'Ilona')
        self.assertEqual(self.sm.state_payload()['speakers'][1], {'id': 's1', 'name': 'Ilona', 'colour': 1})

    def test_payload_shape(self):
        p = self.sm.state_payload()
        self.assertEqual(p['state'], 'idle')
        self.live()
        self.sm.set_behind(12.5)
        p = self.sm.state_payload()
        for key in ('state', 'id', 'title', 'source', 'started_at', 'elapsed_s', 'paused_by_user', 'excluded', 'behind_s',
                    'speakers', 'notice', 'turns', 'can_continue', 'can_split', 'offer'):
            self.assertIn(key, p)
        self.assertEqual(p['behind_s'], 12.5)


def started_at(meeting):
    from koli.meeting.store import started_ts
    return started_ts(meeting)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... -m unittest tests.test_meeting.SessionTests -v`
Expected: `ModuleNotFoundError: No module named 'koli.meeting.session'`.

- [ ] **Step 3: Write the session manager**

`src/koli/meeting/session.py`:

```python
# meeting/session.py
# When a meeting starts, pauses, resumes and ends. Pure logic: audio, windows
# and files are reached only through the callbacks handed in, so every rule in
# docs/design/meeting-mode.md is tested without hardware. One meeting at a time.

import datetime
import logging
import threading
from typing import Callable, Optional

from .store import MeetingStore, ThrottledSaver, duration_s, header, new_id, new_meeting, started_ts

logger = logging.getLogger(__name__)

STATES = ('idle', 'offered', 'live', 'paused', 'ended')
NOTICE_SECONDS = 6.0

# Friendly names for the apps the shipped rules know.
APP_NAMES = {'discord.exe': 'Discord', 'zoom.exe': 'Zoom', 'ms-teams.exe': 'Teams', 'teams.exe': 'Teams',
             'slack.exe': 'Slack', 'chrome.exe': 'Chrome', 'msedge.exe': 'Edge', 'brave.exe': 'Brave'}


def _default_schedule(seconds: float, fn: Callable) -> Callable[[], None]:
    timer = threading.Timer(seconds, fn)
    timer.daemon = True
    timer.start()
    return timer.cancel


def _now() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


# "Meet - abc-defg-hij" reads as Google Meet; a native app by its name.
def title_for(source: dict) -> str:
    if source.get('kind') == 'manual':
        return 'Meeting'
    title = (source.get('title') or '').strip()
    if title.startswith('Meet - ') or title.startswith('Meet – '):
        return 'Google Meet'
    if title:
        return title[:60]
    return APP_NAMES.get(source.get('app', ''), source.get('app', 'Meeting').replace('.exe', '').title())


class SessionManager:
    def __init__(self, store: MeetingStore, *, start_capture: Callable[[dict], None], stop_capture: Callable[[], None],
                 publish: Callable[[str, dict], None], settings: Callable, now: Callable = None,
                 schedule: Callable = None, save_interval: float = 5.0):
        self.store = store
        self.saver = ThrottledSaver(store, save_interval)
        self._start_capture = start_capture
        self._stop_capture = stop_capture
        self._publish = publish
        self._settings = settings
        self._now = now or _now
        self._schedule = schedule or _default_schedule
        self._lock = threading.RLock()
        self.state = 'idle'
        self.meeting: Optional[dict] = None
        self.excluded = False
        self.paused_by_user = False
        self.behind_s = 0.0
        self.notice = None
        self._cancel_notice = None
        self._sources = []              # what the detector saw last
        self._pending = None            # the source waiting for speech, or offered
        self._speech_s = 0.0            # speech heard since the pending source appeared
        self._declined = set()          # source keys answered "Not now"
        self._paused_at = None          # datetime the current pause began
        self._gap_open = None           # index into meeting['gaps'] of the open gap
        self._cancel_gap_timer = None
        self._previous = None           # the meeting ended by "new" while still inside its gap
        self._previous_until = None
        self._resumed_turn_index = None # turns from here came after the last resume

    # ── Queries ──────────────────────────────────────────────────────────────

    def current(self) -> Optional[dict]:
        return self.meeting

    def _elapsed(self) -> float:
        if not self.meeting:
            return 0.0
        return max(0.0, self._now().timestamp() - started_ts(self.meeting))

    def state_payload(self) -> dict:
        with self._lock:
            m = self.meeting
            return {
                'state': self.state,
                'id': m['id'] if m else None,
                'title': m['title'] if m else '',
                'source': m['source'] if m else (self._pending or {}),
                'started_at': m['started_at'] if m else None,
                'elapsed_s': round(self._elapsed(), 1),
                'paused_by_user': self.paused_by_user,
                'excluded': self.excluded,
                'behind_s': round(self.behind_s, 1),
                'speakers': [{'id': s['id'], 'name': s['name'], 'colour': s['colour']} for s in (m['speakers'] if m else [])],
                'notice': self.notice,
                'turns': len(m['turns']) if m else 0,
                'can_continue': self._previous is not None and self.state == 'live',
                'can_split': self._resumed_turn_index is not None and self.state == 'live',
                'offer': dict(self._pending) if self.state == 'offered' and self._pending else None,
            }

    def _emit(self):
        self._publish('meeting', self.state_payload())

    def _set_notice(self, notice: Optional[str]):
        if self._cancel_notice:
            self._cancel_notice()
            self._cancel_notice = None
        self.notice = notice
        if notice:
            self._cancel_notice = self._schedule(NOTICE_SECONDS, self._clear_notice)

    def _clear_notice(self):
        with self._lock:
            self.notice = None
            self._cancel_notice = None
            self._emit()

    # ── Detection ────────────────────────────────────────────────────────────

    # The detector's view, once a second. Only changes matter.
    def on_sources(self, sources: list):
        with self._lock:
            keys = {s['key'] for s in sources}
            self._declined &= keys
            self._sources = list(sources)
            if self.state in ('live', 'paused'):
                self._sources_while_meeting(sources)
                return
            if self.state == 'offered':
                if self._pending['key'] not in keys:
                    self._pending = None
                    self.state = 'idle'
                    self._emit()
                return
            # idle: pick a source to wait for speech on
            candidates = [s for s in sources if s['key'] not in self._declined and s.get('mode') != 'off']
            if self._pending and self._pending['key'] in keys and any(c['key'] == self._pending['key'] for c in candidates):
                return
            self._pending = candidates[0] if candidates else None
            self._speech_s = 0.0

    def _sources_while_meeting(self, sources: list):
        m = self.meeting
        match = next((s for s in sources if self._same_meeting(m, s)), None)
        if self.state == 'live' and match is None and m['source'].get('kind') != 'manual':
            self._pause(by_user=False)
            return
        if self.state == 'paused' and not self.paused_by_user:
            if match is not None:
                self._resume(notice='resumed')
                return
            other = next((s for s in sources if s.get('mode') != 'off'), None)
            if other is not None:
                # A different call: this one ends, the new one waits for speech.
                self._end()
                self._pending = other
                self._speech_s = 0.0

    @staticmethod
    def _same_meeting(meeting: dict, source: dict) -> bool:
        src = meeting['source']
        if src.get('kind') == 'manual':
            return True
        if src.get('code') or source.get('code'):
            return src.get('code') == source.get('code')
        return src.get('app') == source.get('app') and src.get('title') == source.get('title')

    # Speech heard on the loopback: a pending source starts once enough of it.
    def on_speech(self, seconds: float):
        with self._lock:
            if self.state != 'idle' or not self._pending:
                return
            self._speech_s += float(seconds)
            if self._speech_s < float(self._settings('meeting.start_speech_seconds', 3) or 3):
                return
            source = self._pending
            if source.get('mode') == 'ask':
                self.state = 'offered'
                self._emit()
                return
            self._begin(source, notice=None)

    def offer_accept(self):
        with self._lock:
            if self.state != 'offered' or not self._pending:
                return
            self._begin(self._pending, notice=None)

    def offer_decline(self):
        with self._lock:
            if self.state != 'offered' or not self._pending:
                return
            self._declined.add(self._pending['key'])
            self._pending = None
            self.state = 'idle'
            self._emit()

    # ── Commands ─────────────────────────────────────────────────────────────

    def start(self, source: Optional[dict] = None):
        with self._lock:
            if self.state in ('live', 'paused'):
                return
            source = source or self._pending or (self._sources[0] if self._sources else None) \
                or {'key': 'manual', 'app': '', 'kind': 'manual', 'title': '', 'code': ''}
            self._begin(source, notice=None)

    def end(self):
        with self._lock:
            if self.state not in ('live', 'paused', 'offered'):
                return
            if self.state == 'offered':
                self.offer_decline()
                return
            self._end()

    def new(self):
        with self._lock:
            if self.state not in ('live', 'paused'):
                return
            source = dict(self.meeting['source'])
            self._end(remember=False)
            self._begin(source, notice=None)

    def pause(self):
        with self._lock:
            if self.state == 'live':
                self._pause(by_user=True)

    def resume(self):
        with self._lock:
            if self.state == 'paused':
                self._resume(notice=None)

    def set_excluded(self, on: bool):
        with self._lock:
            on = bool(on)
            if on == self.excluded:
                return
            self.excluded = on
            if self.meeting and self.state == 'live':
                if on:
                    self._open_gap('excluded')
                else:
                    self._close_gap()
            self._emit()

    def set_behind(self, seconds: float):
        with self._lock:
            self.behind_s = float(seconds)

    # ── Turns and speakers ───────────────────────────────────────────────────

    # A turn from the transcriber, with wall-clock t0/t1 (time.time()).
    def add_turn(self, turn: dict):
        with self._lock:
            m = self.meeting
            if not m or self.state not in ('live', 'paused'):
                return
            base = started_ts(m)
            stored = {'start': round(float(turn['t0']) - base, 2), 'end': round(float(turn['t1']) - base, 2),
                      'stream': turn['stream'], 'speaker': turn['speaker'], 'text': turn['text'], 'lang': turn.get('lang', '')}
            m['turns'].append(stored)
            self.saver.mark(m)
            self._publish('meeting_turn', {'id': m['id'], 'turn': stored})

    # Called by the transcriber when it sees a speaker for the first time, or
    # with an updated centroid.
    def ensure_speaker(self, speaker: dict):
        with self._lock:
            m = self.meeting
            if not m:
                return
            for i, s in enumerate(m['speakers']):
                if s['id'] == speaker['id']:
                    m['speakers'][i] = {**s, **{k: v for k, v in speaker.items() if k != 'name'}}
                    self.saver.mark(m)
                    return
            m['speakers'].append(dict(speaker))
            self.saver.mark(m)
            self._emit()

    def rename_speaker(self, speaker_id: str, name: str):
        with self._lock:
            m = self.meeting
            if not m:
                return
            for s in m['speakers']:
                if s['id'] == speaker_id:
                    s['name'] = str(name).strip()[:40] or s['name']
            self.saver.mark(m)
            self._emit()

    # ── Resume or new, by hand ───────────────────────────────────────────────

    # "Continue previous instead": the turns of this new meeting move onto the
    # previous one, which comes back live; the new file goes away.
    def continue_previous(self):
        with self._lock:
            if self.state != 'live' or not self._previous:
                return
            new, prev = self.meeting, self._previous
            offset = started_ts(new) - started_ts(prev)
            for t in new['turns']:
                prev['turns'].append({**t, 'start': round(t['start'] + offset, 2), 'end': round(t['end'] + offset, 2)})
            for s in new['speakers']:
                if not any(p['id'] == s['id'] for p in prev['speakers']):
                    prev['speakers'].append(s)
            prev['ended_at'] = None
            prev['gaps'].append({'start': round(self._previous_paused_s, 2), 'end': round(offset, 2), 'reason': 'paused'})
            self.store.delete(new['id'])
            self.meeting = prev
            self._previous = None
            self._resumed_turn_index = len(prev['turns']) - len(new['turns'])
            self.saver.mark(prev)
            self._set_notice('resumed')
            self._publish('meeting_turn', {'id': prev['id'], 'reload': True})
            self._emit()

    # "Start new instead" after a resume: what came after the resume becomes
    # a meeting of its own; the previous one ends where it paused.
    def split_resumed(self):
        with self._lock:
            if self.state != 'live' or self._resumed_turn_index is None:
                return
            old = self.meeting
            cut = self._resumed_turn_index
            moved, old['turns'] = old['turns'][cut:], old['turns'][:cut]
            gap = old['gaps'].pop() if old['gaps'] and old['gaps'][-1]['reason'] == 'paused' else None
            resumed_s = gap['end'] if gap else (moved[0]['start'] if moved else self._elapsed())
            start_dt = datetime.datetime.fromtimestamp(started_ts(old) + resumed_s, tz=datetime.timezone.utc).astimezone()
            old['ended_at'] = datetime.datetime.fromtimestamp(started_ts(old) + (gap['start'] if gap else resumed_s),
                                                              tz=datetime.timezone.utc).astimezone().isoformat(timespec='seconds')
            self.store.save(old)
            fresh = new_meeting(new_id(start_dt), old['title'], old['source'], start_dt, old.get('language', 'auto'))
            fresh['speakers'] = [dict(s) for s in old['speakers']]
            fresh['turns'] = [{**t, 'start': round(t['start'] - resumed_s, 2), 'end': round(t['end'] - resumed_s, 2)} for t in moved]
            self.meeting = fresh
            self._resumed_turn_index = None
            self._previous = None
            self.store.save(fresh)
            self._set_notice('new')
            self._publish('meeting_turn', {'id': fresh['id'], 'reload': True})
            self._emit()

    # ── Transitions ──────────────────────────────────────────────────────────

    def _begin(self, source: dict, notice: Optional[str]):
        now = self._now()
        source = {k: source.get(k, '') for k in ('key', 'app', 'kind', 'title', 'code')}
        language = str(self._settings('meeting.language', 'auto') or 'auto')
        self.meeting = new_meeting(new_id(now), title_for(source), source, now, language)
        self.state = 'live'
        self.excluded = False
        self.paused_by_user = False
        self.behind_s = 0.0
        self._pending = None
        self._speech_s = 0.0
        self._resumed_turn_index = None
        self._gap_open = None
        self.store.save(self.meeting)
        if self._previous is not None and self._previous_until and now <= self._previous_until:
            notice = 'new'
        else:
            self._previous = None
        self._set_notice(notice)
        logger.info(f"Meeting started: {self.meeting['title']} ({source['kind']})")
        self._start_capture(self.meeting)
        self._emit()

    def _pause(self, by_user: bool):
        self.state = 'paused'
        self.paused_by_user = by_user
        self._paused_at = self._now()
        self._open_gap('paused')
        self._stop_capture()
        self.saver.flush()
        gap_s = float(self._settings('meeting.gap_minutes', 10) or 10) * 60
        self._cancel_gap_timer = self._schedule(gap_s, self._gap_elapsed)
        logger.info(f"Meeting paused ({'by you' if by_user else 'source gone'})")
        self._emit()

    def _resume(self, notice: Optional[str]):
        if self._cancel_gap_timer:
            self._cancel_gap_timer()
            self._cancel_gap_timer = None
        self.state = 'live'
        self.paused_by_user = False
        self._close_gap()
        self._resumed_turn_index = len(self.meeting['turns'])
        self._set_notice(notice)
        self._start_capture(self.meeting)
        self._emit()

    def _gap_elapsed(self):
        with self._lock:
            if self.state == 'paused' and not self.paused_by_user:
                self._end()

    def _end(self, remember: bool = True):
        m = self.meeting
        if self._cancel_gap_timer:
            self._cancel_gap_timer()
            self._cancel_gap_timer = None
        if self.state == 'live':
            self._stop_capture()
        self._close_gap()
        ended = self._paused_at if (self.state == 'paused' and self._paused_at) else self._now()
        m['ended_at'] = ended.isoformat(timespec='seconds')
        self.saver.flush()
        self.store.save(m)
        gap_s = float(self._settings('meeting.gap_minutes', 10) or 10) * 60
        if remember:
            self._previous = m
            self._previous_paused_s = max(0.0, ended.timestamp() - started_ts(m))
            self._previous_until = self._now() + datetime.timedelta(seconds=gap_s)
        else:
            self._previous = None
        self.state = 'ended'
        self.excluded = False
        self.paused_by_user = False
        self._paused_at = None
        logger.info(f"Meeting ended: {m['title']}, {duration_s(m):.0f} s, {len(m['turns'])} turns")
        payload = self.state_payload()
        payload['duration_s'] = round(duration_s(m))
        self._publish('meeting', payload)
        self.meeting = None
        self.state = 'idle'
        self._resumed_turn_index = None
        self._emit()

    def _open_gap(self, reason: str):
        m = self.meeting
        if self._gap_open is not None:
            return
        m['gaps'].append({'start': round(self._elapsed(), 2), 'end': None, 'reason': reason})
        self._gap_open = len(m['gaps']) - 1

    def _close_gap(self):
        m = self.meeting
        if m is None or self._gap_open is None:
            return
        m['gaps'][self._gap_open]['end'] = round(self._elapsed(), 2)
        self._gap_open = None
        self.saver.mark(m)
```

Note for the implementer: `test_pause_and_exclude_record_gaps` expects the pause gap `{'start': 0.0, 'end': 10.0}` because the meeting began at the same fake time; the exclude gap starts at 15.0 (10 s paused + 5 s). `test_source_gone_pauses_and_same_source_resumes` expects the gap end at 120.0 for the same reason.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... -m unittest tests.test_meeting.SessionTests -v`. Expected: all 15 pass. If `test_different_code_within_gap_starts_new_and_can_continue_previous` fails on `can_continue`, check that `_begin` keeps `_previous` when `now <= _previous_until`.

- [ ] **Step 5: Commit**

```bash
git -C S:/Koli-meeting-mode add src/koli/meeting/session.py tests/test_meeting.py
git -C S:/Koli-meeting-mode commit -m "feat(meeting): session state machine with resume-or-new, gaps and exclude me

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Source detection, platform probes, and the `meeting` rule key

**Files:**
- Create: `src/koli/meeting/detector.py`, `src/koli/platform/windows/audio_sessions.py`, `src/koli/platform/macos/audio_sessions.py`, `src/koli/platform/windows/browser_tabs.py`, `src/koli/platform/macos/browser_tabs.py`
- Modify: `src/koli/platform/__init__.py` (both import lines), `src/koli/services/rules.py:16` (`RULE_KEYS`), `src/koli/app_rules.defaults.yaml` (header comment + rules), `tests/test_smoke.py:17` (`_PLATFORM_SUBMODULES`)
- Test: `tests/test_meeting.py`

**Interfaces:**
- Consumes: `koli.app_rules.rule_matches(rule, exe, title)`.
- Produces: `BROWSERS`, `MEET_CODE`, `strip_media(name) -> (title, media)`, `find_sources(sessions, tabs, rules, detect_default) -> list`, `class MeetingDetector(on_sources, rules_provider, mode_provider, interval=1.0, sessions_fn=None, tabs_fn=None)` with `start()`, `stop()`, `tick()`. Platform: `audio_sessions.active() -> [{pid, exe, state}]` (state 1 = active), `browser_tabs.tabs(exes=BROWSERS) -> [{exe, title, media, hwnd}]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meeting.py`:

```python
RULES = [
    {'id': 'discord', 'match': ['discord.exe'], 'meeting': 'default'},
    {'id': 'zoom', 'match': ['zoom.exe'], 'meeting': 'ask'},
    {'id': 'meet', 'match': ['chrome.exe', 'msedge.exe', 'brave.exe'], 'window_title': ['Meet - '], 'meeting': 'default'},
    {'id': 'teams-web', 'match': ['chrome.exe', 'msedge.exe'], 'window_title': ['Microsoft Teams'], 'meeting': 'off'},
    {'id': 'terminals', 'match': ['cmd.exe', 'WindowsTerminal.exe'], 'auto_send': False},
]


class DetectorTests(unittest.TestCase):
    def test_native_app_with_active_session_is_a_source(self):
        from koli.meeting.detector import find_sources
        sources = find_sources([{'pid': 1, 'exe': 'discord.exe', 'state': 1}], [], RULES, 'auto')
        self.assertEqual(sources, [{'key': 'discord.exe', 'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': '', 'mode': 'auto'}])

    def test_inactive_session_and_unlisted_app_are_ignored(self):
        from koli.meeting.detector import find_sources
        sessions = [{'pid': 1, 'exe': 'discord.exe', 'state': 0}, {'pid': 2, 'exe': 'spotify.exe', 'state': 1},
                    {'pid': 3, 'exe': 'cmd.exe', 'state': 1}]
        self.assertEqual(find_sources(sessions, [], RULES, 'auto'), [])

    def test_rule_mode_wins_over_default_and_off_drops(self):
        from koli.meeting.detector import find_sources
        sessions = [{'pid': 1, 'exe': 'zoom.exe', 'state': 1}, {'pid': 2, 'exe': 'chrome.exe', 'state': 1}]
        tabs = [{'exe': 'chrome.exe', 'title': 'Microsoft Teams - Audio playing', 'media': 'Audio playing', 'hwnd': 9}]
        sources = find_sources(sessions, tabs, RULES, 'auto')
        self.assertEqual([(s['app'], s['mode']) for s in sources], [('zoom.exe', 'ask')])

    def test_browser_tab_behind_others_with_code(self):
        from koli.meeting.detector import find_sources
        sessions = [{'pid': 2, 'exe': 'chrome.exe', 'state': 1}]
        tabs = [{'exe': 'chrome.exe', 'title': 'Inbox - Gmail', 'media': '', 'hwnd': 9},
                {'exe': 'chrome.exe', 'title': 'Meet - abc-defg-hij', 'media': 'Camera and microphone in use', 'hwnd': 9}]
        sources = find_sources(sessions, tabs, RULES, 'auto')
        self.assertEqual(sources, [{'key': 'chrome.exe|Meet - abc-defg-hij', 'app': 'chrome.exe', 'kind': 'browser',
                                    'title': 'Meet - abc-defg-hij', 'code': 'abc-defg-hij', 'mode': 'auto'}])

    def test_browser_tab_without_audio_session_is_not_a_source(self):
        from koli.meeting.detector import find_sources
        tabs = [{'exe': 'chrome.exe', 'title': 'Meet - abc-defg-hij', 'media': '', 'hwnd': 9}]
        self.assertEqual(find_sources([{'pid': 2, 'exe': 'chrome.exe', 'state': 0}], tabs, RULES, 'auto'), [])

    def test_strip_media_suffix(self):
        from koli.meeting.detector import strip_media
        self.assertEqual(strip_media('Meet - abc-defg-hij - Microphone in use'), ('Meet - abc-defg-hij', 'Microphone in use'))
        self.assertEqual(strip_media('Inbox - Gmail'), ('Inbox - Gmail', ''))
        self.assertEqual(strip_media('Tab - Audio muted'), ('Tab', 'Audio muted'))

    def test_detector_tick_reports_only_changes_to_the_log_but_always_to_session(self):
        from koli.meeting.detector import MeetingDetector
        seen = []
        d = MeetingDetector(on_sources=seen.append, rules_provider=lambda: RULES, mode_provider=lambda: 'auto',
                            sessions_fn=lambda: [{'pid': 1, 'exe': 'discord.exe', 'state': 1}], tabs_fn=lambda: [])
        d.tick()
        d.tick()
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][0]['app'], 'discord.exe')

    def test_detector_survives_probe_errors(self):
        from koli.meeting.detector import MeetingDetector
        seen = []

        def boom():
            raise RuntimeError('COM failed')
        d = MeetingDetector(on_sources=seen.append, rules_provider=lambda: RULES, mode_provider=lambda: 'auto',
                            sessions_fn=boom, tabs_fn=lambda: [])
        d.tick()
        self.assertEqual(seen, [[]])

    def test_platform_stubs_import_everywhere(self):
        from koli.platform.macos import audio_sessions, browser_tabs
        self.assertEqual(audio_sessions.active(), [])
        self.assertEqual(browser_tabs.tabs(), [])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... -m unittest tests.test_meeting.DetectorTests -v`. Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write the detector**

`src/koli/meeting/detector.py`:

```python
# meeting/detector.py
# Which call is on right now. A process with an open audio session (Discord
# keeps one for the whole call; a notification ping doesn't) that matches an
# app rule with a `meeting` setting is a source; a browser counts through its
# tabs, whose names carry the tab title and media state ("Meet - abc-defg-hij
# - Microphone in use"). The pure part is find_sources(); the thread just
# polls the platform probes once a second.

import logging
import re
import threading
from typing import Callable, Optional

from ..app_rules import rule_matches

logger = logging.getLogger(__name__)

BROWSERS = ('chrome.exe', 'msedge.exe', 'brave.exe')
MEET_CODE = re.compile(r'\b([a-z]{3}-[a-z]{4}-[a-z]{3})\b')
MODES = ('auto', 'ask', 'off')
SESSION_ACTIVE = 1
# Chrome and Edge append these to a tab's accessible name (tab_utils.cc).
MEDIA_STATES = ('Audio playing', 'Audio muted', 'Microphone in use', 'Camera in use',
                'Camera and microphone in use', 'Sharing screen', 'Playing in picture-in-picture',
                'Media recording', 'Connected to a device')


# ── Pure ─────────────────────────────────────────────────────────────────────

def strip_media(name: str) -> tuple:
    name = name or ''
    for state in MEDIA_STATES:
        suffix = f' - {state}'
        if name.endswith(suffix):
            return name[:-len(suffix)], state
    return name, ''


def _mode_of(rule: dict, detect_default: str) -> str:
    mode = rule.get('meeting')
    return mode if mode in MODES else detect_default


def _meeting_rules(rules: list) -> list:
    return [r for r in rules if r.get('meeting') and not r.get('disabled')]


# The sources on right now, in a stable order. `sessions` come from
# audio_sessions.active(), `tabs` from browser_tabs.tabs(), `rules` from the
# app rules (first match wins, as for delivery).
def find_sources(sessions: list, tabs: list, rules: list, detect_default: str) -> list:
    detect_default = detect_default if detect_default in MODES else 'auto'
    meeting_rules = _meeting_rules(rules)
    active = []
    for s in sessions:
        exe = (s.get('exe') or '').lower()
        if exe and int(s.get('state', 0)) == SESSION_ACTIVE and exe not in active:
            active.append(exe)
    sources = []
    for exe in active:
        if exe in BROWSERS:
            for tab in tabs:
                if (tab.get('exe') or '').lower() != exe:
                    continue
                title, _ = strip_media(tab.get('title') or '')
                rule = next((r for r in meeting_rules if rule_matches(r, exe, title)), None)
                if rule is None:
                    continue
                mode = _mode_of(rule, detect_default)
                if mode == 'off':
                    continue
                found = MEET_CODE.search(title.lower())
                sources.append({'key': f'{exe}|{title}', 'app': exe, 'kind': 'browser', 'title': title,
                                'code': found.group(1) if found else '', 'mode': mode})
            continue
        rule = next((r for r in meeting_rules if rule_matches(r, exe, '')), None)
        if rule is None:
            continue
        mode = _mode_of(rule, detect_default)
        if mode == 'off':
            continue
        sources.append({'key': exe, 'app': exe, 'kind': 'native', 'title': '', 'code': '', 'mode': mode})
    return sources


# ── Thread ───────────────────────────────────────────────────────────────────

class MeetingDetector:
    def __init__(self, on_sources: Callable[[list], None], rules_provider: Callable[[], list],
                 mode_provider: Callable[[], str], interval: float = 1.0,
                 sessions_fn: Optional[Callable[[], list]] = None, tabs_fn: Optional[Callable[[], list]] = None):
        self.on_sources = on_sources
        self.rules_provider = rules_provider
        self.mode_provider = mode_provider
        self.interval = interval
        self._sessions_fn = sessions_fn
        self._tabs_fn = tabs_fn
        self._stop = threading.Event()
        self._thread = None
        self._last_keys = None

    def _sessions(self) -> list:
        if self._sessions_fn is not None:
            return self._sessions_fn()
        from ..platform import audio_sessions
        return audio_sessions.active()

    def _tabs(self, needed: bool) -> list:
        if not needed:
            return []
        if self._tabs_fn is not None:
            return self._tabs_fn()
        from ..platform import browser_tabs
        return browser_tabs.tabs()

    # One poll. A probe that fails counts as "nothing on", and is logged once.
    def tick(self):
        try:
            sessions = self._sessions()
            browser_on = any((s.get('exe') or '').lower() in BROWSERS and int(s.get('state', 0)) == SESSION_ACTIVE
                             for s in sessions)
            sources = find_sources(sessions, self._tabs(browser_on), self.rules_provider(), self.mode_provider())
        except Exception as e:
            if self._last_keys != 'error':
                logger.warning(f"Meeting detection probe failed: {e}")
                self._last_keys = 'error'
            sources = []
        keys = [s['key'] for s in sources]
        if keys != self._last_keys and self._last_keys != 'error' or (self._last_keys == 'error' and keys):
            logger.info(f"Meeting sources: {keys or 'none'}")
        if self._last_keys != 'error' or keys:
            self._last_keys = keys
        self.on_sources(sources)

    def _run(self):
        while not self._stop.wait(self.interval):
            self.tick()

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name='meeting-detector')
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread = None
```

`src/koli/platform/windows/audio_sessions.py`:

```python
# platform/windows/audio_sessions.py
# Which programs have an audio session open right now (the entries the volume
# mixer shows) and whether each is active, through pycaw. A call keeps its
# session active for its whole length, even in silence, which is what meeting
# detection keys on. macOS mirror: platform/macos/audio_sessions.py (stub).

import logging
import threading

logger = logging.getLogger(__name__)

_com_ready = threading.local()


# COM has to be set up once per thread; the detector thread is not the one
# that imported comtypes.
def _init_com():
    if getattr(_com_ready, 'done', False):
        return
    try:
        import comtypes
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass
    except Exception:
        pass
    _com_ready.done = True


def active() -> list:
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception as e:
        logger.debug(f"pycaw unavailable: {e}")
        return []
    _init_com()
    out = []
    try:
        for session in AudioUtilities.GetAllSessions():
            try:
                process = session.Process
                exe = process.name().lower() if process else ''
                pid = process.pid if process else 0
                out.append({'pid': int(pid), 'exe': exe, 'state': int(session.State)})
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"Audio session probe failed: {e}")
    return out
```

`src/koli/platform/macos/audio_sessions.py`:

```python
# platform/macos/audio_sessions.py
# Not implemented on macOS yet: meeting detection reports no sources, so
# meetings start by hand. Mirrors platform/windows/audio_sessions.py.


def active() -> list:
    return []
```

`src/koli/platform/windows/browser_tabs.py`:

```python
# platform/windows/browser_tabs.py
# The tabs of every Chrome, Edge or Brave window, with their media state, read
# from the browser's tab strip through UI Automation. The strip exposes each
# tab as a TabItem whose name is "<title> - <state>" ("Audio playing",
# "Microphone in use", ...), which is how a Meet call is found behind other
# tabs. Cached for a few seconds: the detector asks once a second.
# macOS mirror: platform/macos/browser_tabs.py (stub).

import ctypes
import ctypes.wintypes as wt
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

BROWSERS = ('chrome.exe', 'msedge.exe', 'brave.exe')
CACHE_SECONDS = 3.0
UIA_CONTROL_TYPE = 30003
UIA_NAME = 30005
UIA_TAB = 50018
UIA_TABITEM = 50019
TREE_SCOPE_DESCENDANTS = 4
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_cache = (0.0, [])
_cache_lock = threading.Lock()
_client = threading.local()


def _exe_of(pid: int) -> str:
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ''
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ''
    finally:
        _kernel32.CloseHandle(handle)


# Visible top-level windows of the browsers, as (hwnd, exe).
def _browser_windows(exes) -> list:
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def each(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _exe_of(pid.value)
        if exe in exes:
            found.append((int(hwnd), exe))
        return True
    _user32.EnumWindows(each, 0)
    return found


# One UI Automation client per thread, the way input_watch builds its probe.
def _uia():
    if getattr(_client, 'uia', None) is not None:
        return _client.uia
    if getattr(_client, 'failed', False):
        return None
    try:
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass
        comtypes.client.GetModule('UIAutomationCore.dll')
        from comtypes.gen import UIAutomationClient as uiac
        _client.uia = comtypes.client.CreateObject(uiac.CUIAutomation, interface=uiac.IUIAutomation)
        return _client.uia
    except Exception as e:
        _client.failed = True
        logger.warning(f"Browser tabs: UI Automation unavailable ({e})")
        return None


def _tab_names(uia, hwnd: int) -> list:
    root = uia.ElementFromHandle(hwnd)
    strip_cond = uia.CreatePropertyCondition(UIA_CONTROL_TYPE, UIA_TAB)
    item_cond = uia.CreatePropertyCondition(UIA_CONTROL_TYPE, UIA_TABITEM)
    names = []
    strips = root.FindAll(TREE_SCOPE_DESCENDANTS, strip_cond)
    scopes = [strips.GetElement(i) for i in range(strips.Length)] or [root]
    for scope in scopes:
        items = scope.FindAll(TREE_SCOPE_DESCENDANTS, item_cond)
        for i in range(items.Length):
            name = items.GetElement(i).CurrentName or ''
            if name:
                names.append(name)
    return names


def tabs(exes=BROWSERS) -> list:
    global _cache
    with _cache_lock:
        stamp, cached = _cache
        if time.monotonic() - stamp < CACHE_SECONDS:
            return list(cached)
    from ...meeting.detector import strip_media
    uia = _uia()
    out = []
    if uia is not None:
        for hwnd, exe in _browser_windows(exes):
            try:
                for name in _tab_names(uia, hwnd):
                    title, media = strip_media(name)
                    out.append({'exe': exe, 'title': title, 'media': media, 'hwnd': hwnd})
            except Exception as e:
                logger.debug(f"Tab strip of {exe} window {hwnd} unreadable: {e}")
    with _cache_lock:
        _cache = (time.monotonic(), out)
    return list(out)
```

`src/koli/platform/macos/browser_tabs.py`:

```python
# platform/macos/browser_tabs.py
# Not implemented on macOS yet: no browser tab is ever a meeting source.
# Mirrors platform/windows/browser_tabs.py.

BROWSERS = ('Google Chrome', 'Microsoft Edge', 'Brave Browser')


def tabs(exes=BROWSERS) -> list:
    return []
```

`src/koli/platform/__init__.py`: add `audio_sessions, browser_tabs` to the end of both import lines.

`src/koli/services/rules.py`: in `RULE_KEYS` add `'meeting'` after `'auto_dictation'`.

`tests/test_smoke.py:17`: add `'audio_sessions', 'browser_tabs'` to `_PLATFORM_SUBMODULES` so the stand-in platform package has them.

`src/koli/app_rules.defaults.yaml`: in the header comment, after the `auto_dictation` lines, add:

```
#   meeting:     default | auto | ask | off - this app (or browser tab, by
#                                 window_title) holds calls Koli can
#                                 transcribe: default follows the Meetings
#                                 setting, auto starts a meeting when the
#                                 call has sound, ask offers one, off ignores
#                                 it. A rule without `meeting` is never a
#                                 meeting source.
```

and append these rules at the END of the `rules:` list (after the last shipped rule), so delivery rules keep matching first:

```yaml
  # ── Meetings ─────────────────────────────────────────────────────────────
  # Calls Koli can transcribe (Settings → Meetings). Browser tabs match by the
  # tab's title.
  - id: meeting-discord
    name: Discord calls
    match: ["discord.exe"]
    meeting: default
  - id: meeting-zoom
    name: Zoom
    match: ["zoom.exe"]
    meeting: default
  - id: meeting-teams
    name: Microsoft Teams
    match: ["ms-teams.exe", "teams.exe"]
    meeting: default
  - id: meeting-slack
    name: Slack huddles
    match: ["slack.exe"]
    meeting: default
  - id: meeting-google-meet
    name: Google Meet
    match: ["chrome.exe", "msedge.exe", "brave.exe"]
    window_title: ["Meet - "]
    meeting: default
  - id: meeting-teams-web
    name: Teams in the browser
    match: ["chrome.exe", "msedge.exe", "brave.exe"]
    window_title: ["Microsoft Teams"]
    meeting: default
  - id: meeting-zoom-web
    name: Zoom in the browser
    match: ["chrome.exe", "msedge.exe", "brave.exe"]
    window_title: ["Zoom Meeting", "Zoom Workplace"]
    meeting: default
  - id: meeting-discord-web
    name: Discord in the browser
    match: ["chrome.exe", "msedge.exe", "brave.exe"]
    window_title: ["Discord"]
    meeting: default
```

Check first how existing rules in that file set `name` and `id` (read the file's first rule) and match that style exactly.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... -m unittest tests.test_meeting.DetectorTests -v` then the full suite. Expected: all pass. `test_smoke.py` has tests over the shipped rules (grep `app_rules.defaults` in it); if one counts rules or expects the last rule to be a specific id, update that expectation.

- [ ] **Step 5: Commit**

```bash
git -C S:/Koli-meeting-mode add src/koli/meeting/detector.py src/koli/platform/windows/audio_sessions.py src/koli/platform/macos/audio_sessions.py src/koli/platform/windows/browser_tabs.py src/koli/platform/macos/browser_tabs.py src/koli/platform/__init__.py src/koli/services/rules.py src/koli/app_rules.defaults.yaml tests/test_meeting.py tests/test_smoke.py
git -C S:/Koli-meeting-mode commit -m "feat(meeting): detect calls from audio sessions and browser tabs; meeting key on app rules

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---
### Task 4: Capture: chunker, loopback stream, microphone tap

**Files:**
- Create: `src/koli/meeting/capture.py`
- Modify: `src/koli/audio_recorder.py` (`__init__` near line 95, `_audio_callback` line 255, plus two new methods), `src/koli/state_manager.py:1237` (`rebuild_audio_recorder`)
- Test: `tests/test_meeting.py`

**Interfaces:**
- Consumes: `koli.voice_activity_detection.convert_audio_for_ten_vad`, `VAD_CHUNK_SIZE`.
- Produces: `class Chunker(stream, speech_prob, on_chunk, on_speech=None, pause_s=0.6, max_s=15.0, min_speech_s=0.3, rate=16000)` with `feed(samples, t_end)`, `flush()`; chunks are `{'audio': np.float32[...], 'stream': 'mic'|'loopback', 't0': float, 't1': float, 'speech_s': float}`. `speech_prob_function() -> Callable[[np.ndarray], float]`. `class LoopbackStream(on_block, on_error, block_s=0.1, rate=16000, retries=3, open_fn=None)` with `start()`, `stop()`. `class MicTap(recorder_provider, on_block, allow)` with `start()`, `stop()`. `AudioRecorder.add_tap(fn)`, `AudioRecorder.remove_tap(fn)`; taps are called as `fn(mono_16k_float32, time.time())`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meeting.py`:

```python
import numpy as np


class CaptureTests(unittest.TestCase):
    # A fake VAD: a frame is speech when its mean absolute level is above 0.1.
    @staticmethod
    def prob(frame):
        return 0.9 if float(np.abs(frame).mean()) > 0.1 else 0.05

    def make(self, **kw):
        from koli.meeting.capture import Chunker
        self.chunks = []
        self.speech = []
        return Chunker('loopback', self.prob, self.chunks.append, on_speech=self.speech.append, **kw)

    @staticmethod
    def seconds(level, s, rate=16000):
        return np.full(int(s * rate), level, dtype=np.float32)

    def test_cuts_at_a_pause_and_keeps_wall_clock_times(self):
        c = self.make()
        t = 1000.0
        for block in (self.seconds(0.5, 1.0), self.seconds(0.0, 0.8)):
            t += len(block) / 16000
            c.feed(block, t)
        self.assertEqual(len(self.chunks), 1)
        ch = self.chunks[0]
        self.assertEqual(ch['stream'], 'loopback')
        self.assertAlmostEqual(ch['t0'], 1000.0, places=2)
        self.assertLessEqual(ch['t1'], 1001.5)       # trailing silence trimmed
        self.assertGreaterEqual(ch['t1'], 1001.0)
        self.assertAlmostEqual(ch['speech_s'], 1.0, places=1)
        self.assertGreater(sum(self.speech), 0.9)     # on_speech saw about a second

    def test_cuts_at_max_length_while_still_speaking(self):
        c = self.make(max_s=2.0)
        t = 0.0
        for _ in range(5):
            block = self.seconds(0.5, 0.5)
            t += 0.5
            c.feed(block, t)
        self.assertEqual(len(self.chunks), 1)
        self.assertAlmostEqual(len(self.chunks[0]['audio']) / 16000, 2.0, places=1)

    def test_drops_too_short_speech_and_pure_silence(self):
        c = self.make()
        c.feed(self.seconds(0.5, 0.1), 0.1)
        c.feed(self.seconds(0.0, 1.0), 1.1)
        c.feed(self.seconds(0.0, 3.0), 4.1)
        self.assertEqual(self.chunks, [])

    def test_flush_emits_the_open_chunk(self):
        c = self.make()
        c.feed(self.seconds(0.5, 0.7), 0.7)
        self.assertEqual(self.chunks, [])
        c.flush()
        self.assertEqual(len(self.chunks), 1)

    def test_loopback_stream_reports_after_retries(self):
        from koli.meeting.capture import LoopbackStream
        errors = []
        attempts = []

        def open_fn():
            attempts.append(1)
            raise RuntimeError('no loopback device')
        s = LoopbackStream(on_block=lambda a, t: None, on_error=errors.append, retries=2, open_fn=open_fn, retry_wait=0.0)
        s.start()
        s._thread.join(2.0)
        self.assertEqual(len(attempts), 3)
        self.assertIn('no loopback device', errors[0])

    def test_mic_tap_respects_allow(self):
        from koli.meeting.capture import MicTap

        class Recorder:
            def __init__(self):
                self.taps = []

            def add_tap(self, fn):
                self.taps.append(fn)

            def remove_tap(self, fn):
                self.taps.remove(fn)
        rec = Recorder()
        got = []
        allowed = {'ok': True}
        tap = MicTap(lambda: rec, got.append, lambda: allowed['ok'])
        tap.start()
        rec.taps[0](np.zeros(160, dtype=np.float32), 1.0)
        allowed['ok'] = False
        rec.taps[0](np.zeros(160, dtype=np.float32), 2.0)
        self.assertEqual(len(got), 1)
        tap.stop()
        self.assertEqual(rec.taps, [])

    def test_recorder_calls_taps_with_16k_mono(self):
        from koli.audio_recorder import AudioRecorder
        rec = AudioRecorder.__new__(AudioRecorder)
        rec._taps = []
        rec._recording_rate = 48000
        rec._needs_resampling_cached = True
        rec.WHISPER_SAMPLE_RATE = 16000
        import logging
        rec.logger = logging.getLogger('test')
        got = []
        rec.add_tap(lambda audio, when: got.append(audio))
        rec._notify_taps(np.zeros((4800, 1), dtype=np.float32))
        self.assertEqual(got[0].shape, (1600,))
        self.assertEqual(got[0].dtype, np.float32)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... -m unittest tests.test_meeting.CaptureTests -v`. Expected: `ModuleNotFoundError` for `koli.meeting.capture`, and `AttributeError: add_tap` for the recorder test.

- [ ] **Step 3: Write capture.py**

```python
# meeting/capture.py
# The two audio streams of a meeting and how they become chunks. The loopback
# stream reads what the speakers play (soundcard, WASAPI loopback); the mic
# tap hooks the recorder's always-open microphone stream. Each goes through a
# Chunker that uses ten-vad to cut at pauses, so Whisper gets short pieces of
# one speaker at a time, with wall-clock times for the transcript.

import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

from ..voice_activity_detection import VAD_CHUNK_SIZE, convert_audio_for_ten_vad

logger = logging.getLogger(__name__)

RATE = 16000
FRAME = VAD_CHUNK_SIZE              # 256 samples, 16 ms
FRAME_S = FRAME / RATE
SPEECH_THRESHOLD = 0.5
TRAIL_S = 0.3                        # silence kept after the last speech


# ── Speech probability ───────────────────────────────────────────────────────

# A function frame -> probability of speech, from ten-vad when it is
# installed, else a loudness guess so a meeting still cuts at pauses.
def speech_prob_function() -> Callable[[np.ndarray], float]:
    try:
        from ten_vad import TenVad
        vad = TenVad()
        lock = threading.Lock()

        def prob(frame: np.ndarray) -> float:
            with lock:
                p, _ = vad.process(convert_audio_for_ten_vad(frame))
            return float(p)
        return prob
    except Exception as e:
        logger.warning(f"ten-vad unavailable for meetings ({e}); using loudness")

        def loud(frame: np.ndarray) -> float:
            return 0.9 if float(np.sqrt(np.mean(frame.astype(np.float32) ** 2))) > 0.01 else 0.0
        return loud


# ── Chunker ──────────────────────────────────────────────────────────────────

class Chunker:
    def __init__(self, stream: str, speech_prob: Callable[[np.ndarray], float], on_chunk: Callable[[dict], None],
                 on_speech: Optional[Callable[[float], None]] = None, pause_s: float = 0.6, max_s: float = 15.0,
                 min_speech_s: float = 0.3, rate: int = RATE):
        self.stream = stream
        self.speech_prob = speech_prob
        self.on_chunk = on_chunk
        self.on_speech = on_speech
        self.pause_frames = max(1, int(round(pause_s / FRAME_S)))
        self.max_frames = max(self.pause_frames + 1, int(round(max_s / FRAME_S)))
        self.min_speech_frames = max(1, int(round(min_speech_s / FRAME_S)))
        self.rate = rate
        self._lock = threading.Lock()
        self._frames = []            # frames of the open chunk
        self._speech = []            # one bool per frame
        self._rest = np.zeros(0, dtype=np.float32)   # samples not yet a full frame
        self._t_end = 0.0            # wall clock at the end of the last frame

    # `samples` is mono float32 at `rate`; `t_end` is the wall clock of its
    # last sample.
    def feed(self, samples: np.ndarray, t_end: float):
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        with self._lock:
            data = np.concatenate([self._rest, samples]) if len(self._rest) else samples
            whole = (len(data) // FRAME) * FRAME
            frames, self._rest = data[:whole], data[whole:]
            # The first frame of this feed ended this long before t_end.
            n = whole // FRAME
            spoken = 0
            for i in range(n):
                frame = frames[i * FRAME:(i + 1) * FRAME]
                speech = self.speech_prob(frame) >= SPEECH_THRESHOLD
                self._t_end = t_end - (n - 1 - i) * FRAME_S - len(self._rest) / self.rate
                self._push(frame, speech)
                spoken += speech
            if spoken and self.on_speech:
                self.on_speech(spoken * FRAME_S)

    def _push(self, frame: np.ndarray, speech: bool):
        if not self._frames and not speech:
            return                   # silence before any speech is not kept
        self._frames.append(frame)
        self._speech.append(speech)
        trailing = 0
        for s in reversed(self._speech):
            if s:
                break
            trailing += 1
        if trailing >= self.pause_frames or len(self._frames) >= self.max_frames:
            self._emit_locked()

    def flush(self):
        with self._lock:
            self._emit_locked()

    def _emit_locked(self):
        if not self._frames:
            return
        frames, speech = self._frames, self._speech
        self._frames, self._speech = [], []
        voiced = sum(speech)
        if voiced < self.min_speech_frames:
            return
        # Keep a little silence after the last speech, not the whole pause.
        last = max(i for i, s in enumerate(speech) if s)
        keep = min(len(frames), last + 1 + int(round(TRAIL_S / FRAME_S)))
        dropped = len(frames) - keep
        t1 = self._t_end - dropped * FRAME_S
        t0 = t1 - keep * FRAME_S
        self.on_chunk({'audio': np.concatenate(frames[:keep]), 'stream': self.stream,
                       't0': t0, 't1': t1, 'speech_s': voiced * FRAME_S})


# ── Loopback ─────────────────────────────────────────────────────────────────

def _open_default_loopback(rate: int, frames: int):
    import soundcard as sc
    speaker = sc.default_speaker()
    mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
    return mic.recorder(samplerate=rate, channels=1, blocksize=frames)


# Reads what the speakers play, 100 ms at a time, on its own thread. A device
# that disappears is reopened a few times; after that on_error gets a message
# and the thread ends (the meeting carries on with the microphone).
class LoopbackStream:
    def __init__(self, on_block: Callable[[np.ndarray, float], None], on_error: Callable[[str], None],
                 block_s: float = 0.1, rate: int = RATE, retries: int = 3, open_fn=None, retry_wait: float = 1.0):
        self.on_block = on_block
        self.on_error = on_error
        self.rate = rate
        self.frames = int(block_s * rate)
        self.retries = retries
        self.retry_wait = retry_wait
        self._open = open_fn or (lambda: _open_default_loopback(self.rate, self.frames))
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name='meeting-loopback')
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)

    def _run(self):
        failures = 0
        while not self._stop.is_set():
            try:
                with self._open() as rec:
                    failures = 0
                    while not self._stop.is_set():
                        data = rec.record(numframes=self.frames)
                        block = np.asarray(data, dtype=np.float32)
                        if block.ndim > 1:
                            block = block[:, 0]
                        self.on_block(block, time.time())
                return
            except Exception as e:
                failures += 1
                if failures > self.retries:
                    logger.error(f"System audio capture gave up: {e}")
                    self.on_error(f"System audio capture stopped: {e}")
                    return
                logger.warning(f"System audio capture failed ({e}); retry {failures}/{self.retries}")
                if self._stop.wait(self.retry_wait):
                    return


# ── Microphone tap ───────────────────────────────────────────────────────────

# The microphone as the recorder already captures it. `allow()` says whether
# a block may go into the meeting: not while you dictate, not while excluded.
class MicTap:
    def __init__(self, recorder_provider: Callable, on_block: Callable[[np.ndarray, float], None],
                 allow: Callable[[], bool]):
        self._recorder = recorder_provider
        self.on_block = on_block
        self.allow = allow
        self._attached = None

    def _tap(self, audio: np.ndarray, when: float):
        if self.allow():
            self.on_block(audio, when)

    def start(self):
        rec = self._recorder()
        if rec is None or self._attached is rec:
            return
        self.stop()
        rec.add_tap(self._tap)
        self._attached = rec

    def stop(self):
        if self._attached is not None:
            try:
                self._attached.remove_tap(self._tap)
            except Exception:
                pass
            self._attached = None
```

`src/koli/audio_recorder.py` changes:

In `__init__`, next to `self._current_level = 0.0` (line ~95) add:

```python
        # Meeting mode listens to the microphone through taps: fn(mono 16 kHz
        # float32, wall clock), called on the audio thread for every chunk.
        self._taps = []
```

Add after `_handle_vad_event`:

```python
    # ── Taps ─────────────────────────────────────────────────────────────────

    def add_tap(self, fn):
        if fn not in self._taps:
            self._taps.append(fn)

    def remove_tap(self, fn):
        if fn in self._taps:
            self._taps.remove(fn)

    # Each chunk to every tap, at Whisper's rate. A tap that raises is logged,
    # never allowed to break the audio callback.
    def _notify_taps(self, chunk: np.ndarray):
        if not self._taps:
            return
        mono = chunk.reshape(-1) if chunk.ndim == 1 or chunk.shape[1] == 1 else chunk.mean(axis=1)
        mono = mono.astype(np.float32, copy=False)
        if self._needs_resampling_cached:
            mono = self._resample_audio(mono, self._recording_rate, self.WHISPER_SAMPLE_RATE)
        when = time.time()
        for fn in list(self._taps):
            try:
                fn(mono, when)
            except Exception as e:
                self.logger.debug(f"Audio tap failed: {e}")
```

In `_audio_callback`, right after the `with self._buffer_lock:` block (before `if recording:`), add:

```python
        self._notify_taps(chunk)
```

Check that `time` is imported at the top of `audio_recorder.py` (it uses `time.sleep` already).

`src/koli/state_manager.py` `rebuild_audio_recorder`: after `new_recorder = AudioRecorder(**params)` add:

```python
        # A meeting's microphone tap must follow the microphone.
        new_recorder._taps = list(getattr(old, '_taps', []))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... -m unittest tests.test_meeting.CaptureTests -v`, then the full suite. Expected: all pass. If `test_cuts_at_a_pause_and_keeps_wall_clock_times` is off by a frame on `t0`, the `_t_end` bookkeeping in `feed` is wrong: the i-th frame of a feed of n frames ends `(n-1-i)` frames plus the leftover samples before `t_end`.

- [ ] **Step 5: Commit**

```bash
git -C S:/Koli-meeting-mode add src/koli/meeting/capture.py src/koli/audio_recorder.py src/koli/state_manager.py tests/test_meeting.py
git -C S:/Koli-meeting-mode commit -m "feat(meeting): loopback stream, microphone tap and VAD chunker

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Speaker labels and the speaker model

**Files:**
- Create: `src/koli/meeting/speakers.py`
- Modify: `src/koli/model_registry.py` (`__init__` line 17, new methods after `download_streaming_model`)
- Test: `tests/test_meeting.py`

**Interfaces:**
- Produces: `class SpeakerTracker(embed=None, similarity=0.55)` with `load(speakers)`, `label(audio) -> (speaker_id|None, speaker_dict|None)` (the dict is the new or updated speaker to store, `None` when nothing changed), `export() -> list`, `UNKNOWN = '?'`; `make_embedder(model_path, num_threads=1) -> Callable[[np.ndarray], Optional[np.ndarray]]`; `ModelRegistry(whisper_models_config, streaming_models_config, speaker_models_config=None)` with `get_speaker_model_path(key) -> Optional[str]` (downloads on first use) and `is_speaker_model_cached(key) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meeting.py`:

```python
class SpeakerTests(unittest.TestCase):
    @staticmethod
    def unit(*xs):
        v = np.array(xs, dtype=np.float32)
        return v / np.linalg.norm(v)

    def test_same_voice_joins_far_voice_is_new(self):
        from koli.meeting.speakers import SpeakerTracker
        vecs = {'a': self.unit(1, 0, 0), 'a2': self.unit(0.95, 0.1, 0), 'b': self.unit(0, 1, 0)}
        t = SpeakerTracker(embed=lambda audio: vecs[audio])
        sid, spk = t.label('a')
        self.assertEqual(sid, 's1')
        self.assertEqual(spk['name'], 'Speaker 1')
        self.assertEqual(spk['colour'], 1)
        sid2, spk2 = t.label('a2')
        self.assertEqual(sid2, 's1')
        self.assertEqual(spk2['count'], 2)
        sid3, spk3 = t.label('b')
        self.assertEqual(sid3, 's2')
        self.assertEqual(spk3['colour'], 2)

    def test_centroid_moves_toward_new_samples(self):
        from koli.meeting.speakers import SpeakerTracker
        vecs = {'a': self.unit(1, 0), 'a2': self.unit(0.8, 0.6)}
        t = SpeakerTracker(embed=lambda audio: vecs[audio], similarity=0.5)
        t.label('a')
        _, spk = t.label('a2')
        c = np.array(spk['centroid'])
        self.assertGreater(c[1], 0.2)
        self.assertAlmostEqual(float(np.linalg.norm(c)), 1.0, places=5)

    def test_load_restores_speakers_and_numbering(self):
        from koli.meeting.speakers import SpeakerTracker
        t = SpeakerTracker(embed=lambda audio: self.unit(0, 0, 1))
        t.load([{'id': 'you', 'name': 'You', 'colour': 0},
                {'id': 's1', 'name': 'Ilona', 'colour': 1, 'centroid': [1.0, 0.0, 0.0], 'count': 4},
                {'id': 's2', 'name': 'Speaker 2', 'colour': 2, 'centroid': [0.0, 1.0, 0.0], 'count': 2}])
        sid, spk = t.label('x')
        self.assertEqual(sid, 's3')
        self.assertEqual(spk['colour'], 3)
        self.assertEqual([s['id'] for s in t.export()], ['s1', 's2', 's3'])

    def test_no_embedder_means_unknown(self):
        from koli.meeting.speakers import SpeakerTracker
        t = SpeakerTracker(embed=None)
        self.assertEqual(t.label(np.zeros(16000)), (SpeakerTracker.UNKNOWN, None))
        t2 = SpeakerTracker(embed=lambda audio: None)
        self.assertEqual(t2.label(np.zeros(16000)), (SpeakerTracker.UNKNOWN, None))

    def test_colours_wrap_after_seven(self):
        from koli.meeting.speakers import SpeakerTracker
        t = SpeakerTracker(embed=lambda audio: audio, similarity=0.99)
        ids = []
        for i in range(9):
            v = np.zeros(9, dtype=np.float32)
            v[i] = 1.0
            ids.append(t.label(v))
        self.assertEqual([spk['colour'] for _, spk in ids][:8], [1, 2, 3, 4, 5, 6, 7, 1])

    def test_registry_knows_speaker_models(self):
        from koli.model_registry import ModelRegistry
        r = ModelRegistry(speaker_models_config={'eres2net': {
            'source': 'csukuangfj/speaker-embedding-models', 'label': 'ERes2Net',
            'files': {'model': '3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx'}}})
        self.assertIn('eres2net', r.speaker_models)
        self.assertFalse(r.is_speaker_model_cached('nope'))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... -m unittest tests.test_meeting.SpeakerTests -v`. Expected: `ModuleNotFoundError` and `TypeError: unexpected keyword 'speaker_models_config'`.

- [ ] **Step 3: Write speakers.py and the registry additions**

`src/koli/meeting/speakers.py`:

```python
# meeting/speakers.py
# Who is talking, from the voice alone. Each loopback chunk becomes a
# voiceprint (a sherpa-onnx speaker embedding); voiceprints close to a known
# speaker's average join that speaker, the others start "Speaker N". The
# averages live in the meeting file, so a resumed meeting keeps its labels.
# Pure numpy apart from the embedding model, so the clustering is testable.

import logging
import threading
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

SIMILARITY = 0.55        # cosine; tuned on real calls
COLOURS = 7              # colours 1..7 for the others; 0 is "You"
RATE = 16000


class SpeakerTracker:
    UNKNOWN = '?'

    def __init__(self, embed: Optional[Callable[[np.ndarray], Optional[np.ndarray]]] = None,
                 similarity: float = SIMILARITY):
        self.embed = embed
        self.similarity = similarity
        self._lock = threading.Lock()
        self._speakers = []       # [{'id','name','colour','centroid': np.ndarray,'count'}]

    # Speakers from a meeting file (the "you" entry is ignored).
    def load(self, speakers: list):
        with self._lock:
            self._speakers = []
            for s in speakers:
                if s.get('id') == 'you' or not s.get('centroid'):
                    continue
                self._speakers.append({'id': s['id'], 'name': s.get('name', s['id']), 'colour': int(s.get('colour', 1)),
                                       'centroid': self._unit(np.asarray(s['centroid'], dtype=np.float32)),
                                       'count': int(s.get('count', 1))})

    @staticmethod
    def _unit(v: np.ndarray) -> np.ndarray:
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else v

    def _to_dict(self, s: dict) -> dict:
        return {'id': s['id'], 'name': s['name'], 'colour': s['colour'],
                'centroid': [round(float(x), 5) for x in s['centroid']], 'count': s['count']}

    # The speaker of `audio` (mono float32 at 16 kHz): an existing id or a new
    # one, plus the speaker entry to store; UNKNOWN when no voiceprint could
    # be made.
    def label(self, audio) -> tuple:
        if self.embed is None:
            return self.UNKNOWN, None
        try:
            vec = self.embed(audio)
        except Exception as e:
            logger.debug(f"Speaker embedding failed: {e}")
            vec = None
        if vec is None:
            return self.UNKNOWN, None
        vec = self._unit(np.asarray(vec, dtype=np.float32))
        with self._lock:
            best, best_sim = None, -1.0
            for s in self._speakers:
                sim = float(np.dot(s['centroid'], vec))
                if sim > best_sim:
                    best, best_sim = s, sim
            if best is not None and best_sim >= self.similarity:
                best['centroid'] = self._unit(best['centroid'] * best['count'] + vec)
                best['count'] += 1
                return best['id'], self._to_dict(best)
            n = self._next_number()
            s = {'id': f's{n}', 'name': f'Speaker {n}', 'colour': 1 + (n - 1) % COLOURS, 'centroid': vec, 'count': 1}
            self._speakers.append(s)
            return s['id'], self._to_dict(s)

    def _next_number(self) -> int:
        numbers = [int(s['id'][1:]) for s in self._speakers if s['id'][1:].isdigit()]
        return (max(numbers) + 1) if numbers else 1

    def export(self) -> list:
        with self._lock:
            return [self._to_dict(s) for s in self._speakers]


# A voiceprint function over a sherpa-onnx speaker-embedding model. One
# extractor, one stream per call, serialised: the extractor is not
# thread-safe and only the transcriber calls it anyway.
def make_embedder(model_path: str, num_threads: int = 1) -> Callable[[np.ndarray], Optional[np.ndarray]]:
    import sherpa_onnx
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model_path, num_threads=num_threads, provider='cpu')
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
    lock = threading.Lock()

    def embed(audio: np.ndarray) -> Optional[np.ndarray]:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if len(samples) < RATE // 2:          # under half a second says nothing
            return None
        with lock:
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=RATE, waveform=samples)
            stream.input_finished()
            if not extractor.is_ready(stream):
                return None
            return np.asarray(extractor.compute(stream), dtype=np.float32)
    return embed
```

`src/koli/model_registry.py`:

Change the constructor signature and body:

```python
    def __init__(self, whisper_models_config: dict = None, streaming_models_config: dict = None,
                 speaker_models_config: dict = None):
        self.whisper_models = {}
        self.streaming_models = {}
        self.speaker_models = {}
        ...
        if speaker_models_config:
            for key, config in speaker_models_config.items():
                if isinstance(config, dict):
                    self.speaker_models[key] = ModelDefinition(key, config, model_type="speaker")
```

Add after `download_streaming_model`:

```python
    # ── Speaker-embedding models (meeting mode) ──────────────────────────────
    # One ONNX file each, from a Hugging Face repo, through the hub's cache.

    def _speaker_file(self, key: str) -> Optional[str]:
        model = self.speaker_models.get(key)
        if not model or not model.files.get('model'):
            return None
        snapshots_dir = os.path.join(self.get_hf_cache_path(), model.cache_folder, 'snapshots')
        if not os.path.isdir(snapshots_dir):
            return None
        for snapshot in os.listdir(snapshots_dir):
            path = os.path.join(snapshots_dir, snapshot, model.files['model'])
            if os.path.exists(path):
                return path
        return None

    def is_speaker_model_cached(self, key: str) -> bool:
        return self._speaker_file(key) is not None

    # The model file's path, downloading it first when needed (blocking: call
    # from a background task).
    def get_speaker_model_path(self, key: str) -> Optional[str]:
        path = self._speaker_file(key)
        if path:
            return path
        model = self.speaker_models.get(key)
        if not model:
            return None
        try:
            from huggingface_hub import hf_hub_download
            self.logger.info(f"Downloading speaker model: {model.source}/{model.files['model']}")
            return hf_hub_download(repo_id=model.source, filename=model.files['model'])
        except Exception as e:
            self.logger.error(f"Failed to download speaker model {model.source}: {e}")
            return None
```

Also update the module header comment ("Whisper/streaming models" → "Whisper, streaming and speaker models"). Before relying on the file name, confirm it exists: `curl -sI https://huggingface.co/csukuangfj/speaker-embedding-models/resolve/main/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx | head -1` should show `HTTP/2 302` or `200`. If it is 404, list the repo (`https://huggingface.co/api/models/csukuangfj/speaker-embedding-models`) and pick the `3dspeaker_speech_eres2net_base_sv_*16k.onnx` or `wespeaker_en_voxceleb_resnet34_LM.onnx` file, and use that name in Task 7's defaults.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... -m unittest tests.test_meeting.SpeakerTests -v` and the full suite. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git -C S:/Koli-meeting-mode add src/koli/meeting/speakers.py src/koli/model_registry.py tests/test_meeting.py
git -C S:/Koli-meeting-mode commit -m "feat(meeting): speaker tracker over sherpa-onnx voiceprints; speaker models in the registry

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Transcriber worker and a per-call language for the engine

**Files:**
- Create: `src/koli/meeting/transcriber.py`
- Modify: `src/koli/whisper_engine.py:227` (`_route`), `:336` (`transcribe_audio`); `src/koli/whisper_engine_cpp.py:80` (`transcribe_audio`)
- Test: `tests/test_meeting.py`

**Interfaces:**
- Consumes: Task 4 chunks; Task 5 `SpeakerTracker.label`.
- Produces: `WhisperEngine.transcribe_audio(audio_data, language=None)` where `None` keeps the engine's language, `'auto'` detects, anything else forces that language for this call only. `class MeetingTranscriber(engine_provider, is_busy, tracker, language_provider, on_turn, on_speaker, on_pending, on_behind)` with `start()`, `stop()`, `submit(chunk)`, `clear()`. `on_turn({'t0','t1','stream','speaker','text','lang'})`; `on_speaker(speaker_dict)`; `on_pending(stream|None)`; `on_behind(seconds)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meeting.py`:

```python
import time


class FakeEngine:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.last_info = {}

    def transcribe_audio(self, audio, language=None):
        self.calls.append((len(audio), language))
        text = self.answers.pop(0) if self.answers else None
        self.last_info = {'language': 'he' if text and 'שלום' in text else 'en'}
        return text


class TranscriberTests(unittest.TestCase):
    def run_chunks(self, chunks, answers, busy=None, tracker=None, language='auto'):
        from koli.meeting.transcriber import MeetingTranscriber
        from koli.meeting.speakers import SpeakerTracker
        engine = FakeEngine(answers)
        self.turns, self.speakers, self.pending, self.behind = [], [], [], []
        busy_state = busy or {'busy': False}
        t = MeetingTranscriber(engine_provider=lambda: engine, is_busy=lambda: busy_state['busy'],
                               tracker=tracker or SpeakerTracker(embed=lambda a: np.array([1.0, 0.0])),
                               language_provider=lambda: language,
                               on_turn=self.turns.append, on_speaker=self.speakers.append,
                               on_pending=self.pending.append, on_behind=self.behind.append, poll_s=0.01)
        t.start()
        for c in chunks:
            t.submit(c)
        deadline = time.time() + 3
        while t.queued_seconds() > 0 or t.working:
            if time.time() > deadline:
                break
            time.sleep(0.01)
        t.stop()
        return engine

    @staticmethod
    def chunk(stream, seconds=1.0, t0=100.0):
        return {'audio': np.zeros(int(16000 * seconds), dtype=np.float32), 'stream': stream, 't0': t0, 't1': t0 + seconds, 'speech_s': seconds}

    def test_mic_turn_is_you_and_loopback_turn_is_labelled(self):
        engine = self.run_chunks([self.chunk('mic'), self.chunk('loopback', t0=105)], ['hello', 'שלום'])
        self.assertEqual([(t['speaker'], t['text'], t['lang']) for t in self.turns], [('you', 'hello', 'en'), ('s1', 'שלום', 'he')])
        self.assertEqual(self.speakers[0]['id'], 's1')
        self.assertEqual(engine.calls[0][1], 'auto')
        self.assertEqual(self.pending[0], 'mic')
        self.assertIsNone(self.pending[-1])

    def test_empty_transcription_makes_no_turn(self):
        self.run_chunks([self.chunk('loopback')], [None])
        self.assertEqual(self.turns, [])
        self.assertEqual(self.speakers, [])
        self.assertIsNone(self.pending[-1])

    def test_waits_while_dictation_is_busy(self):
        busy = {'busy': True}
        from koli.meeting.transcriber import MeetingTranscriber
        from koli.meeting.speakers import SpeakerTracker
        engine = FakeEngine(['late'])
        turns = []
        t = MeetingTranscriber(engine_provider=lambda: engine, is_busy=lambda: busy['busy'], tracker=SpeakerTracker(),
                               language_provider=lambda: 'en', on_turn=turns.append, on_speaker=lambda s: None,
                               on_pending=lambda s: None, on_behind=lambda s: None, poll_s=0.01)
        t.start()
        t.submit(self.chunk('mic'))
        time.sleep(0.1)
        self.assertEqual(engine.calls, [])
        busy['busy'] = False
        time.sleep(0.3)
        t.stop()
        self.assertEqual([x['text'] for x in turns], ['late'])
        self.assertEqual(engine.calls[0][1], 'en')

    def test_behind_reports_queued_seconds(self):
        self.run_chunks([self.chunk('mic', 2.0), self.chunk('mic', 3.0)], ['a', 'b'])
        self.assertIn(5.0, self.behind)       # both queued before the first finished, or at least reported
        self.assertEqual(self.behind[-1], 0.0)

    def test_unknown_speaker_when_no_model(self):
        from koli.meeting.speakers import SpeakerTracker
        self.run_chunks([self.chunk('loopback')], ['x'], tracker=SpeakerTracker(embed=None))
        self.assertEqual(self.turns[0]['speaker'], '?')


class EngineLanguageTests(unittest.TestCase):
    def test_route_honours_a_per_call_language(self):
        from koli.whisper_engine import WhisperEngine
        e = WhisperEngine.__new__(WhisperEngine)
        e.model, e.model_key, e.language, e.initial_prompt = 'M', 'small', 'en', None
        e.language_model = None
        self.assertEqual(e._route(np.zeros(10))[2], 'en')
        self.assertEqual(e._route(np.zeros(10), language='he')[2], 'he')
        self.assertIsNone(e._route(np.zeros(10), language='auto')[2])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... -m unittest tests.test_meeting.TranscriberTests tests.test_meeting.EngineLanguageTests -v`. Expected: `ModuleNotFoundError` and `TypeError: _route() got an unexpected keyword argument 'language'`.

- [ ] **Step 3: Write the transcriber and the engine change**

`src/koli/whisper_engine.py`:

Change `_route` to take an optional per-call language. `None` means the engine's own setting; `'auto'` means detect:

```python
    def _route(self, audio: np.ndarray, language: Optional[str] = None) -> tuple:
        forced = self.language if language is None else (None if language == 'auto' else language)
        if getattr(self, 'language_model', None) is None:
            return self.model, self.model_key, forced, self.initial_prompt, None
        target = self.language_model_lang
        if forced:
            if forced == target:
                return self.language_model, self.language_model_key, target, None, None
            return self.model, self.model_key, forced, self.initial_prompt, None
        detected, probability, _ = self.model.detect_language(audio)
        ...   # unchanged from here
```

(`self.language` is replaced by `forced` in the two places shown; the detection branch stays as it is.)

Change `transcribe_audio(self, audio_data: np.ndarray)` to `transcribe_audio(self, audio_data: np.ndarray, language: Optional[str] = None)` and inside `decode()` call `self._route(audio_data, language)`.

`src/koli/whisper_engine_cpp.py`: change the signature the same way and compute the language per call:

```python
    def transcribe_audio(self, audio_data: Optional[np.ndarray], language: Optional[str] = None) -> Optional[str]:
        ...
            forced = self.language if language is None else (None if language == 'auto' else language)
            kwargs = {}
            if forced:
                kwargs['language'] = forced
```

and in `last_info` use `forced or 'auto'`.

`src/koli/meeting/transcriber.py`:

```python
# meeting/transcriber.py
# Turns meeting chunks into transcript turns on one background worker. The
# Whisper engine is shared with dictation, so the worker waits whenever a
# dictation is being recorded or transcribed; the queue just grows and the
# panel shows how far behind it is. Mic chunks are "You"; loopback chunks get
# a speaker from the tracker.

import logging
import queue
import threading
import time
from typing import Callable, Optional

from .speakers import SpeakerTracker

logger = logging.getLogger(__name__)


class MeetingTranscriber:
    def __init__(self, engine_provider: Callable, is_busy: Callable[[], bool], tracker: SpeakerTracker,
                 language_provider: Callable[[], str], on_turn: Callable[[dict], None],
                 on_speaker: Callable[[dict], None], on_pending: Callable[[Optional[str]], None],
                 on_behind: Callable[[float], None], poll_s: float = 0.1):
        self.engine_provider = engine_provider
        self.is_busy = is_busy
        self.tracker = tracker
        self.language_provider = language_provider
        self.on_turn = on_turn
        self.on_speaker = on_speaker
        self.on_pending = on_pending
        self.on_behind = on_behind
        self.poll_s = poll_s
        self._queue = queue.Queue()
        self._queued_s = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.working = False

    def queued_seconds(self) -> float:
        with self._lock:
            return self._queued_s

    def submit(self, chunk: dict):
        with self._lock:
            self._queued_s += float(chunk['t1'] - chunk['t0'])
            behind = self._queued_s
        self._queue.put(chunk)
        self.on_behind(round(behind, 1))

    # Drop what is queued (a meeting ended or was cancelled).
    def clear(self):
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            self._queued_s = 0.0
        self.on_behind(0.0)

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name='meeting-transcriber')
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._queue.put(None)
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(5.0)

    def _run(self):
        while not self._stop.is_set():
            chunk = self._queue.get()
            if chunk is None:
                break
            while self.is_busy() and not self._stop.is_set():
                time.sleep(self.poll_s)
            if self._stop.is_set():
                break
            self.working = True
            try:
                self._transcribe(chunk)
            except Exception as e:
                logger.error(f"Meeting chunk failed: {e}")
            finally:
                self.working = False
                with self._lock:
                    self._queued_s = max(0.0, self._queued_s - float(chunk['t1'] - chunk['t0']))
                    behind = self._queued_s
                self.on_behind(round(behind, 1))
                self.on_pending(None)

    def _transcribe(self, chunk: dict):
        engine = self.engine_provider()
        if engine is None:
            return
        self.on_pending(chunk['stream'])
        text = engine.transcribe_audio(chunk['audio'], language=self.language_provider() or 'auto')
        if not text:
            return
        lang = (getattr(engine, 'last_info', None) or {}).get('language') or ''
        if chunk['stream'] == 'mic':
            speaker = 'you'
        else:
            speaker, entry = self.tracker.label(chunk['audio'])
            if entry is not None:
                self.on_speaker(entry)
        self.on_turn({'t0': chunk['t0'], 't1': chunk['t1'], 'stream': chunk['stream'],
                      'speaker': speaker, 'text': text, 'lang': lang})
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... -m unittest tests.test_meeting.TranscriberTests tests.test_meeting.EngineLanguageTests -v` and the full suite (the smoke tests cover `_route` and both engines' `transcribe_audio`; they must stay green). Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git -C S:/Koli-meeting-mode add src/koli/meeting/transcriber.py src/koli/whisper_engine.py src/koli/whisper_engine_cpp.py tests/test_meeting.py
git -C S:/Koli-meeting-mode commit -m "feat(meeting): low-priority transcriber worker; per-call language on the engine

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---
### Task 7: Settings: defaults, schema, appliers, Simple mode, mock data

**Files:**
- Modify: `src/koli/config.defaults.yaml` (new `meeting:` section after `overlay:`; `overlay.hide_from_sharing`; `hotkey.meeting_hotkey`), `src/koli/settings/schema.py` (`SECTIONS` line 48, entries after the overlay block, `MAP_PATHS` line 392), `src/koli/settings/appliers.py` (`_LISTENER_KEYS`, `_READ_ON_USE`, new `meeting` handler, `_PREFIX_HANDLERS`), `src/koli/settings/simple_mode.py` (`to_patch`, `from_settings`), `src/koli/ui/web/js/screens/simple.js:108`, `src/koli/main.py:765` and `src/koli/settings/appliers.py:120` (speaker models into the registry), `tests/test_smoke.py:3728` (round-trip case), `src/koli/ui/web/js/mock-data.json` (regenerated)
- Test: `tests/test_meeting.py`, existing `SettingsSchemaTests` and `SimpleModeRoundTripTests`

**Interfaces:**
- Produces: config keys exactly as in the spec table plus `meeting.speaker_models`; `Appliers.meeting(path, value, old)` forwards to `ctx.meetings.apply_setting(path, value)` when `ctx.meetings` exists, and `overlay.hide_from_sharing` / `meeting.panel.*` to `ctx.bar.host.apply_capture_flags()` / `apply_panel_settings(path)` (Task 9 defines those on the host; guard with `getattr`). Simple question `calls` with answers `auto|ask|off`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meeting.py`:

```python
class MeetingSettingsTests(unittest.TestCase):
    def defaults(self):
        import yaml
        from koli.config_manager import _resolve_platform_values
        return _resolve_platform_values(yaml.safe_load((ROOT / 'src' / 'koli' / 'config.defaults.yaml').read_text(encoding='utf-8')))

    def test_defaults_match_the_spec(self):
        d = self.defaults()['meeting']
        self.assertTrue(d['enabled'])
        self.assertEqual(d['detect'], 'auto')
        self.assertTrue(d['include_me'])
        self.assertEqual(d['gap_minutes'], 10)
        self.assertEqual(d['start_speech_seconds'], 3)
        self.assertEqual(d['language'], 'auto')
        self.assertEqual(d['model'], '')
        self.assertEqual(d['speaker_model'], 'eres2net')
        self.assertIn('eres2net', d['speaker_models'])
        self.assertEqual(d['panel']['opacity'], 85)
        self.assertEqual(d['panel']['position'], 'right-edge')
        self.assertEqual((d['panel']['width'], d['panel']['height']), (380, 420))
        self.assertTrue(d['panel']['hide_from_sharing'])
        self.assertFalse(self.defaults()['overlay']['hide_from_sharing'])
        self.assertEqual(self.defaults()['hotkey']['meeting_hotkey'], '')

    def test_schema_section_and_simple_question(self):
        from koli.settings import schema, simple_mode
        self.assertIn('meetings', [s['id'] for s in schema.SECTIONS])
        self.assertEqual(schema.get('meeting.detect').choices, [['auto', 'Automatically'], ['ask', 'Ask me first'], ['off', 'Never']])
        self.assertEqual(schema.get('meeting.panel.custom_x').mode, schema.HIDDEN)
        self.assertEqual(simple_mode.to_patch('calls', 'ask'), {'meeting.detect': 'ask'})
        self.assertEqual(simple_mode.from_settings(lambda p: {'meeting.detect': 'off'}.get(p))['calls'], 'off')

    def test_appliers_route_meeting_keys(self):
        from koli.settings.appliers import _PREFIX_HANDLERS, _LISTENER_KEYS, Appliers
        self.assertIn('hotkey.meeting_hotkey', _LISTENER_KEYS)
        self.assertTrue(any(prefix == 'meeting.' and fn is Appliers.meeting for prefix, fn in _PREFIX_HANDLERS))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... -m unittest tests.test_meeting.MeetingSettingsTests -v`. Expected: `KeyError: 'meeting'`.

- [ ] **Step 3: Add the settings**

`src/koli/config.defaults.yaml`: inside `overlay:` after `click_through: true` add:

```yaml
    # Keep the bubble out of screen sharing and recordings (you still see it)
    hide_from_sharing: false
```

After the `overlay:` section (before `postprocess:`) add:

```yaml
meeting: # Meetings: calls in Discord, Zoom, Teams and the browser
    # Notice calls and transcribe them. Off hooks nothing; meetings can
    # still be started by hand from the Meetings screen.
    enabled: true
    # When a listed app has a call with sound: auto starts a meeting, ask
    # offers one in the panel, off ignores it. Each app rule can override
    # this with its own `meeting:` value (Apps screen).
    detect: auto
    # Your own side, from the microphone, as "You". The panel's Exclude me
    # button turns it off for a while.
    include_me: true
    # Sound back from the same call within this many minutes resumes the
    # meeting; later, or from another call, starts a new one
    gap_minutes: 10
    # Seconds of speech heard from the call before a meeting starts (so a
    # notification ping never starts one)
    start_speech_seconds: 3
    # Language of the meetings: auto for mixed (Hebrew and English in one
    # call), or a code to force one
    language: auto
    # A separate Whisper model for meetings (a key from whisper.models), or
    # empty for the dictation model. Only worth it on a GPU.
    model: ''
    # Speaker voiceprint model (meeting.speaker_models)
    speaker_model: eres2net
    # Speaker-embedding models: one ONNX file each from a Hugging Face repo
    speaker_models:
        eres2net:
            source: csukuangfj/speaker-embedding-models
            label: "3D-Speaker ERes2Net (26MB)"
            files:
                model: 3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx
    panel: # The dark panel that shows the conversation
        # How solid it is, 40 to 100 (percent)
        opacity: 85
        # Same choices as the bubble: right-edge | left-edge | top-* |
        # bottom-* | custom (where you dragged it)
        position: right-edge
        # Where you last dropped it (managed by the app; physical pixels)
        custom_x: null
        custom_y: null
        # Size in page pixels; drag the corner handle to change it
        width: 380
        height: 420
        # Shrunk to a small pill (managed by the app)
        collapsed: false
        # Keep the panel out of screen sharing and recordings. The panel's
        # menu can show it for one meeting.
        hide_from_sharing: true
```

In the `hotkey:` section, after `paste_last_hotkey`, add:

```yaml
    # Start a meeting now, or end the one running. Leave empty to disable.
    meeting_hotkey: ""
```

`src/koli/settings/schema.py`:

In `SECTIONS`, after the `overlay` entry add:

```python
    {'id': 'meetings', 'title': 'Meetings', 'desc': 'Calls in Discord, Zoom, Teams and the browser: when Koli listens, who said what, and the panel.'},
```

After the overlay entries (before `# ── Privacy & logs`) add:

```python
    _S('overlay.hide_from_sharing', 'Hide the bubble from screen sharing', 'overlay', 'toggle',
       help='Shared screens and recordings leave it out; you still see it.'),

    # ── Meetings ─────────────────────────────────────────────────────────────
    _S('meeting.enabled', 'Notice calls', 'meetings', 'toggle', mode=SIMPLE,
       help='Off hooks nothing. Meetings can still be started by hand from the Meetings screen.'),
    _S('meeting.detect', 'When a call has sound', 'meetings', 'segmented', mode=SIMPLE,
       choices=[['auto', 'Automatically'], ['ask', 'Ask me first'], ['off', 'Never']],
       help='Each app can override this on the Apps screen.'),
    _S('meeting.include_me', 'Include my side', 'meetings', 'toggle',
       help='Your microphone becomes "You" in the transcript. The panel\'s Exclude me button pauses it.'),
    _S('meeting.gap_minutes', 'Resume within', 'meetings', 'number', min=1, max=60, unit='min',
       help='Sound back from the same call within this time resumes the meeting; later starts a new one.'),
    _S('meeting.start_speech_seconds', 'Speech before a meeting starts', 'meetings', 'number', min=1, max=10, unit='s',
       help='So a notification ping never starts one.'),
    _S('meeting.language', 'Meeting language', 'meetings', 'select', choices=LANGUAGES, apply=NEXT_RECORDING,
       help='Auto-detect handles Hebrew and English in one call.'),
    _S('meeting.model', 'Meeting model', 'meetings', 'text', apply=BACKGROUND,
       help='A model key from the Model screen, or empty for the dictation model. Only worth it on a GPU.'),
    _S('meeting.speaker_model', 'Voiceprint model', 'meetings', 'select', apply=BACKGROUND,
       choices=[['eres2net', '3D-Speaker ERes2Net (26 MB)']], help='Tells speakers apart. Downloaded at the first meeting.'),
    _S('meeting.speaker_models', 'Voiceprint models', 'meetings', 'readonly', help='Defined in user_settings.yaml.'),
    _S('meeting.panel.opacity', 'Panel opacity', 'meetings', 'range', min=40, max=100, step=5, unit='%', group='Panel'),
    _S('meeting.panel.position', 'Panel position', 'meetings', 'segmented', choices=POSITIONS, group='Panel',
       help='Or drag the panel by its header.'),
    _S('meeting.panel.width', 'Panel width', 'meetings', 'number', min=240, max=1200, unit='px', group='Panel'),
    _S('meeting.panel.height', 'Panel height', 'meetings', 'number', min=160, max=1600, unit='px', group='Panel'),
    _S('meeting.panel.hide_from_sharing', 'Hide the panel from screen sharing', 'meetings', 'toggle', group='Panel',
       help='Shared screens and recordings leave it out; you still see it. The panel\'s menu can show it for one meeting.'),
    _S('meeting.panel.custom_x', 'Panel spot x', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN, nullable=True),
    _S('meeting.panel.custom_y', 'Panel spot y', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN, nullable=True),
    _S('meeting.panel.collapsed', 'Panel collapsed', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN),
```

In the hotkeys block, after `hotkey.paste_last_hotkey`:

```python
    _S('hotkey.meeting_hotkey', 'Start or end a meeting', 'hotkeys', 'hotkey', nullable=True),
```

In `MAP_PATHS` add `'meeting.speaker_models'`.

`src/koli/settings/appliers.py`:

- `_LISTENER_KEYS`: add `'hotkey.meeting_hotkey': 'meeting_hotkey'`.
- `_READ_ON_USE`: add `'meeting.panel.custom_x', 'meeting.panel.custom_y', 'meeting.panel.collapsed', 'meeting.speaker_models'`.
- `AppContext.__init__`: add keyword `meetings=None` and `self.meetings = meetings`.
- `models_map`: pass `speaker_models_config=self.cm.get_path('meeting.speaker_models') or {}` to `ModelRegistry(...)`.
- New handler after `overlay`:

```python
    # Meeting settings reach the running service; the panel's look goes to
    # the UI host, which owns that window.
    def meeting(self, path, value, old):
        service = getattr(self.ctx, 'meetings', None)
        if service is not None:
            service.apply_setting(path, value)
        host = getattr(self.ctx.bar, 'host', None)
        if host is not None and path.startswith('meeting.panel.'):
            apply = getattr(host, 'apply_panel_setting', None)
            if apply:
                apply(path)
        if path in ('meeting.model', 'meeting.speaker_model'):
            return self._live()   # read at the next meeting start
```

- In `overlay`, add a branch:

```python
        elif path == 'overlay.hide_from_sharing':
            host = getattr(bar, 'host', None)
            if host is not None and hasattr(host, 'apply_capture_flags'):
                host.apply_capture_flags()
```

- `_PREFIX_HANDLERS`: add `('meeting.', Appliers.meeting)`.

`src/koli/settings/simple_mode.py`: in `to_patch` before the `raise`:

```python
    if question == 'calls':
        return {'meeting.detect': answer if answer in ('auto', 'ask', 'off') else 'auto'}
```

and in `from_settings` answers dict: `'calls': get('meeting.detect') or 'auto',`.

`src/koli/ui/web/js/screens/simple.js`: in the "App" card after the bubble line add:

```javascript
      <${Q} title="Transcribe my calls?" help="Discord, Zoom, Teams and Google Meet. The panel shows who said what; nothing leaves this computer.">
        <${Options} name="Calls" value=${a.calls} onPick=${(v) => set('calls', v)}
          options=${[['auto', 'Yes, automatically', 'Starts when a call has sound.'], ['ask', 'Ask me first', 'A small prompt when a call starts.'], ['off', 'No', 'Never listens to calls.']]} /></${Q}>
```

(`Q` and `Options` are the components already used higher in the file for the text-style question.)

`tests/test_smoke.py:3728`: add `('calls', 'ask'), ('calls', 'off')` to the round-trip cases.

`src/koli/main.py:765`: pass `speaker_models_config=(config_manager.config.get('meeting') or {}).get('speaker_models') or {}` to `ModelRegistry(...)`.

Regenerate the mock data: `cd S:/Koli-meeting-mode && PYTHONPATH=S:/Koli-meeting-mode/src S:/Koli/.venv/Scripts/python.exe tools/make_mock_data.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... -m unittest tests.test_meeting.MeetingSettingsTests -v` and the full suite. Expected: all pass, including `SettingsSchemaTests` (every new key has an entry) and `SimpleModeRoundTripTests`.

- [ ] **Step 5: Commit**

```bash
git -C S:/Koli-meeting-mode add src/koli/config.defaults.yaml src/koli/settings/schema.py src/koli/settings/appliers.py src/koli/settings/simple_mode.py src/koli/ui/web/js/screens/simple.js src/koli/main.py src/koli/ui/web/js/mock-data.json tests/test_meeting.py tests/test_smoke.py
git -C S:/Koli-meeting-mode commit -m "feat(meeting): Meetings settings section, simple-mode question, appliers and hotkey key

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: MeetingService, wiring, bridge, events, hotkey, tray, loopback extra

**Files:**
- Create: `src/koli/services/meetings.py`
- Modify: `src/koli/ui/events.py:16` (`TOPICS`), `src/koli/services/system.py:223` (`EXTRAS`), `src/koli/hotkey_listener.py` (constructor, `_setup_hotkeys`, `change_hotkey_config`), `src/koli/state_manager.py` (`toggle_meeting`), `src/koli/main.py:408` (`setup_hotkey_listener`) and `:825` (build the service), `src/koli/system_tray.py:404` (`_create_app_window_menu`), `src/koli/ui/host.py:150` (`attach`), `src/koli/ui/bridge.py` (new `meeting_*` methods)
- Test: `tests/test_meeting.py`

**Interfaces:**
- Consumes: Tasks 1–7.
- Produces: `class MeetingService(ctx)` with `start_detector()`, `stop_detector()`, `apply_setting(path, value)`, `state() -> dict`, `turns(meeting_id) -> list`, `start()`, `end()`, `new()`, `toggle()`, `pause()`, `resume()`, `set_excluded(on)`, `offer(accept)`, `rename_speaker(id, name)`, `continue_previous()`, `split_resumed()`, `list()`, `get(id)`, `delete(id)`, `export(id, fmt) -> str`, `shutdown()`; `StateManager.toggle_meeting()`; tray attributes `on_meeting_toggle`, `meeting_live`; bridge methods `meeting_state`, `meeting_start`, `meeting_end`, `meeting_new`, `meeting_pause`, `meeting_resume`, `meeting_exclude(on)`, `meeting_offer(accept)`, `meeting_rename(speaker_id, name)`, `meeting_continue_previous`, `meeting_split`, `meetings_list`, `meeting_get(id)`, `meeting_delete(id)`, `meeting_export(id, fmt)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meeting.py`:

```python
class FakeStateManager:
    def __init__(self):
        self.recording = False
        self.processing = False
        self.whisper_engine = FakeEngine(['hello'])
        self.app_rules = type('R', (), {'rules': RULES})()
        self.audio_recorder = type('Rec', (), {'add_tap': lambda s, f: None, 'remove_tap': lambda s, f: None})()

    def is_idle(self):
        return not (self.recording or self.processing)

    def is_transcription_recording(self):
        return self.recording


class FakeConfig:
    def __init__(self, values):
        self.values = values

    def get_path(self, path, default=None):
        return self.values.get(path, default)


class ServiceTests(unittest.TestCase):
    def make(self, enabled=True):
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        values = {'meeting.enabled': enabled, 'meeting.detect': 'auto', 'meeting.include_me': True,
                  'meeting.gap_minutes': 10, 'meeting.start_speech_seconds': 3, 'meeting.language': 'auto',
                  'meeting.model': '', 'meeting.speaker_model': 'eres2net'}
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig(values)
        ctx.model_registry = None
        self.events = []
        svc = MeetingService(ctx, store=MeetingStore(Path(self.tmp.name)), publish=lambda t, p: self.events.append((t, p)),
                             loopback_factory=lambda on_block, on_error: type('L', (), {'start': lambda s: None, 'stop': lambda s: None})(),
                             embedder_factory=lambda: None)
        self.addCleanup(svc.shutdown)
        return svc

    def test_manual_start_and_end_round_trip(self):
        svc = self.make()
        self.assertEqual(svc.state()['state'], 'idle')
        svc.start()
        st = svc.state()
        self.assertEqual(st['state'], 'live')
        svc.end()
        self.assertEqual(svc.state()['state'], 'idle')
        heads = svc.list()
        self.assertEqual(len(heads), 1)
        self.assertEqual(svc.get(heads[0]['id'])['turns'], [])
        self.assertIn('[', svc.export(heads[0]['id'], 'txt'))
        self.assertTrue(svc.delete(heads[0]['id']))

    def test_toggle_starts_then_ends(self):
        svc = self.make()
        svc.toggle()
        self.assertEqual(svc.state()['state'], 'live')
        svc.toggle()
        self.assertEqual(svc.state()['state'], 'idle')

    def test_detector_runs_only_when_enabled(self):
        svc = self.make(enabled=False)
        self.assertIsNone(svc.detector)
        svc.apply_setting('meeting.enabled', True)
        self.assertIsNotNone(svc.detector)
        svc.apply_setting('meeting.enabled', False)
        self.assertIsNone(svc.detector)

    def test_mic_blocks_are_dropped_while_dictating_or_excluded(self):
        svc = self.make()
        svc.start()
        self.assertTrue(svc._mic_allowed())
        svc.ctx.state_manager.recording = True
        self.assertFalse(svc._mic_allowed())
        svc.ctx.state_manager.recording = False
        svc.set_excluded(True)
        self.assertFalse(svc._mic_allowed())
        svc.set_excluded(False)
        svc.apply_setting('meeting.include_me', False)
        self.assertFalse(svc._mic_allowed())

    def test_events_topics_registered(self):
        from koli.ui.events import TOPICS
        self.assertIn('meeting', TOPICS)
        self.assertIn('meeting_turn', TOPICS)

    def test_loopback_extra_is_installable(self):
        from koli.services.system import EXTRAS
        self.assertEqual(EXTRAS['loopback'][0], 'soundcard')
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... -m unittest tests.test_meeting.ServiceTests -v`. Expected: `ModuleNotFoundError: koli.services.meetings`.

- [ ] **Step 3: Write the service and wire it**

`src/koli/services/meetings.py`:

```python
# services/meetings.py
# Meeting mode, assembled: the detector feeds the session manager, a live
# meeting opens the loopback stream and the microphone tap, their chunks go
# to the transcriber, and turns come back into the meeting. This is the one
# object the bridge, the tray and the hotkey talk to. Nothing here touches
# the windows: the panel listens to the 'meeting' and 'meeting_turn' events.

import logging
import threading
from typing import Callable, Optional

from ..meeting.capture import Chunker, LoopbackStream, MicTap, speech_prob_function
from ..meeting.detector import MeetingDetector
from ..meeting.session import SessionManager
from ..meeting.speakers import SpeakerTracker, make_embedder
from ..meeting.store import MeetingStore
from ..meeting.transcriber import MeetingTranscriber
from ..ui.events import publish as publish_event, report_problem
from ..ui.tasks import get_task_manager

logger = logging.getLogger(__name__)

LOOPBACK_HINT = "Meetings need the loopback package. Install it from Settings → Meetings, or: pip install koli[loopback]"


class MeetingService:
    def __init__(self, ctx, store: Optional[MeetingStore] = None, publish: Callable = None,
                 loopback_factory: Callable = None, embedder_factory: Callable = None):
        self.ctx = ctx
        self.store = store or MeetingStore()
        self._publish = publish or publish_event
        self._loopback_factory = loopback_factory or (lambda on_block, on_error: LoopbackStream(on_block, on_error))
        self._embedder_factory = embedder_factory or self._load_embedder
        self._lock = threading.Lock()
        self.detector: Optional[MeetingDetector] = None
        self._loopback = None
        self._mic_tap = None
        self._loop_chunker = None
        self._mic_chunker = None
        self._speech_prob = None
        self.tracker = SpeakerTracker(embed=None)
        self._embedder_task = None
        self.session = SessionManager(self.store, start_capture=self._start_capture, stop_capture=self._stop_capture,
                                      publish=self._publish, settings=self._setting)
        self.transcriber = MeetingTranscriber(
            engine_provider=lambda: self.ctx.state_manager.whisper_engine,
            is_busy=lambda: not self.ctx.state_manager.is_idle(), tracker=self.tracker,
            language_provider=lambda: str(self._setting('meeting.language', 'auto') or 'auto'),
            on_turn=self.session.add_turn, on_speaker=self.session.ensure_speaker,
            on_pending=self._on_pending, on_behind=self._on_behind)
        self.transcriber.start()
        if self._setting('meeting.enabled', True):
            self.start_detector()

    # ── Settings ─────────────────────────────────────────────────────────────

    def _setting(self, path: str, default=None):
        value = self.ctx.config_manager.get_path(path, default)
        return default if value is None else value

    def apply_setting(self, path: str, value):
        if path == 'meeting.enabled':
            if value:
                self.start_detector()
            else:
                self.stop_detector()
        # Everything else is read when it is used.

    # ── Detector ─────────────────────────────────────────────────────────────

    def start_detector(self):
        with self._lock:
            if self.detector is not None:
                return
            self.detector = MeetingDetector(
                on_sources=self.session.on_sources,
                rules_provider=lambda: list(getattr(self.ctx.state_manager.app_rules, 'rules', []) or []),
                mode_provider=lambda: str(self._setting('meeting.detect', 'auto') or 'auto'))
            self.detector.start()
        logger.info("Meeting detection on")

    def stop_detector(self):
        with self._lock:
            if self.detector is None:
                return
            self.detector.stop()
            self.detector = None
        self.session.on_sources([])
        logger.info("Meeting detection off")

    # ── Capture ──────────────────────────────────────────────────────────────

    # The microphone counts when it is wanted, you aren't dictating, and you
    # haven't excluded yourself.
    def _mic_allowed(self) -> bool:
        sm = self.ctx.state_manager
        return bool(self._setting('meeting.include_me', True)) and not self.session.excluded \
            and not sm.is_transcription_recording()

    def _start_capture(self, meeting: dict):
        self._ensure_embedder()
        self.tracker.load(meeting.get('speakers', []))
        if self._speech_prob is None:
            self._speech_prob = speech_prob_function()
        self._loop_chunker = Chunker('loopback', self._speech_prob, self.transcriber.submit, on_speech=self.session.on_speech)
        self._mic_chunker = Chunker('mic', self._speech_prob, self.transcriber.submit)
        self._loopback = self._loopback_factory(self._loop_chunker.feed, self._loopback_failed)
        self._loopback.start()
        self._mic_tap = MicTap(lambda: self.ctx.state_manager.audio_recorder, self._mic_chunker.feed, self._mic_allowed)
        self._mic_tap.start()

    def _stop_capture(self):
        if self._loopback is not None:
            self._loopback.stop()
            self._loopback = None
        if self._mic_tap is not None:
            self._mic_tap.stop()
            self._mic_tap = None
        for chunker in (self._loop_chunker, self._mic_chunker):
            if chunker is not None:
                chunker.flush()
        self._loop_chunker = self._mic_chunker = None

    def _loopback_failed(self, message: str):
        if 'soundcard' in message.lower() or 'no module' in message.lower():
            report_problem(LOOPBACK_HINT)
        else:
            report_problem(message)

    # The speech heard before a meeting starts also comes from the loopback,
    # so the detector's "wait for speech" needs a stream while idle. Rather
    # than keep one open all the time, the idle listener runs only while a
    # source is present.
    def _on_pending(self, stream):
        if self.session.meeting:
            self._publish('meeting_turn', {'id': self.session.meeting['id'], 'pending': {'stream': stream} if stream else None})

    def _on_behind(self, seconds: float):
        self.session.set_behind(seconds)

    # ── Speaker model ────────────────────────────────────────────────────────

    def _load_embedder(self):
        registry = self.ctx.model_registry
        key = str(self._setting('meeting.speaker_model', 'eres2net') or 'eres2net')
        if registry is None:
            return None
        path = registry.get_speaker_model_path(key)
        return make_embedder(path) if path else None

    # Loads the voiceprint model in the background the first time; turns
    # transcribed before it is ready are "Speaker ?".
    def _ensure_embedder(self):
        if self.tracker.embed is not None or self._embedder_task is not None:
            return

        def job(reporter):
            reporter.report(None, 'Downloading the voiceprint model')
            embed = self._embedder_factory()
            if embed is None:
                raise RuntimeError("The voiceprint model could not be loaded; speakers stay unnamed")
            self.tracker.embed = embed
            return 'ready'

        def done(task):
            self._embedder_task = None
            if task.status == 'failed':
                logger.warning(f"Voiceprint model: {task.error}")
        try:
            self._embedder_task = get_task_manager().run('Preparing speaker recognition', job, kind='meeting', on_done=done)
        except Exception as e:
            logger.debug(f"Speaker model task not started: {e}")
            self._embedder_task = None

    # ── Commands (bridge, tray, hotkey) ──────────────────────────────────────

    def state(self) -> dict:
        return self.session.state_payload()

    def turns(self, meeting_id: str) -> list:
        m = self.session.meeting
        if m and m['id'] == meeting_id:
            return list(m['turns'])
        return self.store.load(meeting_id)['turns']

    def start(self):
        self.session.start()

    def end(self):
        self.session.end()
        self.transcriber.clear()

    def new(self):
        self.session.new()

    def toggle(self):
        if self.session.state in ('live', 'paused'):
            self.end()
        else:
            self.start()

    def pause(self):
        self.session.pause()

    def resume(self):
        self.session.resume()

    def set_excluded(self, on: bool):
        self.session.set_excluded(bool(on))

    def offer(self, accept: bool):
        if accept:
            self.session.offer_accept()
        else:
            self.session.offer_decline()

    def rename_speaker(self, speaker_id: str, name: str):
        m = self.session.meeting
        if m:
            self.session.rename_speaker(speaker_id, name)

    def rename_saved_speaker(self, meeting_id: str, speaker_id: str, name: str):
        m = self.store.load(meeting_id)
        for s in m['speakers']:
            if s['id'] == speaker_id:
                s['name'] = str(name).strip()[:40] or s['name']
        self.store.save(m)

    def continue_previous(self):
        self.session.continue_previous()

    def split_resumed(self):
        self.session.split_resumed()

    def list(self) -> list:
        return self.store.list()

    def get(self, meeting_id: str) -> dict:
        m = self.session.meeting
        if m and m['id'] == meeting_id:
            return {k: v for k, v in m.items()}
        return self.store.load(meeting_id)

    def delete(self, meeting_id: str) -> bool:
        m = self.session.meeting
        if m and m['id'] == meeting_id:
            raise ValueError("End the meeting before deleting it")
        return self.store.delete(meeting_id)

    def export(self, meeting_id: str, fmt: str) -> str:
        return self.store.export(meeting_id, fmt)

    def shutdown(self):
        self.stop_detector()
        if self.session.state in ('live', 'paused'):
            self.session.end()
        self.transcriber.stop()
```

A note on `_mic_allowed` with the fake state manager in tests: it calls `is_transcription_recording()`, which the fake provides.

`src/koli/ui/events.py`: add to `TOPICS`:

```python
    'meeting',        # {'state', 'id', 'title', ...} the meeting panel's whole state, see SessionManager.state_payload
    'meeting_turn',   # {'id', 'turn'} a transcribed turn; {'id', 'pending'} one being transcribed; {'id', 'reload': True}
```

`src/koli/services/system.py` `EXTRAS`: add `'loopback': ('soundcard', ['soundcard>=0.4.2']),`.

`src/koli/hotkey_listener.py`:
- constructor: add `meeting_hotkey: str = None` parameter and `self.meeting_hotkey = meeting_hotkey`.
- `_setup_hotkeys`, after the pause binding:

```python
        if self.meeting_hotkey:
            hotkey_configs.append({
                'combination': self.meeting_hotkey,
                'callback': self._meeting_hotkey_pressed,
                'name': 'meeting'
            })
```

- new method next to `_pause_hotkey_pressed`:

```python
    # Start a meeting, or end the one running. Off the hotkey thread: ending
    # flushes the meeting file.
    def _meeting_hotkey_pressed(self):
        import threading
        self.logger.info("Meeting hotkey pressed")
        threading.Thread(target=self.state_manager.toggle_meeting, daemon=True, name='meeting-toggle').start()
```

- `change_hotkey_config`: add `'meeting_hotkey'` to `valid_settings`.

`src/koli/state_manager.py`: in `__init__` add `self.meetings = None   # MeetingService, set by main when the app window runs`; add near `set_hands_free`:

```python
    # The meeting hotkey and the tray: start a meeting or end the running one.
    def toggle_meeting(self) -> bool:
        if self.meetings is None:
            self.system_tray.notify("Meetings need the app window (not --legacy-ui)")
            return False
        self.meetings.toggle()
        return True
```

`src/koli/main.py`:
- `setup_hotkey_listener`: add `meeting_hotkey=hotkey_config.get('meeting_hotkey'),`.
- Right after `ui_host.attach()` (inside `if use_ui:`):

```python
            try:
                from .services.meetings import MeetingService
                ctx.meetings = MeetingService(ctx)
                state_manager.meetings = ctx.meetings
            except Exception as e:
                logger.error(f"Meeting mode not started: {e}", exc_info=True)
```

and in the shutdown path (find where `state_manager.shutdown()` or the tray stops at the end of `main`) add `if getattr(state_manager, 'meetings', None): state_manager.meetings.shutdown()` before it.

`src/koli/system_tray.py`: `__init__` add `self.on_meeting_toggle = None` and `self.meeting_live = None`; in `_create_app_window_menu` after the pause item:

```python
        if self.on_meeting_toggle:
            live = bool(self.meeting_live and self.meeting_live())
            items.append(pystray.MenuItem("End meeting" if live else "Start meeting",
                                          lambda icon, item: self.on_meeting_toggle()))
```

`src/koli/ui/host.py` `attach()`: after `tray.on_pause = ...`:

```python
            tray.on_meeting_toggle = lambda: self.ctx.meetings.toggle() if getattr(self.ctx, 'meetings', None) else None
            tray.meeting_live = lambda: getattr(self.ctx, 'meetings', None) is not None \
                and self.ctx.meetings.state()['state'] in ('live', 'paused')
```

Since `ctx.meetings` is created after `attach()`, the lambdas look it up at call time. Also subscribe the tray to refresh on meeting state: in `_on_event`, `if topic == 'meeting' and tray: tray.refresh_menu()` guarded by try/except (do it only when `payload['state']` changed from the last one you saw, keep `self._meeting_state` on the host).

`src/koli/ui/bridge.py`: add a section before `# ── App rules and profiles`:

```python
    # ── Meetings ─────────────────────────────────────────────────────────────

    @property
    def _meetings(self):
        svc = getattr(self._host.ctx, 'meetings', None)
        if svc is None:
            raise ValueError("Meeting mode is not running")
        return svc

    @_api
    def meeting_state(self):
        return self._meetings.state()

    @_api
    def meeting_start(self):
        self._meetings.start()

    @_api
    def meeting_end(self):
        self._meetings.end()

    @_api
    def meeting_new(self):
        self._meetings.new()

    @_api
    def meeting_pause(self):
        self._meetings.pause()

    @_api
    def meeting_resume(self):
        self._meetings.resume()

    @_api
    def meeting_exclude(self, on):
        self._meetings.set_excluded(bool(on))

    @_api
    def meeting_offer(self, accept):
        self._meetings.offer(bool(accept))

    @_api
    def meeting_rename(self, speaker_id, name, meeting_id=None):
        svc = self._meetings
        current = svc.state().get('id')
        if meeting_id and meeting_id != current:
            svc.rename_saved_speaker(meeting_id, speaker_id, name)
        else:
            svc.rename_speaker(speaker_id, name)

    @_api
    def meeting_continue_previous(self):
        self._meetings.continue_previous()

    @_api
    def meeting_split(self):
        self._meetings.split_resumed()

    @_api
    def meetings_list(self):
        return self._meetings.list()

    @_api
    def meeting_get(self, meeting_id):
        return self._meetings.get(meeting_id)

    @_api
    def meeting_delete(self, meeting_id):
        return self._meetings.delete(meeting_id)

    # Writes the export where the user picks; returns the path or None.
    @_api
    def meeting_export(self, meeting_id, fmt='txt'):
        text = self._meetings.export(meeting_id, fmt)
        path = self._host.pick('save', 'Export meeting', filename=f'{meeting_id}.{fmt}')
        if not path:
            return None
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... -m unittest tests.test_meeting.ServiceTests -v` and the full suite. Expected: all pass. Then a launch check: `cd S:/Koli-meeting-mode && PYTHONPATH=S:/Koli-meeting-mode/src S:/Koli/.venv/Scripts/python.exe -m koli --test --open` (a second instance under `--test`); look in the log for "Meeting detection on" and no traceback; quit with the tray.

- [ ] **Step 5: Commit**

```bash
git -C S:/Koli-meeting-mode add src/koli/services/meetings.py src/koli/ui/events.py src/koli/services/system.py src/koli/hotkey_listener.py src/koli/state_manager.py src/koli/main.py src/koli/system_tray.py src/koli/ui/host.py src/koli/ui/bridge.py tests/test_meeting.py
git -C S:/Koli-meeting-mode commit -m "feat(meeting): meeting service wired to the app: bridge, tray, hotkey, loopback extra

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---
### Task 9: The panel window and page

**Files:**
- Create: `src/koli/ui/web/meeting.html`, `src/koli/ui/web/css/meeting.css`, `src/koli/ui/web/js/meeting.js`
- Modify: `src/koli/ui/win32.py` (new `exclude_from_capture`), `src/koli/ui/host.py` (constants, `__init__`, `run`, `_on_loaded`, `quit`, `_on_event`, `_on_setting`, `_pump`, `bootstrap`, new panel section), `src/koli/ui/bridge.py` (`meeting_resize`, `meeting_drag`, `meeting_collapse`, `meeting_show_in_sharing`)
- Test: `tests/test_meeting.py` (placement maths only; the window is checked by launching)

**Interfaces:**
- Consumes: `floating_bar.bar_origin`, `dropped_spot`, `win32.*`, events `meeting` / `meeting_turn`, bridge `meeting_*` from Task 8.
- Produces: `win32.exclude_from_capture(hwnd, on) -> bool`; host methods `place_panel()`, `resize_panel(width, height)`, `set_panel_collapsed(collapsed, width, height)`, `drag_panel(seq, dx, dy, done)`, `apply_panel_setting(path)`, `apply_capture_flags()`, `show_panel_in_sharing(on)`; `panel_geometry(settings_get, size, scale, area, collapsed) -> (x, y, w, h)` pure helper in `floating_bar.py`; bridge `meeting_resize(width, height)`, `meeting_drag(seq, dx, dy, done)`, `meeting_collapse(collapsed, width, height)`, `meeting_show_in_sharing(on)`; `app_bootstrap('meeting')` returns `{settings, meeting, turns}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_meeting.py`:

```python
class PanelPlacementTests(unittest.TestCase):
    def test_panel_docks_right_lower_half_and_clamps(self):
        from koli.ui.floating_bar import panel_origin
        area = (0, 0, 1920, 1040)
        x, y = panel_origin(area, (380, 420), 1.0, 'right-edge', None)
        self.assertEqual(x, 1920 - 380 - 6)
        self.assertGreater(y, 1040 // 2 - 1)
        self.assertLessEqual(y + 420, 1040 - 6)
        x, y = panel_origin(area, (380, 420), 1.0, 'custom', (1900, 1000))
        self.assertEqual((x, y), (1920 - 380, 1040 - 420))
        x, y = panel_origin(area, (380, 420), 1.0, 'top-left', None)
        self.assertEqual((x, y), (6, 6))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `... -m unittest tests.test_meeting.PanelPlacementTests -v`. Expected: `ImportError: cannot import name 'panel_origin'`.

- [ ] **Step 3: Write the window side**

`src/koli/ui/floating_bar.py`: add after `is_upright`:

```python
# Top-left corner for the meeting panel of `size`: a side of the screen puts
# it in the lower half with the bubble's margin, a dragged spot is its centre,
# the corners and middles are the bubble's. Never off screen.
def panel_origin(area: tuple, size: tuple, scale: float, position: str, custom: Optional[tuple]) -> tuple:
    left, top, right, bottom = area
    pw, ph = size
    margin = int(EDGE_MARGIN * scale)
    if position == 'custom' and custom:
        return _clamp(custom[0] - pw // 2, custom[1] - ph // 2, area, size)
    if position in EDGE_POSITIONS:
        x = right - pw - margin if position == 'right-edge' else left + margin
        y = bottom - ph - margin
        return _clamp(x, max(y, top + (bottom - top) // 2), area, size)
    return bar_origin(area, size, scale, position if position != 'custom' else 'bottom-right')
```

`src/koli/ui/win32.py`: after `set_opacity`:

```python
# Keep a window out of screen sharing and recordings (Windows 10 2004+). It
# stays visible on the monitor. Older capture tools show a black box instead.
def exclude_from_capture(hwnd: int, on: bool) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
    try:
        return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if on else WDA_NONE))
    except Exception:
        return False
```

`src/koli/ui/host.py`:

Constants:

```python
PANEL_TITLE = 'Koli · meeting'
PANEL_TOPICS = {'meeting', 'meeting_turn', 'settings', 'task', 'problem'}
PANEL_HIDE_DELAY = 2.5
```

Add `'meeting'` to `COALESCE`.

`__init__`: `self.panel = _PageChannel('meeting', PANEL_TOPICS)`, `self.panel_hwnd = 0`, `self._panel_visible = False`, `self._panel_collapsed = False`, `self._panel_pill = (160, 36)`, `self._panel_drag = None`, `self._panel_drag_seq = 0`, `self._panel_hide_timer = None`, `self._panel_shown_in_sharing = False`, `self._meeting_state = 'idle'`.

`run()`: after the bar window is created:

```python
        pw, ph = self._panel_size()
        px, py = self._panel_geometry()[:2]
        self.panel.window = webview.create_window(
            PANEL_TITLE, url=str(WEB_DIR / 'meeting.html'), js_api=self.bridge,
            width=pw, height=ph, x=px, y=py, frameless=True, easy_drag=False, resizable=False, on_top=True,
            focus=False, min_size=(120, 30), shadow=False, background_color='#0B0E11', hidden=True)
        self.panel.window.events.loaded += lambda: self._on_loaded(self.panel)
```

`_on_loaded`: add a branch before the `else`:

```python
        elif channel is self.panel:
            self.panel_hwnd = win32.find_window(PANEL_TITLE)
            win32.make_tool_window(self.panel_hwnd)
            win32.round_corners(self.panel_hwnd)
            self.apply_panel_setting('meeting.panel.opacity')
            self.apply_capture_flags()
            self.place_panel()
            if self._meeting_state in ('offered', 'live', 'paused'):
                self._show_panel()
```

`quit()`: include `self.panel` in the destroy loop.

`_on_event`: after the existing topic handling, add:

```python
        elif topic == 'meeting' and isinstance(payload, dict):
            self._on_meeting_state(payload.get('state', 'idle'))
```

and at the end route to the panel: `if topic in self.panel.topics: self.panel.queue.put(item)`.

`_pump`: iterate `(self.bar, self.main, self.panel)`; a hidden panel still gets its batches (it is small) so the page is current when shown.

`_on_setting`: add `elif path.startswith('meeting.panel.'): self.apply_panel_setting(path)` and `elif path == 'overlay.hide_from_sharing': self.apply_capture_flags()`.

`bootstrap`: add

```python
        elif which == 'meeting':
            svc = getattr(self.ctx, 'meetings', None)
            state = svc.state() if svc else {'state': 'idle'}
            data['meeting'] = state
            data['turns'] = svc.turns(state['id']) if svc and state.get('id') else []
```

New section, after the bar's dragging section:

```python
    # ── Meeting panel ────────────────────────────────────────────────────────

    def _panel_size(self):
        if self._panel_collapsed:
            return self._panel_pill
        cfg = self.settings
        return (max(240, int(cfg.get('meeting.panel.width', 380) or 380)),
                max(160, int(cfg.get('meeting.panel.height', 420) or 420)))

    def _panel_geometry(self):
        scale = win32.dpi_scale(self.panel_hwnd) or 1.0
        w, h = self._panel_size()
        size = (int(w * scale), int(h * scale))
        position = self.settings.get('meeting.panel.position', 'right-edge') or 'right-edge'
        cx, cy = self.settings.get('meeting.panel.custom_x'), self.settings.get('meeting.panel.custom_y')
        custom = (int(cx), int(cy)) if cx is not None and cy is not None else None
        area = win32.work_area(custom) if position == 'custom' and custom else win32.work_area()
        x, y = panel_origin(area, size, scale, position, custom)
        return x, y, size

    def place_panel(self):
        if not self.panel_hwnd or self._panel_drag is not None:
            return
        x, y, size = self._panel_geometry()
        win32.place(self.panel_hwnd, x, y, *size)

    # The meeting's state decides whether the panel is on screen: shown for
    # an offer and while live or paused, hidden a moment after it ends so
    # "Saved" can be read.
    def _on_meeting_state(self, state):
        self._meeting_state = state
        tray = self.ctx.system_tray
        if tray is not None:
            try:
                tray.refresh_menu()
            except Exception:
                pass
        if state in ('offered', 'live', 'paused'):
            self._show_panel()
        elif state == 'ended':
            self._hide_panel_later()
        elif state == 'idle' and self._panel_hide_timer is None:
            self._hide_panel()

    def _show_panel(self):
        if self._panel_hide_timer is not None:
            self._panel_hide_timer.cancel()
            self._panel_hide_timer = None
        if not self.panel_hwnd:
            return
        self._panel_visible = True
        win32.show_no_activate(self.panel_hwnd)
        self.place_panel()

    def _hide_panel_later(self):
        if self._panel_hide_timer is not None:
            self._panel_hide_timer.cancel()
        self._panel_hide_timer = threading.Timer(PANEL_HIDE_DELAY, self._hide_panel)
        self._panel_hide_timer.daemon = True
        self._panel_hide_timer.start()

    def _hide_panel(self):
        self._panel_hide_timer = None
        self._panel_visible = False
        self._panel_shown_in_sharing = False
        self.apply_capture_flags()
        if self.panel_hwnd:
            win32.hide(self.panel_hwnd)

    # The page's corner handle: a new size in page pixels, remembered.
    def resize_panel(self, width, height):
        cm = self.ctx.config_manager
        cm.set_path('meeting.panel.width', max(240, int(width)), save=False)
        cm.set_path('meeting.panel.height', max(160, int(height)))
        self.place_panel()

    # Collapsed to a pill: the page says how big the pill is.
    def set_panel_collapsed(self, collapsed, width=None, height=None):
        self._panel_collapsed = bool(collapsed)
        if width and height:
            self._panel_pill = (int(width), int(height))
        try:
            self.ctx.config_manager.set_path('meeting.panel.collapsed', self._panel_collapsed)
        except Exception:
            pass
        self.place_panel()

    def drag_panel(self, seq, dx, dy, done):
        if not self.panel_hwnd:
            return
        with self._drag_lock:
            if seq <= self._panel_drag_seq:
                return
            self._panel_drag_seq = seq
            if self._panel_drag is None:
                self._panel_drag = win32.window_rect(self.panel_hwnd)
            scale = win32.dpi_scale(self.panel_hwnd) or 1.0
            left, top, right, bottom = self._panel_drag
            x, y = left + int(dx * scale), top + int(dy * scale)
            w, h = right - left, bottom - top
            win32.place(self.panel_hwnd, x, y, w, h)
            if not done:
                return
            self._panel_drag = None
        cm = self.ctx.config_manager
        cm.set_path('meeting.panel.custom_x', x + w // 2, save=False)
        cm.set_path('meeting.panel.custom_y', y + h // 2)
        if self.settings.set('meeting.panel.position', 'custom', source='app').get('applied') == 'unchanged':
            self.place_panel()

    def apply_panel_setting(self, path):
        if not self.panel_hwnd:
            return
        if path == 'meeting.panel.opacity':
            percent = self.settings.get('meeting.panel.opacity', 85) or 85
            win32.set_opacity(self.panel_hwnd, max(0.4, min(1.0, percent / 100.0)))
        elif path == 'meeting.panel.hide_from_sharing':
            self.apply_capture_flags()
        else:
            self.place_panel()

    # Which of Koli's windows screen sharing may show.
    def apply_capture_flags(self):
        if self.panel_hwnd:
            hide = bool(self.settings.get('meeting.panel.hide_from_sharing', True)) and not self._panel_shown_in_sharing
            win32.exclude_from_capture(self.panel_hwnd, hide)
        if self.bar_hwnd:
            win32.exclude_from_capture(self.bar_hwnd, bool(self.settings.get('overlay.hide_from_sharing', False)))

    # The panel's menu: show it to the others for this meeting only.
    def show_panel_in_sharing(self, on):
        self._panel_shown_in_sharing = bool(on)
        self.apply_capture_flags()
```

Import `panel_origin` from `.floating_bar` at the top of host.py.

`src/koli/ui/bridge.py`, in the Meetings section:

```python
    @_api
    def meeting_resize(self, width, height):
        self._host.resize_panel(int(width), int(height))

    @_api
    def meeting_collapse(self, collapsed, width=None, height=None):
        self._host.set_panel_collapsed(bool(collapsed), width, height)
        return {'collapsed': bool(collapsed)}

    @_api
    def meeting_drag(self, seq, dx, dy, done=False):
        self._host.drag_panel(int(seq), float(dx), float(dy), bool(done))

    @_api
    def meeting_show_in_sharing(self, on):
        self._host.show_panel_in_sharing(bool(on))
        return {'shown': bool(on)}
```

- [ ] **Step 4: Write the page**

`src/koli/ui/web/meeting.html` (same CSP and structure as `bar.html`, title `Koli meeting`, stylesheets `css/tokens.css` and `css/meeting.css`, script `js/meeting.js`).

`src/koli/ui/web/css/meeting.css`:

```css
/* meeting.css
   The meeting panel: a dark sheet the OS makes translucent, with the last
   turns of the conversation, one colour per speaker, and the controls that
   change what is captured. Fills its window; the window's size is a setting. */

html, body { margin: 0; height: 100%; background: #0B0E11; color: #E6EAED; overflow: hidden; }
body { font: 450 13.5px/1.4 var(--font); -webkit-font-smoothing: antialiased; -webkit-user-select: none; user-select: none; }
* { box-sizing: border-box; }
button { font: inherit; color: inherit; }
:root { --sp0: #15B4D4; --sp1: #F5A623; --sp2: #7C5CFF; --sp3: #22C55E; --sp4: #EC4899; --sp5: #38BDF8; --sp6: #FB923C; --sp7: #A3E635;
        --dim: #8B949E; --line: rgba(255,255,255,.1); --raise: rgba(255,255,255,.07); }
.ic { width: 16px; height: 16px; flex: none; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; }

/* Layout */
.panel { display: flex; flex-direction: column; height: 100vh; }
.head { display: flex; align-items: center; gap: 8px; padding: 8px 8px 8px 12px; border-bottom: 1px solid var(--line); cursor: grab; }
.head:active { cursor: grabbing; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: #E5484D; flex: none; }
.dot.paused, .dot.offered { background: #F5A623; }
.dot.ended { background: #22C55E; }
.title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.sub { color: var(--dim); font-size: 12px; white-space: nowrap; }
.turns { flex: 1; overflow-y: auto; padding: 8px 12px; display: flex; flex-direction: column; gap: 8px; scrollbar-width: thin; }
.turn { display: grid; grid-template-columns: 88px minmax(0, 1fr); gap: 10px; align-items: start; }
.who { font-weight: 600; font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; border: 0; background: none; padding: 0; text-align: start; }
.who:hover { text-decoration: underline; }
.turn p { margin: 0; white-space: pre-wrap; word-break: break-word; }
.turn.pending p { color: var(--dim); font-style: italic; }
.foot { display: flex; align-items: center; gap: 6px; padding: 8px; border-top: 1px solid var(--line); }
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 30px; min-width: 30px; padding: 0 10px; border: 0; border-radius: 8px; background: var(--raise); color: inherit; font-size: 12.5px; cursor: pointer; white-space: nowrap; }
.btn:hover { background: rgba(255,255,255,.12); }
.btn.on { background: #F5A623; color: #1E1300; }
.btn.icon { padding: 0; width: 30px; }
.btn.pri { background: var(--sp0); color: #0E2A33; }
.grow { flex: 1; }
.notice { display: flex; align-items: center; gap: 8px; padding: 6px 12px; background: var(--raise); font-size: 12.5px; }
.empty { color: var(--dim); text-align: center; margin: auto; padding: 20px; }
.grip { position: absolute; right: 2px; bottom: 2px; width: 14px; height: 14px; cursor: nwse-resize; opacity: .5; }
.menu { position: absolute; top: 40px; right: 8px; background: #161B22; border: 1px solid var(--line); border-radius: 10px; padding: 6px; display: flex; flex-direction: column; gap: 2px; min-width: 220px; z-index: 2; }
.mi { display: flex; align-items: center; gap: 10px; min-height: 34px; padding: 0 10px; border-radius: 8px; border: 0; background: transparent; color: inherit; text-align: start; cursor: pointer; font-size: 13px; }
.mi:hover { background: var(--raise); }
.mi input { margin-inline-start: auto; }

/* The collapsed pill, and the offer */
.pill { display: inline-flex; align-items: center; gap: 8px; height: 36px; padding: 0 12px 0 10px; border-radius: 18px; background: #0B0E11; cursor: pointer; white-space: nowrap; }
.offer { display: flex; flex-direction: column; gap: 10px; padding: 14px; }
.offer .row { display: flex; gap: 8px; justify-content: flex-end; }
```

`src/koli/ui/web/js/meeting.js`:

```javascript
// meeting.js
// The meeting panel: who said what as it happens. Shows an offer when a call
// is detected in "ask" mode, the live transcript with speaker colours, the
// paused and excluded states, the resumed/new notices with their undo, and
// "Saved" at the end. The header drags the window; the corner handle resizes
// it; collapse shrinks it to a pill and reports the pill's size.

import { html, render, useEffect, useLayoutEffect, useRef, useState } from '../vendor/preact-htm.module.js';
import { call, on } from './api.js';
import { createStore } from './store.js';
import { Icon } from './ui.js';

const panel = createStore({
  settings: null, meeting: { state: 'idle' }, turns: [], pending: null, menu: false, collapsed: false,
  shownInSharing: false, stuck: true,   // stuck: the list follows new turns
});

function dir(text) { return /[\u0590-\u05FF\u0600-\u06FF]/.test(text || '') ? 'rtl' : 'ltr'; }
function clock(s) { s = Math.max(0, Math.floor(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }

function wire() {
  on('meeting', (m) => {
    const prev = panel.get().meeting;
    const patch = { meeting: m };
    if (m.id !== prev.id) { patch.turns = []; patch.pending = null; }
    if (m.id && m.id !== prev.id) call('meeting_get', m.id).then((d) => panel.set({ turns: d.turns })).catch(() => {});
    panel.set(patch);
  });
  on('meeting_turn', (p) => {
    const s = panel.get();
    if (p.id !== s.meeting.id) return;
    if (p.reload) { call('meeting_get', p.id).then((d) => panel.set({ turns: d.turns })).catch(() => {}); return; }
    if ('pending' in p) { panel.set({ pending: p.pending }); return; }
    panel.set({ turns: [...s.turns, p.turn], pending: null });
  });
  on('settings', (p) => {
    const s = panel.get().settings;
    if (!s) return;
    const keys = p.path.split('.');
    let node = s;
    keys.slice(0, -1).forEach((k) => { node[k] = node[k] || {}; node = node[k]; });
    node[keys[keys.length - 1]] = p.new;
    panel.set({ settings: { ...s } });
  });
}

// ── Dragging and resizing ─────────────────────────────────────────────────

let seq = 0;
function useDrag(action) {
  const drag = useRef(null);
  const send = (d, done) => call(action, ++seq, d.dx, d.dy, done).catch(() => {});
  const end = () => { const d = drag.current; drag.current = null; if (d && d.moving) { cancelAnimationFrame(d.frame); send(d, true); } };
  return {
    onPointerDown: (e) => { if (e.button !== 0 || e.target.closest('button, input, label')) return; drag.current = { x: e.screenX, y: e.screenY, dx: 0, dy: 0, moving: false, frame: 0, id: e.pointerId }; },
    onPointerMove: (e) => {
      const d = drag.current; if (!d) return;
      d.dx = e.screenX - d.x; d.dy = e.screenY - d.y;
      if (!d.moving) { if (Math.hypot(d.dx, d.dy) < 4) return; d.moving = true; e.currentTarget.setPointerCapture(d.id); }
      if (!d.frame) d.frame = requestAnimationFrame(() => { d.frame = 0; if (drag.current === d) send(d, false); });
    },
    onPointerUp: end, onPointerCancel: end,
  };
}

function Grip() {
  const start = useRef(null);
  return html`<svg class="grip" viewBox="0 0 14 14" aria-label="Resize"
    onPointerDown=${(e) => { start.current = { x: e.screenX, y: e.screenY, w: window.innerWidth, h: window.innerHeight }; e.currentTarget.setPointerCapture(e.pointerId); }}
    onPointerMove=${(e) => { const s = start.current; if (!s) return; call('meeting_resize', s.w + (e.screenX - s.x), s.h + (e.screenY - s.y)).catch(() => {}); }}
    onPointerUp=${() => { start.current = null; }}><path d="M13 1L1 13M13 7l-6 6M13 13h0" stroke="currentColor" stroke-width="1.5" fill="none"/></svg>`;
}

// ── Pieces ────────────────────────────────────────────────────────────────

function Timer({ since, paused }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);
  if (!since) return null;
  return html`<span class="sub">${clock((now - new Date(since).getTime()) / 1000)}${paused ? ' · paused' : ''}</span>`;
}

// Inline edit of the name; never a browser prompt.
function rename(speaker) {
  panel.set({ editing: speaker.id, draft: speaker.name });
}

function Who({ speaker, meeting }) {
  const editing = panel.use((s) => s.editing);
  const draft = panel.use((s) => s.draft);
  if (editing === speaker.id) {
    const commit = () => { call('meeting_rename', speaker.id, draft, meeting.id).catch(() => {}); panel.set({ editing: null }); };
    return html`<input class="who" style="background:var(--raise);border-radius:6px;padding:2px 6px" autofocus value=${draft}
      onInput=${(e) => panel.set({ draft: e.currentTarget.value })} onBlur=${commit}
      onKeyDown=${(e) => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') panel.set({ editing: null }); }} />`;
  }
  return html`<button type="button" class="who" style=${`color:var(--sp${speaker.colour % 8})`} title="Rename" onClick=${() => rename(speaker)}>${speaker.name}</button>`;
}

function Turns() {
  const turns = panel.use((s) => s.turns);
  const pending = panel.use((s) => s.pending);
  const meeting = panel.use((s) => s.meeting);
  const stuck = panel.use((s) => s.stuck);
  const box = useRef(null);
  const speakers = Object.fromEntries((meeting.speakers || []).map((sp) => [sp.id, sp]));
  const who = (id) => speakers[id] || { id, name: id === '?' ? 'Speaker ?' : id, colour: 7 };
  useLayoutEffect(() => { if (stuck && box.current) box.current.scrollTop = box.current.scrollHeight; });
  const onScroll = (e) => { const el = e.currentTarget; panel.set({ stuck: el.scrollHeight - el.scrollTop - el.clientHeight < 24 }); };
  const last = turns.slice(-200);
  return html`<div class="turns" ref=${box} onScroll=${onScroll} aria-live="polite">
    ${last.length === 0 && !pending && html`<div class="empty">Listening…</div>`}
    ${last.map((t) => html`<div class="turn"><${Who} speaker=${who(t.speaker)} meeting=${meeting} /><p dir=${dir(t.text)}>${t.text}</p></div>`)}
    ${pending && html`<div class="turn pending"><span class="who" style=${`color:var(--sp${pending.stream === 'mic' ? 0 : 7})`}>${pending.stream === 'mic' ? 'You' : '…'}</span><p>…</p></div>`}
  </div>`;
}

function Menu({ close }) {
  const settings = panel.use((s) => s.settings);
  const shown = panel.use((s) => s.shownInSharing);
  const hide = settings && settings.meeting.panel.hide_from_sharing;
  return html`<div class="menu" role="menu">
    <label class="mi">Hide from screen sharing<input type="checkbox" checked=${hide} onChange=${(e) => call('settings_set', 'meeting.panel.hide_from_sharing', e.currentTarget.checked)} /></label>
    ${hide && html`<label class="mi">Show it this meeting<input type="checkbox" checked=${shown} onChange=${(e) => { panel.set({ shownInSharing: e.currentTarget.checked }); call('meeting_show_in_sharing', e.currentTarget.checked); }} /></label>`}
    <button type="button" class="mi" onClick=${() => { close(); call('window_open', 'meetings'); }}>Open in Koli</button>
    <button type="button" class="mi" onClick=${() => { close(); call('meeting_new'); }}>New meeting</button>
    <button type="button" class="mi" onClick=${() => { close(); call('meeting_end'); }}>End meeting</button>
  </div>`;
}

function Panel() {
  const m = panel.use((s) => s.meeting);
  const menu = panel.use((s) => s.menu);
  const collapsed = panel.use((s) => s.collapsed);
  const drag = useDrag('meeting_drag');
  const pillRef = useRef(null);
  useLayoutEffect(() => {
    if (!collapsed || !pillRef.current) return;
    const r = pillRef.current.getBoundingClientRect();
    call('meeting_collapse', true, Math.ceil(r.width), Math.ceil(r.height)).catch(() => {});
  }, [collapsed, m.state]);
  const title = m.title || 'Meeting';
  if (m.state === 'offered') {
    return html`<div class="panel"><div class="head" ...${drag}><span class="dot offered"></span><span class="title">Call detected</span></div>
      <div class="offer"><div>${m.source && m.source.app ? m.source.app.replace('.exe', '') : 'A call'} has sound. Transcribe it?</div>
        <div class="row"><button type="button" class="btn" onClick=${() => call('meeting_offer', false)}>Not now</button>
          <button type="button" class="btn pri" onClick=${() => call('meeting_offer', true)}>Start</button></div></div></div>`;
  }
  if (m.state === 'ended') {
    return html`<div class="panel"><div class="head"><span class="dot ended"></span><span class="title">Saved · ${clock(m.duration_s || 0)}</span></div></div>`;
  }
  if (collapsed) {
    return html`<div ref=${pillRef} class="pill" ...${drag} onClick=${() => { panel.set({ collapsed: false }); call('meeting_collapse', false); }} title="Expand">
      <span class=${'dot ' + m.state}></span>${title}<${Timer} since=${m.started_at} paused=${m.state === 'paused'} /></div>`;
  }
  const paused = m.state === 'paused';
  return html`<div class="panel" onKeyDown=${(e) => e.key === 'Escape' && panel.set({ menu: false })}>
    <div class="head" ...${drag}>
      <span class=${'dot ' + m.state}></span><span class="title" title=${title}>${title}</span>
      <${Timer} since=${m.started_at} paused=${paused} />
      ${m.behind_s > 5 && html`<span class="sub" title="Transcription is catching up">· ${Math.round(m.behind_s)} s behind</span>`}
      <button type="button" class="btn icon" aria-label="Menu" onClick=${() => panel.set({ menu: !menu })}><${Icon} name="dots" /></button>
      <button type="button" class="btn icon" aria-label="Collapse" onClick=${() => panel.set({ collapsed: true })}><${Icon} name="down" /></button>
      <button type="button" class="btn icon" aria-label="End meeting" title="End meeting" onClick=${() => call('meeting_end')}><${Icon} name="x" /></button>
    </div>
    ${menu && html`<${Menu} close=${() => panel.set({ menu: false })} />`}
    ${m.notice === 'resumed' && html`<div class="notice"><span class="grow">Resumed · ${title}</span>${m.can_split && html`<button type="button" class="btn" onClick=${() => call('meeting_split')}>Start new instead</button>`}</div>`}
    ${m.notice === 'new' && html`<div class="notice"><span class="grow">New meeting</span>${m.can_continue && html`<button type="button" class="btn" onClick=${() => call('meeting_continue_previous')}>Continue previous instead</button>`}</div>`}
    ${m.excluded && html`<div class="notice"><span class="grow">You're excluded · your microphone isn't in the transcript</span></div>`}
    <${Turns} />
    <div class="foot">
      <button type="button" class=${'btn' + (m.excluded ? ' on' : '')} onClick=${() => call('meeting_exclude', !m.excluded)}><${Icon} name="mic" />${m.excluded ? 'Include me' : 'Exclude me'}</button>
      <button type="button" class="btn" onClick=${() => call(paused ? 'meeting_resume' : 'meeting_pause')}><${Icon} name=${paused ? 'play' : 'pause'} />${paused ? 'Resume' : 'Pause'}</button>
      <span class="grow"></span>
      <button type="button" class="btn icon" aria-label="Open in Koli" title="Open in Koli" onClick=${() => call('window_open', 'meetings')}><${Icon} name="doc" /></button>
    </div>
    <${Grip} />
  </div>`;
}

async function boot() {
  wire();
  render(html`<${Panel} />`, document.getElementById('root'));
  const data = await call('app_bootstrap', 'meeting');
  panel.set({ settings: data.settings, meeting: data.meeting || { state: 'idle' }, turns: data.turns || [],
              collapsed: !!(data.settings.meeting && data.settings.meeting.panel.collapsed) });
}
boot();
```

The page never uses `window.prompt` (CLAUDE.md: no browser prompts).

Also add `meeting.html` to the mock's reach: `js/api.js` already loads `mock-api.js` with `?mock=1`; add to `mock-api.js` (Task 10) the `meeting_*` handlers so `meeting.html?mock=1` renders a fake live meeting.

- [ ] **Step 5: Run the test and launch**

Run: `... -m unittest tests.test_meeting.PanelPlacementTests -v` and the full suite. Then launch: `PYTHONPATH=S:/Koli-meeting-mode/src S:/Koli/.venv/Scripts/python.exe -m koli --test --open`, open the Koli window, and from the tray pick "Start meeting": the dark panel must appear at the right edge, never take focus when clicked, drag by its header, resize by the corner, collapse to a pill, and hide a couple of seconds after "End meeting". With Discord or Chrome sharing the screen, the panel must not appear in the shared picture while "Hide from screen sharing" is on.

- [ ] **Step 6: Commit**

```bash
git -C S:/Koli-meeting-mode add src/koli/ui/web/meeting.html src/koli/ui/web/css/meeting.css src/koli/ui/web/js/meeting.js src/koli/ui/win32.py src/koli/ui/host.py src/koli/ui/bridge.py src/koli/ui/floating_bar.py tests/test_meeting.py
git -C S:/Koli-meeting-mode commit -m "feat(meeting): translucent meeting panel window and page, hidden from screen sharing

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Meetings screen, the Apps control, and the browser mock

**Files:**
- Create: `src/koli/ui/web/js/screens/meetings.js`
- Modify: `src/koli/ui/web/js/screens/shared.js:144` (`EXTRA_FOR` gains `'meeting.enabled': 'loopback'` so Settings → Meetings shows the Install row while `soundcard` is missing), `src/koli/ui/web/js/app.js` (imports, `ADVANCED_NAV`, `SCREENS`, search screens, store wiring for `meeting` events), `src/koli/ui/web/js/store.js:30` (`meeting: { state: 'idle' }` in the app store), `src/koli/ui/web/js/ui.js` (a `people` icon), `src/koli/ui/web/js/screens/apps.js` (`summary`, `RuleEditor`), `src/koli/ui/web/js/mock-api.js` (`meeting_*` handlers and a fake live meeting)

**Interfaces:**
- Consumes: bridge `meetings_list`, `meeting_get`, `meeting_delete`, `meeting_export`, `meeting_rename`, `meeting_start`, `meeting_end`, `meeting_new`, `meeting_pause`, `meeting_resume`, `meeting_exclude`, `meeting_continue_previous`, `meeting_split`; events `meeting`, `meeting_turn`.

- [ ] **Step 1: Write the screen**

`src/koli/ui/web/js/ui.js` `PATHS`: add

```javascript
  people: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><circle cx="16.5" cy="9" r="2.6"/><path d="M15 14.2a4.5 4.5 0 0 1 5.5 4.3"/>',
```

`src/koli/ui/web/js/store.js`: add `meeting: { state: 'idle' }` to the `app` store's initial state.

`src/koli/ui/web/js/app.js`:
- import `{ MeetingsScreen } from './screens/meetings.js'`;
- `ADVANCED_NAV`: insert `['meetings', 'Meetings', 'people']` after `['history', 'History', 'clock']`;
- `SCREENS`: add `meetings: MeetingsScreen`;
- search screens list: add `['Meetings', 'meetings']`;
- where the app wires events (find `on('state'` in app.js), add `on('meeting', (m) => app.set({ meeting: m }));` and in the bootstrap set `meeting: data.meeting || { state: 'idle' }` (the host's `bootstrap('main')` must include `data['meeting'] = svc.state() if svc else {'state': 'idle'}`; add that line in Task 9's `bootstrap` for `which == 'main'` as well).

`src/koli/ui/web/js/screens/meetings.js`:

```javascript
// screens/meetings.js
// Meetings: the one running now (with the panel's controls), the saved ones,
// and a transcript view with speaker colours, rename, export and delete.

import { html, useEffect, useRef, useState } from '../../vendor/preact-htm.module.js';
import { call, on } from '../api.js';
import { app, confirmModal, promptModal, textDir, toast } from '../store.js';
import { Icon, Select } from '../ui.js';
import { ExtraInstall } from './shared.js';

const COLOURS = ['#0A6E82', '#B45309', '#6D28D9', '#15803D', '#BE185D', '#0369A1', '#C2410C', '#4D7C0F'];
function clock(s) { s = Math.max(0, Math.floor(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }
function when(iso) { return (iso || '').replace('T', ' ').slice(0, 16); }

function LiveCard({ m }) {
  if (!m || !['live', 'paused', 'offered'].includes(m.state)) return null;
  const paused = m.state === 'paused';
  return html`<section class="card pad stack" aria-label="Meeting now">
    <div class="row-flex" style="justify-content:space-between">
      <span class="card-title"><span class="chip rec">${m.state === 'offered' ? 'Call detected' : paused ? 'Paused' : 'Live'}</span> ${m.title || 'Meeting'}</span>
      <span class="small muted">${m.turns} turns${m.behind_s > 5 ? ` · ${Math.round(m.behind_s)} s behind` : ''}</span></div>
    ${m.state === 'offered' ? html`<div class="row-flex"><button type="button" class="btn pri" onClick=${() => call('meeting_offer', true)}>Start transcribing</button>
        <button type="button" class="btn" onClick=${() => call('meeting_offer', false)}>Not now</button></div>`
      : html`<div class="row-flex">
        <button type="button" class=${'btn' + (m.excluded ? ' pri' : '')} onClick=${() => call('meeting_exclude', !m.excluded)}>${m.excluded ? 'Include me' : 'Exclude me'}</button>
        <button type="button" class="btn" onClick=${() => call(paused ? 'meeting_resume' : 'meeting_pause')}>${paused ? 'Resume' : 'Pause'}</button>
        <button type="button" class="btn" onClick=${() => call('meeting_new')}>New meeting</button>
        ${m.can_continue && html`<button type="button" class="btn" onClick=${() => call('meeting_continue_previous')}>Continue previous instead</button>`}
        ${m.can_split && html`<button type="button" class="btn" onClick=${() => call('meeting_split')}>Start new instead</button>`}
        <span class="grow"></span>
        <button type="button" class="btn danger" onClick=${() => call('meeting_end')}>End meeting</button></div>`}
  </section>`;
}

function Transcript({ id, live }) {
  const [m, setM] = useState(null);
  const box = useRef(null);
  const load = () => call('meeting_get', id).then(setM).catch((e) => toast(e.message, { kind: 'err' }));
  useEffect(() => { setM(null); load(); }, [id]);
  useEffect(() => on('meeting_turn', (p) => {
    if (p.id !== id) return;
    if (p.reload) { load(); return; }
    if (p.turn) setM((cur) => cur && { ...cur, turns: [...cur.turns, p.turn] });
  }), [id]);
  useEffect(() => on('meeting', (s) => { if (s.id === id && m) setM({ ...m, speakers: s.speakers.map((sp) => ({ ...sp })) }); }), [id, m]);
  useEffect(() => { if (live && box.current) box.current.scrollTop = box.current.scrollHeight; });
  if (!m) return html`<div class="empty">Loading…</div>`;
  const names = Object.fromEntries(m.speakers.map((s) => [s.id, s]));
  const who = (sid) => names[sid] || { id: sid, name: sid === '?' ? 'Speaker ?' : sid, colour: 7 };
  const renameSpeaker = async (sp) => {
    const name = await promptModal({ title: 'Rename speaker', label: 'Name', value: sp.name, confirm: 'Rename' });
    if (name) call('meeting_rename', sp.id, name, id).then(load).catch((e) => toast(e.message, { kind: 'err' }));
  };
  const exportAs = (fmt) => call('meeting_export', id, fmt).then((p) => p && toast(`Exported to ${p}`)).catch((e) => toast(e.message, { kind: 'err' }));
  return html`<section class="card" style="min-width:0;overflow:hidden;display:flex;flex-direction:column;max-height:calc(100vh - 200px)" aria-label="Transcript">
    <div class="row-flex" style="padding:12px 18px;border-bottom:1px solid var(--line)">
      <div class="grow"><div style="font-weight:600">${m.title}</div><div class="small muted">${when(m.started_at)} · ${clock(m.turns.length ? m.turns[m.turns.length - 1].end : 0)} · ${m.source.app ? m.source.app.replace('.exe', '') : 'manual'}</div></div>
      <div class="row-flex">${m.speakers.map((sp) => html`<button type="button" class="chip" style=${`color:${COLOURS[sp.colour % 8]}`} title="Rename" onClick=${() => renameSpeaker(sp)}>${sp.name}</button>`)}</div>
      <${Select} label="Export" value="" options=${[['', 'Export…'], ['txt', 'As .txt'], ['md', 'As .md']]} onChange=${(v) => v && exportAs(v)} />
      ${!live && html`<button type="button" class="btn ghost icon danger" aria-label="Delete" onClick=${async () => {
        if (await confirmModal({ title: 'Delete this meeting?', body: 'The transcript is removed from this computer.', confirm: 'Delete', danger: true })) {
          call('meeting_delete', id).then(() => { toast('Meeting deleted'); app.set({ route: 'meetings' }); }).catch((e) => toast(e.message, { kind: 'err' }));
        }
      }}><${Icon} name="trash" /></button>`}
    </div>
    <div ref=${box} style="overflow-y:auto;padding:8px 18px 16px">
      ${m.turns.length === 0 && html`<div class="empty">${live ? 'Listening…' : 'Nothing was said.'}</div>`}
      ${m.turns.map((t) => { const sp = who(t.speaker); return html`<div style="display:grid;grid-template-columns:56px 120px minmax(0,1fr);gap:10px;padding:6px 0;align-items:start">
        <span class="mono small muted">${clock(t.start)}</span>
        <button type="button" class="btn ghost sm" style=${`justify-content:flex-start;color:${COLOURS[sp.colour % 8]};font-weight:600`} onClick=${() => renameSpeaker(sp)}>${sp.name}</button>
        <p dir=${textDir(t.text)} style="margin:0;white-space:pre-wrap">${t.text}</p></div>`; })}
    </div>
  </section>`;
}

export function MeetingsScreen({ route }) {
  const live = app.use((s) => s.meeting);
  const [list, setList] = useState([]);
  const [selected, setSelected] = useState(null);
  const reload = () => call('meetings_list').then(setList).catch(() => {});
  useEffect(() => { reload(); }, [live.state]);
  const current = selected || (live.id && ['live', 'paused'].includes(live.state) ? live.id : (list[0] && list[0].id));
  return html`<div class="page">
    <div class="page-head">
      <div><h1 class="page-title">Meetings</h1><p class="page-sub">Calls transcribed as they happen, with who said what. Stored only on this computer.</p></div>
      ${!['live', 'paused', 'offered'].includes(live.state) && html`<button type="button" class="btn pri" onClick=${() => call('meeting_start')}>Start a meeting now</button>`}
    </div>
    <${ExtraInstall} name="loopback" />
    <${LiveCard} m=${live} />
    <div style="display:grid;grid-template-columns:minmax(260px,320px) minmax(0,1fr);gap:16px;align-items:start">
      <section class="card" style="overflow:hidden" aria-label="Past meetings">
        ${list.length === 0 && html`<div class="empty">No meetings yet.</div>`}
        ${list.map((h) => html`<button type="button" class="menu-item" style=${`display:block;width:100%;text-align:start;padding:12px 16px;border-top:1px solid var(--line)${current === h.id ? ';background:var(--sunk)' : ''}`} onClick=${() => setSelected(h.id)}>
          <div style="font-weight:600">${h.title}${live.id === h.id ? html` <span class="chip rec">Live</span>` : ''}</div>
          <div class="small muted">${when(h.started_at)} · ${clock(h.duration_s)} · ${h.speakers.join(', ')}</div></button>`)}
      </section>
      ${current ? html`<${Transcript} id=${current} live=${live.id === current} />` : html`<div class="empty">Pick a meeting.</div>`}
    </div>
  </div>`;
}
```

`src/koli/ui/web/js/screens/apps.js`:
- in `summary(e)`: add `if (e.meeting) bits.push(e.meeting === 'off' ? 'Calls ignored' : e.meeting === 'ask' ? 'Calls: ask' : e.meeting === 'auto' ? 'Calls: auto' : 'Calls');`
- in `RuleEditor`, after the auto-listen row:

```javascript
    <${Row} label="Calls in this app"><${Segmented} label="Meetings" value=${e.meeting ?? null}
      options=${[[null, 'Not a meeting app'], ['default', 'Default'], ['auto', 'Auto'], ['ask', 'Ask'], ['off', 'Off']]} onChange=${(v) => set('meeting', v)} /></${Row}>
```

`src/koli/ui/web/js/mock-api.js`: add a fake meeting next to `state`:

```javascript
  const SPEAKERS = [{ id: 'you', name: 'You', colour: 0 }, { id: 's1', name: 'Speaker 1', colour: 1 }, { id: 's2', name: 'Ilona', colour: 2 }];
  const LINES = [['s2', 'We fixed it, but if it breaks again, who knows.'], ['you', 'And the bridge is not working now?'], ['s1', 'On Friday they hit one or two bridges.'], ['you', 'שלום, אני אגיע מחר בבוקר.']];
  let meeting = { state: 'idle', id: null, title: '', source: {}, started_at: null, elapsed_s: 0, paused_by_user: false, excluded: false, behind_s: 0, speakers: [], notice: null, turns: 0, can_continue: false, can_split: false, offer: null };
  const saved = { '2026-10-10-150000': { id: '2026-10-10-150000', title: 'Discord', source: { app: 'discord.exe', kind: 'native', title: '', code: '' }, started_at: '2026-10-10T15:00:00+03:00', ended_at: '2026-10-10T15:42:00+03:00', language: 'auto', speakers: SPEAKERS, turns: LINES.map(([sp, text], i) => ({ start: i * 9, end: i * 9 + 6, stream: sp === 'you' ? 'mic' : 'loopback', speaker: sp, text, lang: /[\u0590-\u05FF]/.test(text) ? 'he' : 'en' })), gaps: [] } };
  let liveTurns = [];
  let liveTimer = null;
  const emitMeeting = () => emit('meeting', { ...meeting, turns: liveTurns.length, speakers: SPEAKERS });
  const startMeeting = () => {
    meeting = { ...meeting, state: 'live', id: 'live-mock', title: 'Discord · mock', source: { app: 'discord.exe', kind: 'native' }, started_at: new Date().toISOString(), notice: 'new', can_continue: true };
    liveTurns = [];
    emitMeeting();
    let i = 0;
    liveTimer = setInterval(() => {
      const [sp, text] = LINES[i % LINES.length];
      emit('meeting_turn', { id: 'live-mock', pending: { stream: sp === 'you' ? 'mic' : 'loopback' } });
      setTimeout(() => { const turn = { start: i * 9, end: i * 9 + 6, stream: sp === 'you' ? 'mic' : 'loopback', speaker: sp, text, lang: 'en' }; liveTurns.push(turn); emit('meeting_turn', { id: 'live-mock', turn }); }, 800);
      i += 1;
    }, 4000);
  };
  const endMeeting = () => {
    clearInterval(liveTimer);
    emit('meeting', { ...meeting, state: 'ended', duration_s: 125 });
    meeting = { ...meeting, state: 'idle', id: null, notice: null };
    emitMeeting();
  };
```

and handlers in the `api` object:

```javascript
    meeting_state: () => ok({ ...meeting, speakers: SPEAKERS }),
    meeting_start: () => { startMeeting(); return ok(); },
    meeting_end: () => { endMeeting(); return ok(); },
    meeting_new: () => { endMeeting(); startMeeting(); return ok(); },
    meeting_pause: () => { meeting = { ...meeting, state: 'paused', paused_by_user: true }; emitMeeting(); return ok(); },
    meeting_resume: () => { meeting = { ...meeting, state: 'live', paused_by_user: false, notice: 'resumed', can_split: true }; emitMeeting(); return ok(); },
    meeting_exclude: (on) => { meeting = { ...meeting, excluded: !!on }; emitMeeting(); return ok(); },
    meeting_offer: (accept) => { if (accept) startMeeting(); else { meeting = { ...meeting, state: 'idle' }; emitMeeting(); } return ok(); },
    meeting_rename: (sid, name) => { const sp = SPEAKERS.find((s) => s.id === sid); if (sp) sp.name = name; emitMeeting(); return ok(); },
    meeting_continue_previous: () => { meeting = { ...meeting, notice: 'resumed', can_continue: false }; emitMeeting(); return ok(); },
    meeting_split: () => { meeting = { ...meeting, notice: 'new', can_split: false }; emitMeeting(); return ok(); },
    meetings_list: () => ok(Object.values(saved).map((m) => ({ id: m.id, title: m.title, source: m.source, started_at: m.started_at, ended_at: m.ended_at, duration_s: 2520, speakers: m.speakers.map((s) => s.name), turns: m.turns.length }))),
    meeting_get: (id) => ok(id === 'live-mock' ? { id, title: meeting.title, source: meeting.source, started_at: meeting.started_at, speakers: SPEAKERS, turns: liveTurns, gaps: [] } : saved[id]),
    meeting_delete: (id) => { delete saved[id]; return ok(true); },
    meeting_export: () => ok('C:\\Users\\you\\Desktop\\meeting.txt'),
    meeting_resize: () => ok(), meeting_collapse: (c) => ok({ collapsed: !!c }), meeting_drag: () => ok(), meeting_show_in_sharing: (on) => ok({ shown: !!on }),
```

and in `app_bootstrap` include `meeting: { ...meeting, speakers: SPEAKERS }` and, for `which === 'meeting'`, `turns: liveTurns`. With `?mock=1&meeting` in the URL call `startMeeting()` after `loaded` so `meeting.html?mock=1&meeting` shows a live panel.

- [ ] **Step 2: Check in the browser and the app**

Serve `src/koli/ui/web` with `python -m http.server 8765` from that folder and open `http://localhost:8765/index.html?mock=1&advanced#/meetings` and `http://localhost:8765/meeting.html?mock=1&meeting`. The Meetings tab lists the saved meeting and shows the live one with turns arriving; Hebrew lines are right-to-left; renaming a speaker updates the chips. The panel page shows the header, turns with colours, the pending placeholder, and the footer buttons. Then run the full test suite (unchanged) and launch the app (`--test --open`) to see the Meetings tab in the real window.

- [ ] **Step 3: Commit**

```bash
git -C S:/Koli-meeting-mode add src/koli/ui/web/js/screens/meetings.js src/koli/ui/web/js/app.js src/koli/ui/web/js/store.js src/koli/ui/web/js/ui.js src/koli/ui/web/js/screens/apps.js src/koli/ui/web/js/mock-api.js src/koli/ui/host.py
git -C S:/Koli-meeting-mode commit -m "feat(meeting): Meetings screen, calls control on app rules, browser mock

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Docs

**Files:**
- Modify: `CHANGELOG.md` (Unreleased → Added), `README.md` (feature list, next to the system-audio line), `docs/project-index.md` (component table and the UI table), `docs/roadmap/roadmap.md` (remove the meeting-mode line), `docs/roadmap/completed.md` (add it), `docs/design/meeting-mode.md` (a "Found while building" section if anything changed from the spec)

- [ ] **Step 1: Write the entries**

`CHANGELOG.md`, first bullet under `### Added`:

```markdown
- **Meeting mode.** When a call runs in Discord, Zoom, Teams, Slack or a
  browser tab (Google Meet and the web versions), Koli notices, listens to
  what you hear and to your microphone, and shows the conversation in a dark
  translucent panel as it happens, each turn labelled "You" or "Speaker N"
  (rename by clicking). Meetings are saved with speakers and times and listed
  on the new Meetings screen, with export as .txt or .md. A call that drops
  and comes back within 10 minutes resumes the same meeting; a different call
  or a longer gap starts a new one, with a one-click undo either way. The
  panel has Exclude me (your mic stays out while you dictate elsewhere), Pause
  and New meeting, and stays out of screen sharing. Settings → Meetings holds
  automatic / ask / never, the gap, the language and the panel; each app rule
  can override it. Needs the loopback extra (`pip install koli[loopback]`),
  offered with an Install button on first use. Hebrew and English in one
  meeting work with language auto-detect; speakers are told apart by a small
  sherpa-onnx voiceprint model downloaded at the first meeting.
```

`README.md`: after the system-audio capture bullet add:

```markdown
- 🗣️ **Meeting mode** — calls in Discord, Zoom, Teams or Google Meet are transcribed as they happen, with who said what, in a translucent panel that stays out of screen sharing; meetings are saved on the Meetings screen (`pip install 'koli[loopback]'`, Windows first)
```

`docs/project-index.md`: add rows to the component table:

```markdown
| **Meeting Mode** | `meeting/` + `services/meetings.py` | Detects calls (`detector.py`), runs the session (`session.py`), captures loopback and mic (`capture.py`), labels speakers (`speakers.py`), transcribes on a low-priority worker (`transcriber.py`), keeps meeting files (`store.py`) | soundcard, pycaw, ten-vad, sherpa-onnx |
| **Audio Sessions / Browser Tabs** | `platform/*/audio_sessions.py`, `platform/*/browser_tabs.py` | Which apps have sound; which browser tabs are in a call (Windows; macOS stubs) | pycaw, comtypes |
```

and to the UI table:

```markdown
| **Meeting Panel** | `ui/web/meeting.html` + `js/meeting.js` | The dark panel with the live transcript; hidden from screen sharing by `ui/win32.py` | Preact, htm |
```

and in the "App window and floating bar" intro mention the Meetings screen (`js/screens/meetings.js`). Update the "Last Updated" line.

`docs/roadmap/roadmap.md`: remove `- As a *user* I want **meeting mode** so I can transcribe an entire meeting in real-time`; `docs/roadmap/completed.md`: add `- As a *user* I want **meeting mode** so I can transcribe an entire meeting in real-time, with who said what`.

`docs/design/meeting-mode.md`: add `## Found while building (2026-10-11)` listing any deviation (for example `panel.custom` stored as `custom_x`/`custom_y`; `meeting.model` as a text field).

- [ ] **Step 2: Run the whole suite one last time**

Run: `cd S:/Koli-meeting-mode && PYTHONUTF8=1 PYTHONPATH=S:/Koli-meeting-mode/src S:/Koli/.venv/Scripts/python.exe -m unittest discover -s tests -q 2>&1 | tail -3`. Expected: `OK (skipped=4)` with the new tests counted.

- [ ] **Step 3: Commit**

```bash
git -C S:/Koli-meeting-mode add CHANGELOG.md README.md docs/project-index.md docs/roadmap/roadmap.md docs/roadmap/completed.md docs/design/meeting-mode.md
git -C S:/Koli-meeting-mode commit -m "docs(meeting): changelog, readme, project index and roadmap for meeting mode

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

Then ask the user for a real test (CLAUDE.md): a Discord call, a Meet in Chrome behind other tabs, a screen share with the panel hidden, dictating mid-meeting with Exclude me, a reconnect within 10 minutes, and a second call after a longer gap.
