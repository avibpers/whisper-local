# Discord Meeting Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Discord call is offered the moment you join (Discord holds the microphone), survives a short sampling gap without pausing, and gets the other person's name, both on the meeting title and on the far-end voice in a 1:1 call.

**Architecture:** The Windows audio-session probe also reads capture (microphone) sessions; `find_sources` marks each native source with whether the app holds the mic, and the session only offers a native source that does, while an ongoing meeting keeps either signal. A new platform module reads who is in the call from Discord's window through UI Automation; the meeting service polls it every 15 s while a Discord meeting is live, stores it on the meeting, retitles "Discord" to "Discord · Becky", and in a 1:1 call renames the first default-named far-end speaker, which also saves that voiceprint under the name.

**Tech Stack:** Python 3, pycaw (Core Audio), comtypes UI Automation, unittest/pytest.

**Spec:** `docs/research/wispr-flow-meeting-detection.md` (sections 3 and 4; the live Discord test results are the requirements).

## Global Constraints

- Windows only; every new platform function gets a macOS stub with the same signature returning an empty value (`docs/platform-abstraction.md`).
- Detection keys on the app (`discord.exe`), never on one microphone: Discord moves its capture session between devices mid-call.
- Discord does not expose who is speaking; nothing in this plan tries to read it. Voiceprints stay the way to tell voices apart.
- The ConsentStore registry is not used (no key for the current Discord version).
- A name the user gave always wins: the call name only replaces a `Speaker N` default (`is_default_name`).
- Discord's window read takes 1 to 4.7 s: never call it from the detector's 1 s tick.
- Comments: module header 2–4 lines, one-line intent comment above non-trivial functions (project CLAUDE.md).
- Tests run with `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/test_meeting.py -q` from `S:\Koli`.

## Review Focus

1. **You mute yourself in Discord mid-call.** If Discord releases the capture session while muted, the meeting must keep going on the render session, not pause. Covered by `test_ongoing_meeting_keeps_native_source_without_mic` (Task 2) and the manual check in Task 6.
2. **Discord plays a ping or a join sound outside a call** (render session active, no mic). No offer must appear. Covered by `test_native_render_only_is_not_offered` (Task 2).
3. **The far end's voice is split into two voices by diarization.** Only one of them gets the call name; the other stays `Speaker N` so the user can merge by renaming. Covered by `test_call_name_goes_to_one_speaker_only` (Task 5).
4. **Discord's window shows another chat, or is minimized.** The reader returns `{}` and nothing on the meeting changes (no title flip back, no name removal). Covered by `test_empty_call_keeps_previous_call` (Task 5).
5. **A sampling gap of one tick** (both device sessions read between Discord's device switch). The meeting must not pause on a single missing tick. Covered by `test_one_missing_tick_does_not_pause` (Task 3).

---

### Task 1: Capture sessions in the audio-session probe

**Files:**
- Modify: `src/koli/platform/windows/audio_sessions.py`
- Test: `tests/test_meeting.py` (class `DeviceRoutingTests`, next to `test_sessions_carry_their_output_device`)

**Interfaces:**
- Produces: `active(devices_fn=None, capture_devices_fn=None) -> list` whose entries are `{pid, exe, state, device, device_id, flow}` with `flow` `'render'` or `'capture'`. When `devices_fn` is given and `capture_devices_fn` is not, no capture sessions are read (keeps existing tests off real hardware). Capture entries keep the capture device's name in `device`.

- [ ] **Step 1: Write the failing tests** in `DeviceRoutingTests`, reusing the fake `Proc`/`Session` classes of `test_sessions_carry_their_output_device`:
  - `test_capture_sessions_are_marked`: `mod.active(devices_fn=lambda: [('Speakers', 'r1', [Session('discord.exe', 1)])], capture_devices_fn=lambda: [('Microphone (BY-V20)', 'c1', [Session('discord.exe', 1)])])` returns two entries; the capture one has `flow == 'capture'`, `device == 'Microphone (BY-V20)'`; the render one has `flow == 'render'`.
  - `test_render_only_when_capture_probe_missing`: with only `devices_fn` given, every entry has `flow == 'render'`.
- [ ] **Step 2: Run them to verify they fail**
  Run: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/test_meeting.py -q -k "capture_sessions_are_marked or render_only_when_capture"`
  Expected: FAIL (`unexpected keyword argument 'capture_devices_fn'` / `KeyError: 'flow'`).
- [ ] **Step 3: Implement.** Generalize `_render_devices()` into `_devices(flow: int)` (same body, `EDataFlow` value as parameter) and read `eRender` then `eCapture` in `active()`. Update the module header: capture sessions tell which app holds the microphone, which is what a call is.
- [ ] **Step 4: Run all meeting tests**
  Run: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/test_meeting.py -q`
  Expected: PASS.
- [ ] **Step 5: Commit**
  `git add src/koli/platform/windows/audio_sessions.py tests/test_meeting.py`
  `git commit -m "feat(meetings): read which apps hold the microphone"`

### Task 2: A native app is offered only while it holds the mic

**Files:**
- Modify: `src/koli/meeting/detector.py` (`find_sources`, header comment)
- Modify: `src/koli/meeting/session.py` (`SessionManager.on_sources`, idle branch)
- Test: `tests/test_meeting.py` (`DetectorTests`, `SessionTests`)

**Interfaces:**
- Consumes: Task 1's `flow` field.
- Produces: native sources carry `'mic': True | False | None`. `None` means the probe reported no capture sessions at all (macOS, pycaw failure, old test data) and keeps today's behaviour. Browser sources carry `'mic': None`. `mic` is not added to `SOURCE_KEYS` (not saved on the meeting).

- [ ] **Step 1: Write the failing tests**
  - `DetectorTests.test_native_source_holds_mic`: sessions `[{exe:'discord.exe', state:1, flow:'render', device:'Speakers'}, {exe:'discord.exe', state:1, flow:'capture', device:'Mic'}]` → one source, `mic is True`, `device == 'Speakers'` (the loopback device stays the render one).
  - `DetectorTests.test_native_capture_only_is_a_source`: only the capture entry active → one source, `mic is True`, `device == ''`.
  - `DetectorTests.test_native_render_only_reports_no_mic`: render active, capture entry with `state 0` → one source with `mic is False`.
  - `DetectorTests.test_no_capture_data_reports_unknown_mic`: render entries without a `flow` key → `mic is None`.
  - `SessionTests.test_native_render_only_is_not_offered`: `on_sources([discord source with mic False])` then `on_speech(5)` → state stays `idle`, no pending.
  - `SessionTests.test_ongoing_meeting_keeps_native_source_without_mic`: start a meeting from a `mic True` Discord source, then `on_sources([same source with mic False])` → state stays `live`.
- [ ] **Step 2: Run them to verify they fail**
  Run: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/test_meeting.py -q -k "holds_mic or capture_only or reports_no_mic or unknown_mic or render_only_is_not_offered or keeps_native_source_without_mic"`
  Expected: FAIL.
- [ ] **Step 3: Implement.** In `find_sources`: an entry without `flow` counts as render; collect active render exes (with their device) and active capture exes; a native exe is a candidate when it is in either set; `mic` is `exe in capture_exes` when any entry had `flow == 'capture'`, else `None`. Browsers keep using render sessions only. In `on_sources`' idle branch, drop candidates with `s.get('mic') is False` (a native app that only plays sound has not joined a call). Rewrite the detector header to say the microphone is what marks a call, sound alone keeps it going.
- [ ] **Step 4: Run all meeting tests**
  Run: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/test_meeting.py -q`
  Expected: PASS.
- [ ] **Step 5: Commit**
  `git add src/koli/meeting/detector.py src/koli/meeting/session.py tests/test_meeting.py`
  `git commit -m "feat(meetings): offer a native call when the app holds the microphone"`

### Task 3: Confirm a call ended before pausing

**Files:**
- Modify: `src/koli/meeting/session.py` (`_sources_while_meeting`, new constants next to `NOTICE_SECONDS`)
- Test: `tests/test_meeting.py` (`SessionTests`, with `FakeClock`)

**Interfaces:**
- Produces: constants `END_CONFIRM_TICKS = 2` and `END_CONFIRM_SECONDS = 1.5` (Wispr's values: 2 polls and 1.5 s). The live meeting pauses only when its source has been missing for at least `END_CONFIRM_TICKS` consecutive `on_sources` calls **and** `END_CONFIRM_SECONDS` since the first miss (measured with the manager's `now`). Any tick that sees the source resets the count.

- [ ] **Step 1: Write the failing tests**
  - `test_one_missing_tick_does_not_pause`: live meeting; `on_sources([])`; `clock.advance(1)`; `on_sources([source])` → state `live`.
  - `test_missing_source_pauses_after_confirmation`: live; `on_sources([])`; `clock.advance(1)`; `on_sources([])` → still `live` (1 s < 1.5 s); `clock.advance(1)`; `on_sources([])` → `paused`.
  - Update existing tests that expect a pause on the first empty tick to call `on_sources([])` twice with `clock.advance(2)` between (find them with `-k pause`).
- [ ] **Step 2: Run them to verify they fail**
  Run: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/test_meeting.py -q -k "missing_tick or pauses_after_confirmation"`
  Expected: FAIL.
- [ ] **Step 3: Implement** with two fields on the manager (`_missing_ticks: int`, `_missing_since: Optional[datetime]`), reset in `_begin`, `_resume` and whenever a match is seen. Comment why: Discord moves its session between microphones mid-call, and one tick can fall between the two.
- [ ] **Step 4: Run all meeting tests**
  Run: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/test_meeting.py -q`
  Expected: PASS.
- [ ] **Step 5: Commit**
  `git add src/koli/meeting/session.py tests/test_meeting.py`
  `git commit -m "fix(meetings): confirm a call is gone before pausing"`

### Task 4: Read who is in a Discord call from its window

**Files:**
- Create: `src/koli/platform/windows/call_people.py`
- Create: `src/koli/platform/macos/call_people.py`
- Modify: `src/koli/platform/__init__.py` (both import lines)
- Modify: `tests/test_smoke.py:20` (platform module list)
- Test: `tests/test_meeting.py` (new class `CallPeopleTests`)

**Interfaces:**
- Produces:
  - `parse_discord(rows: list) -> dict`, pure, where `rows` is a list of `(class_name: str, name: str)` from the window. Returns `{'with': str, 'people': [{'name': str, 'muted': bool}]}`, or `{}` when no call is visible.
  - `read(exe: str) -> dict`: `{}` for any exe other than `discord.exe`, when Discord has no window, or on any UIA error; otherwise `parse_discord` of every descendant's `(ClassName, Name)` in Discord's top-level `Chrome_WidgetWin_1` windows (one cached `FindAllBuildCache`, scope descendants, Name 30005 and ClassName 30012).
  - macOS: `read(exe: str) -> dict: return {}`.
- Parsing rules, from the recorded window (research doc section 3):
  - `with`: from a hyperlink name matching `^(?:unread, )?(?P<name>.+?) \(direct message\), Ongoing Call` (e.g. `unread, Becky (direct message), Ongoing Call, Online` → `Becky`); fall back to `^In a call \((?P<name>[^)]+)\)` (`In a call (Becky)`). Only the first match counts.
  - `people`: each row whose class contains `focusTarget__` and whose name is not empty; `muted` when the name ends with `, Muted`, which is stripped from `name` (`v069675, Muted` → `{'name': 'v069675', 'muted': True}`). Deduplicate by name, keep order. These rows only exist while the call's conversation is open.
  - No `with` and no `people` → `{}`.

- [ ] **Step 1: Write the failing tests** in `CallPeopleTests`, loading the Windows module by path like the macOS stub tests do (`spec_from_file_location`) so they run without comtypes:
  - `test_dm_call_with_tiles`: rows `[('link__972a0 channelNameFade__972a0', 'unread, Becky (direct message), Ongoing Call, Online'), ('focusTarget__54e4b', 'avi151105'), ('focusTarget__54e4b', 'v069675, Muted'), ('focusTarget__54e4b', 'v069675, Muted')]` → `{'with': 'Becky', 'people': [{'name': 'avi151105', 'muted': False}, {'name': 'v069675', 'muted': True}]}`.
  - `test_call_label_fallback`: rows `[('container_c9d15c textWithIconContainer_c9d15c textXs_c9d15c', 'In a call (Becky)')]` → `{'with': 'Becky', 'people': []}`.
  - `test_no_call`: rows `[('link__972a0 channelNameFade__972a0', 'Ilona (direct message), Idle')]` → `{}`.
  - `test_other_apps_read_nothing`: `mod.read('zoom.exe') == {}`.
  - `test_macos_stub`: the macOS module's `read('discord.exe') == {}`.
- [ ] **Step 2: Run them to verify they fail**
  Run: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/test_meeting.py -q -k CallPeopleTests`
  Expected: FAIL (module not found).
- [ ] **Step 3: Implement.** Copy `browser_tabs.py`'s per-thread `_uia()` pattern (it is private there; a shared helper is a separate refactor). Find Discord windows with `EnumWindows` + the process exe, as `browser_tabs._browser_windows` does, then `uia.ElementFromHandle(hwnd)`. Header: what Discord shows about a call (who, mute), and that who is speaking is not exposed (tested 2026-10-20).
- [ ] **Step 4: Run tests**
  Run: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/test_meeting.py tests/test_smoke.py -q`
  Expected: PASS.
- [ ] **Step 5: Commit**
  `git add src/koli/platform/windows/call_people.py src/koli/platform/macos/call_people.py src/koli/platform/__init__.py tests/test_smoke.py tests/test_meeting.py`
  `git commit -m "feat(meetings): read who is in a Discord call"`

### Task 5: The call's people on the meeting

**Files:**
- Modify: `src/koli/meeting/session.py`
- Test: `tests/test_meeting.py` (new class `CallNameTests`, set up like `SessionVoiceNameTests` at :2043)

**Interfaces:**
- Consumes: Task 4's dict shape.
- Produces on `SessionManager`:
  - `set_call(call: dict)`: an empty dict changes nothing. Otherwise store `meeting['call'] = {'with': ..., 'people': [...]}` (merging: a later read without tiles keeps the earlier `people`), mark for save, emit. If `with` is set and the meeting title is still `title_for(meeting['source'])`, the title becomes `f"{title_for(source)} · {with}"` (`Discord · Becky`). A title changed by the summary or the user is left alone.
  - `call_name_target() -> Optional[tuple]`: `(speaker_id, name)` when the live meeting has a `call.with`, no speaker already has that name, and some speaker other than `you` still has a default name; the earliest such speaker in `meeting['speakers']` order. Otherwise `None`.

- [ ] **Step 1: Write the failing tests** in `CallNameTests`:
  - `test_call_retitles_default_title`: live Discord meeting titled `Discord`; `set_call({'with': 'Becky', 'people': []})` → title `Discord · Becky`, `meeting['call']['with'] == 'Becky'`.
  - `test_call_keeps_custom_title`: title set to `Planning` first → stays `Planning`.
  - `test_empty_call_keeps_previous_call`: `set_call({'with': 'Becky', 'people': [{'name': 'v069675', 'muted': False}]})` then `set_call({})` and `set_call({'with': 'Becky', 'people': []})` → `call` still has the one person, title unchanged.
  - `test_call_name_target_picks_first_default_speaker`: speakers `you`, `s1 'Speaker 1'`, `s2 'Speaker 2'` → `('s1', 'Becky')`.
  - `test_call_name_goes_to_one_speaker_only`: `s1` already named `Becky` → `None`.
  - `test_user_name_wins`: only `s1 'Dana'` (not default) → `None`.
- [ ] **Step 2: Run them to verify they fail**
  Run: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/test_meeting.py -q -k CallNameTests`
  Expected: FAIL.
- [ ] **Step 3: Implement** both methods under the manager's `_transition()` lock like `ensure_speaker`; use `is_default_name`.
- [ ] **Step 4: Run all meeting tests**
  Run: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/test_meeting.py -q`
  Expected: PASS.
- [ ] **Step 5: Commit**
  `git add src/koli/meeting/session.py tests/test_meeting.py`
  `git commit -m "feat(meetings): keep who is in the call and name the far end"`

### Task 6: Poll the call while a Discord meeting runs

**Files:**
- Modify: `src/koli/services/meetings.py`
- Modify: `docs/design/meeting-mode.md` (detection section), `docs/project-index.md` (one row: **Call People** `platform/*/call_people.py`)
- Test: `tests/test_meeting.py` (`ServiceVoicesTests` style, :2368)

**Interfaces:**
- Consumes: `call_people.read`, `SessionManager.set_call`, `SessionManager.call_name_target`, `MeetingService.rename_speaker`.
- Produces on `MeetingService`:
  - constant `CALL_POLL_SECONDS = 15`;
  - `_poll_call(read: Callable[[str], dict] = None)`: one step: when the session is `live` and `meeting['source']['app'] == 'discord.exe'`, call `read('discord.exe')`, pass the result to `session.set_call`, then if `session.call_name_target()` returns `(sid, name)`, call `self.rename_speaker(sid, name)` (which also stores the voiceprint under that name). `read` defaults to the platform module. Errors are logged, never raised.
  - A daemon thread `meeting-call-people`, started in `_start_capture` and stopped in `_stop_capture` (an `Event`, like `_watch_stop`), calling `_poll_call` every `CALL_POLL_SECONDS`, first call right away.

- [ ] **Step 1: Write the failing tests**
  - `test_poll_call_names_far_end`: service with a live Discord meeting and speaker `s1 'Speaker 1'`; `_poll_call(read=lambda exe: {'with': 'Becky', 'people': []})` → speaker `s1` named `Becky`, title `Discord · Becky`.
  - `test_poll_call_skips_other_apps`: live Zoom meeting; a `read` that records calls is never called.
  - `test_poll_call_survives_errors`: `read` raises → no exception, meeting unchanged.
- [ ] **Step 2: Run them to verify they fail**
  Run: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/test_meeting.py -q -k poll_call`
  Expected: FAIL.
- [ ] **Step 3: Implement** `_poll_call` and the thread; update the two docs.
- [ ] **Step 4: Run the full suite**
  Run: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests -q`
  Expected: PASS.
- [ ] **Step 5: Live check with the user** (launch with `run-koli.py`, see memory note on meeting mode). Ask the user to:
  1. Join a Discord DM call: the offer appears within 2 s of connecting, before anyone speaks, titled `Discord`.
  2. Open the call's conversation: within 15 s the title becomes `Discord · <name>`.
  3. The other person talks: their voice appears as `<name>`, not `Speaker 1`.
  4. Mute yourself in Discord for 30 s: the meeting stays live (Review Focus 1). If it pauses, report it before committing; the fix is in Task 2's rule.
  5. Play a sound in Discord outside a call (open a voice message): no offer.
  6. Hang up: the meeting pauses within about 3 s.
- [ ] **Step 6: Commit** after the user confirms the live check
  `git add src/koli/services/meetings.py docs/design/meeting-mode.md docs/project-index.md tests/test_meeting.py`
  `git commit -m "feat(meetings): name Discord calls and their far end from the window"`
