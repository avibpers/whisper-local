# Auto-dictation implementation plan

Spec: `docs/design/auto-dictation.md`. Branch: `feat/new-ui` (in place).
Each task: write the failing test, make it pass, keep the suite green
(`.venv/Scripts/python.exe -m pytest tests/test_smoke.py -q`).

## Task 1 — VAD per-recording timeout and `had_speech`
Files: `src/koli/voice_activity_detection.py`, `tests/test_smoke.py`
- `ContinuousVoiceDetector.set_silence_timeout(seconds)`; `had_speech` flag
  set on entering `SPEECH_DETECTED`, cleared by `reset()`, shown in `snapshot()`.
- Test with a fake `ten_vad` returning fixed probabilities.

## Task 2 — Recorder passes the override
Files: `src/koli/audio_recorder.py`, tests
- `start_recording(silence_timeout=None)`: override or restore from
  `vad_manager.vad_silence_timeout_seconds` before `reset()`.

## Task 3 — State manager auto paths
Files: `src/koli/state_manager.py`, tests
- `_auto_started` flag; `_begin_recording(auto_started=False)`;
  `begin_auto_recording()`, `is_auto_recording()`, `cancel_auto_recording()`;
  `cancel_active_recording(quiet=False)`; `handle_vad_event` quiet-cancel
  branch; `_publish_state` mode `'auto'`; flag cleared in stop/cancel/max.
- `set_auto_dictation(coordinator)` and a `NON_TEXT_EXES` lookup helper.

## Task 4 — Coordinator
Files: `src/koli/auto_dictation.py` (new), tests
- `AutoDictation` per the spec, with injectable watcher, pid and clock, and
  `threading.Timer` for the 300 ms debounce (tests call `flush_timer()`).

## Task 5 — Windows input watcher
Files: `src/koli/platform/windows/input_watch.py` (new),
`src/koli/platform/macos/input_watch.py` (new stub), `src/koli/platform/__init__.py`,
`pyproject.toml` (comtypes for win32), tests for the pure typing filter
(`classify_key(vk, flags, ctrl, alt, win) -> bool`, importable on any OS by
keeping the ctypes setup lazy inside `start()`).

## Task 6 — Settings, rules, UI
Files: `src/koli/config.defaults.yaml`, `src/koli/settings/schema.py`,
`src/koli/settings/appliers.py`, `src/koli/services/rules.py`,
`src/koli/app_rules.defaults.yaml`, `src/koli/ui/web/js/screens/apps.js`,
`src/koli/ui/web/js/bar.js`, `src/koli/ui/web/js/screens/live.js`,
`src/koli/ui/web/js/mock-data.json` (via `tools/make_mock_data.py`).

## Task 7 — Wiring and docs
Files: `src/koli/main.py`, `docs/project-index.md`, `CHANGELOG.md`,
`README.md`, `docs/faq.md`.
- Build, start, stop the coordinator; project-index rows for the two new
  modules; changelog entry under Unreleased.

## Task 8 — Verify
- Full suite green; `koli --doctor` style import check of the new modules
  on Windows; launch via `/test-from-wsl`; then hand over for the real test
  listed in the spec. No commit before that test.
