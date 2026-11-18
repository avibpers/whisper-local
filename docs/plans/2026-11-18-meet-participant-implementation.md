# Koli in the meeting: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Koli follows a running meeting (notes, answers, tasks) in the meeting panel, and can sit in a Google Meet call as her own participant whose tile shows her notes.

**Architecture:** A pure "brain" (`meeting/companion.py`) turns new transcript lines into small JSON notes with one AI call per update. `services/companion.py` runs it on one worker thread, publishes `meeting_companion` to the panel and owns the guest. The guest (`meeting/guest.py`) drives Koli's own Chrome for Testing (`browser/`) over the DevTools Protocol and feeds a canvas board to Meet as the camera.

**Tech Stack:** Python 3.13 standard library (urllib, zipfile, subprocess, ctypes, threading), `cloud_stt/ws.py` for the WebSocket, Preact + htm for the panel, plain JS for the board and the extension.

**Spec:** `docs/design/meet-participant.md`. The spike it rests on: `docs/research/meet-participant-spike/`.

## Global Constraints

- Tests: `PYTHONUTF8=1 PYTHONPATH=src python -m unittest tests.<module>`; unittest only; each test file starts with `sys.path.insert(0, str(ROOT / "src"))`.
- No commits: the owner approves one commit at the end, after his live test. Every touched file goes into the registry task's `files[]`.
- Files claimed by the `meeting-mic` task (`services/meetings.py`, `config.defaults.yaml`, `settings/schema.py`, `settings/appliers.py`, `ui/host.py`, `main.py`, `CHANGELOG.md`, `docs/project-index.md`, `tests/test_meeting.py`) get small additive edits only, in Task 9, after the owner's OK. New tests go in new test files.
- Defaults: `mode: off`, `join_call: false`, `chat: false`. Nothing visible to other people starts by itself.
- Every module opens with `# path/file.py` and a 2–4 line purpose comment; non-trivial functions get a one-line intent comment; sections use `# ── Name ───` rules.
- No new Python dependency.
- The browser port is as it ran in the spike: same flags, same order, same pauses, no `Runtime.enable`, the port chosen by Koli and the address read from `/json/version`.
- Chrome for Testing version: `158.0.8127.36`; URL `https://storage.googleapis.com/chrome-for-testing-public/158.0.8127.36/win64/chrome-win64.zip`.
- No `alert()` / `confirm()` / `prompt()` in any page. No text input in the meeting panel.
- Before adding a CSS class, look for one that already does it in `css/meeting.css`.

## Review Focus

1. **The AI answers with something that isn't the JSON asked for** (prose, a list, half a JSON): the notes stay as they were and a card says the update failed; nothing crashes. (Task 5)
2. **A meeting restarts under the worker** (New meeting, Continue previous, Start new instead, turns removed by echo removal): the worker re-reads the turns and never reads past the end of a shorter list. (Task 7)
3. **Meet refuses the guest, or a step of the join isn't on the page**: state `refused` (retried) or `failed` with what was looked for; the browser is closed; the panel part goes on. (Task 4)
4. **Koli dies while the guest is in a call**: the browser ends with her (job object), and the next start kills any browser still using her profile. (Task 1)
5. **Private text on the board or in the chat**: `public_view` passes only its listed fields, and a card's text reaches the chat only by the owner's click. (Tasks 5, 7)

---

### Task 1: The browser process and the DevTools Protocol

**Files:**
- Create: `src/koli/browser/__init__.py`, `src/koli/browser/cdp.py`, `src/koli/browser/process.py`
- Create: `src/koli/platform/windows/child_jobs.py`, `src/koli/platform/macos/child_jobs.py`
- Modify: `src/koli/platform/__init__.py` (add `child_jobs` to both import lists)
- Test: `tests/test_browser.py`

**Interfaces (produces):**
- `cdp.Connection(ws)`: `send(method, params=None, session_id=None, timeout=30.0) -> dict` (raises `CdpError`), `on_event(fn)`, `close()`, `closed`. One reader thread; answers matched by `id`.
- `cdp.connect(port, timeout=15.0, connect=WebSocket.connect) -> Connection`: polls `http://127.0.0.1:<port>/json/version` for `webSocketDebuggerUrl`.
- `cdp.Page(connection, target_id, session_id)`: `evaluate(expression, timeout=30.0)` (awaitPromise, returnByValue, userGesture; raises `CdpError` on a page exception), `send(method, params=None)`, `navigate(url)`, `add_script(source)`.
- `cdp.first_page(connection) -> Page`: attaches flat to the first `page` target and sends `Page.enable` only.
- `process.BrowserProcess`: `start(exe, profile_dir, headless=True, size=(1100, 760), extra_args=()) -> int` (the port), `stop()`, `running`, `pid`. `process.launch_args(...) -> list` holds the flag list. `process.kill_leftovers(profile_dir, list_processes=None, kill=None) -> int`.
- `child_jobs.attach(pid) -> bool`: Windows job object with kill-on-close, kept for the app's life; macOS returns False.

- [ ] **Step 1: tests** in `tests/test_browser.py`: `CdpConnectionTests` with a fake socket (`send` gets the matching answer though events and another id arrive first; an `error` answer raises `CdpError` with the message; a closed socket fails pending calls at once, not after the timeout); `LaunchArgsTests` (the exact flags of the spec, `--headless=new` only when headless, never `--remote-debugging-port=0`); `LeftoverTests` (only processes whose command line holds the profile path are killed, and only main ones).
- [ ] **Step 2: run, see them fail.**
- [ ] **Step 3: implement.** `BrowserProcess.start` picks a free port, starts with `CREATE_NO_WINDOW`, attaches the job, returns the port. `stop()` sends `Browser.close` if a connection was given, waits 2 s, then kills.
- [ ] **Step 4: run, pass.**
- [ ] **Step 5: real-browser check** `tools/guest_local_check.py` (not a unit test): starts the browser found in `<data>/browser/` or `KOLI_BROWSER_EXE`, serves `docs/research/meet-participant-spike/call-test.html`, injects the spike's `camera-inject.js`, and prints frames sent and decoded over 20 s, visible then minimized. Expected: about 1 frame a second in both.

### Task 2: Installing the browser

**Files:**
- Create: `src/koli/browser/install.py`
- Test: `tests/test_browser.py` (`InstallTests`)

**Interfaces (produces):**
- `install.VERSION`, `install.folder() -> Path` (`<data>/browser`), `install.executable() -> Optional[Path]` (the installed `chrome.exe` of `VERSION`, else None), `install.supported() -> bool` (Windows x64).
- `install.ensure(reporter=None, url=None, fetch=None) -> Path`: downloads to `chrome-<VERSION>.zip.part` with `reporter.report(fraction, "Downloading Koli's browser: N of M MB")` and `reporter.check_cancelled()` between blocks; `zipfile.testzip()`; unpacks to `chrome-<VERSION>/`; writes `INSTALLED`; deletes the zip and other `chrome-*` folders. Raises `InstallError` with a sentence for the user.
- `install.profile_dir() -> Path` (`<data>/browser/profile`).

- [ ] **Step 1: tests**: a local `ThreadingHTTPServer` serving a small zip with `chrome-win64/chrome.exe`: `ensure` installs it, reports rising progress, `executable()` then answers; a truncated zip raises `InstallError` and leaves no `chrome-<VERSION>` folder; a cancelled reporter stops and removes the `.part`; an older `chrome-1.0` folder is removed after success.
- [ ] **Step 2–4: fail, implement, pass.**

### Task 3: Input like a person

**Files:**
- Create: `src/koli/browser/human.py`
- Test: `tests/test_browser.py` (`HumanInputTests`)

**Interfaces (produces):**
- `human.Hands(page, sleep=time.sleep, rng=None)`: `click(x, y)` (18–27 `mouseMoved` steps from the last point with ±1.5 px wobble and 12–34 ms apart, a 180–430 ms rest, `mousePressed` with `buttons: 1`, 60–130 ms, `mouseReleased`), `type(text)` (per character `keyDown` with `text`, 40–100 ms, `keyUp`, then 90–250 ms; Shift down and up around a capital), `press(key)` for `Enter`, `Escape`, `Tab`, `insert(text)` (`Input.insertText`, for text after the join).

- [ ] **Step 1: tests** with a recording fake page: `click(300, 200)` ends on exactly (300, 200), the last three events are moved, pressed, released, and there are at least 18 moves; `type("Ko")` sends rawKeyDown Shift, keyDown `K` with `modifiers: 8`, keyUp, keyUp Shift, then keyDown `o` with `text: 'o'`; no sleep is shorter than 0.012.
- [ ] **Step 2–4: fail, implement, pass.**

### Task 4: The guest and her board

**Files:**
- Create: `src/koli/meeting/guest_board.js`, `src/koli/meeting/guest.py`
- Modify: `pyproject.toml` (package-data: `"meeting/*.js"`)
- Test: `tests/test_guest.py`

**Interfaces:**
- Consumes: Task 1–3.
- Produces: `guest.MeetGuest(on_state, name='Koli', browser=None, install=None, sleep=time.sleep, now=time.time)`:
  - `join(code: str, owner: str = '')` (returns at once; a thread runs the join), `leave()`, `state` (one of `guest.STATES`), `in_call`.
  - `show(view: dict)`: the public view for the board (latest wins; applied on the next 5 s tick or at once when in the call).
  - `say(text: str) -> bool`: one chat message, cut to 400 characters; False when not in the call.
  - `set_tile(width, height)`: the owner's tile, for the frame's shape.
  - `on_state(state: str, detail: str, progress: Optional[float])`.
- `guest.STATES = ('off', 'unsupported', 'installing', 'starting', 'joining', 'asking', 'in_call', 'leaving', 'refused', 'failed')`; `RETRIES = 3`, `RETRY_WAIT_S = 8`, `ASK_WAIT_S = 180`, `TICK_S = 5`.
- `guest.frame_size(tile_w, tile_h) -> (int, int)`: height 720, width `720 × aspect` with the aspect held to 0.75–1.78 and rounded to a multiple of 16; no tile → (960, 720).
- `guest_board.js` exposes `window.__koli`: `board(view)`, `resize(w, h)`, `sending()`, `info()`. `view = {title, subtitle, topic, decisions[], tasks[], questions[], say, dense}`.

- [ ] **Step 1: tests** with a scripted fake page (answers `evaluate` by matching a probe name): the happy path reaches `in_call` and the page saw, in order, grant permissions, add script, navigate to `…/abc-defg-hij?hl=en`, a click, typing `Koli`, a click, hide; a refusal text → `refused`, then a second attempt, and after `RETRIES` the state is `failed` with "Meet refused the join" in the detail; "Ask to join" → `asking`, then `in_call` once the leave button appears; a dialog with "Join now" is pressed; no name box within the wait → `failed` naming the name box; `leave()` during the join stops it and closes the browser; `say('x' * 900)` inserts 400 characters; `frame_size(258, 305)` = (608, 720), `frame_size(1280, 720)` = (1280, 720), `frame_size(0, 0)` = (960, 720).
- [ ] **Step 2–4: fail, implement, pass.** The page probes are named JS snippets in `guest.py` (`NAME_BOX`, `BUTTON(text)`, `IN_CALL`, `REFUSED`), each returning plain data. The pauses between steps are the spike's (3 s after the name box appears, 1 s after the click, 2 s after typing, 8 s after Join).
- [ ] **Step 5: board check** with `tools/guest_local_check.py --board`: renders the board with a Hebrew and an English view at 1280×720, 960×720 and 608×720 and saves PNGs; look at them: Hebrew runs right to left, nothing leaves the frame, long lines wrap.

### Task 5: The brain

**Files:**
- Create: `src/koli/meeting/companion.py`
- Test: `tests/test_companion.py`

**Interfaces (produces):**
- `MODES = ('off', 'insights', 'active')`, `EMPTY_NOTES`, limits `MAX_SUMMARY = 3`, `MAX_DECISIONS = 8`, `MAX_TASKS = 10`, `MAX_QUESTIONS = 5`, `NEW_TEXT_CHARS = 6000`, `MIN_NEW_WORDS = 25`, `SAY_MAX = 400`.
- `lines(m, since=0.0, until=None) -> list[dict]`: `{'at', 'name', 'you', 'text'}` per turn, names resolved now (`You` for the owner).
- `transcript(rows) -> str`: `[m:ss] Name: text` lines, the newest kept when over `NEW_TEXT_CHARS`.
- `update(notes, rows, mode, language, complete, parse) -> dict` (new notes; raises `CompanionError`), `answer(notes, rows, language, complete, parse, past='') -> dict` (`{question, answer, points}`), `catch_up(rows, language, complete, parse) -> dict` (`{lines}`), `task_from_talk(rows, language, complete, parse) -> dict` (`{task, owner}`), `public_answer(notes, rows, question, language, complete, parse) -> str`.
- `clean_notes(raw, mode) -> dict`: only the known fields, cut to the limits; `ask` only in `active`.
- `public_view(notes, owner, name='Koli', say='') -> dict` with exactly `PUBLIC_FIELDS = ('title', 'subtitle', 'topic', 'decisions', 'tasks', 'questions', 'say')`.
- `spoken_to(text, name) -> bool`: the name or an alias (`koli`, `coley`, `collie`, `kohli`, `קולי`) as a word, in a line that asks something (a `?`, or starts with a question word in English or Hebrew, or the name is followed by a comma).
- `words(rows) -> int`.

- [ ] **Step 1: tests**: `clean_notes` drops unknown keys, cuts lists, removes `ask` in insights; `update` with a fake `complete` passes the old notes and only the new lines, and returns cleaned notes; a non-JSON answer raises `CompanionError` and the caller's notes are untouched; `transcript` keeps the newest lines under the limit and formats `[1:05] Dana: hi`; `public_view` returns exactly `PUBLIC_FIELDS` and never `ask`, even when `notes['ask']` is set; `spoken_to('Koli, what did we decide?', 'Koli')` True, `spoken_to('the colour is nice', 'Koli')` False, `spoken_to('קולי מה החלטנו?', 'Koli')` True; `lines` names the owner `You` and resolves a renamed speaker.
- [ ] **Step 2–4: fail, implement, pass.** All AI calls use the feature `'companion'` and `json=True`; the system prompts say: use this meeting only, write in `<language>`, answer with the JSON only.

### Task 6: Tasks in the meeting file

**Files:**
- Modify: `src/koli/meeting/session.py` (`set_tasks` after `set_attendees`; `continue_previous`; `split_resumed`), `src/koli/meeting/summarize.py` (`meeting_text`)
- Test: `tests/test_companion.py` (`TaskStoreTests`)

**Interfaces (produces):**
- `SessionManager.set_tasks(meeting_id, tasks) -> bool`: each `{'id', 'task', 'owner', 'at', 'source'}` with `task` ≤ 200 characters, `owner` ≤ 40 or None, `at` seconds or None, `source` `'you'` or `'koli'`; live or saved, like `set_attendees`.
- `meeting_text` adds, after the people, `Tasks noted during the meeting (include each in the action items):` and one `- [m:ss] task (owner)` line each, only when there are tasks.

- [ ] **Step 1: tests**: tasks set on the live meeting are in `meeting_get` and on disk after the end; Continue previous moves them onto the previous meeting with their times shifted; Start new instead keeps tasks before the cut on the old meeting and moves later ones; `meeting_text` shows them and shows nothing when there are none.
- [ ] **Step 2–4: fail, implement, pass.**

### Task 7: The service

**Files:**
- Create: `src/koli/services/companion.py`
- Test: `tests/test_companion.py` (`ServiceTests`)

**Interfaces:**
- Consumes: Tasks 4–6.
- Produces: `CompanionService(meetings, setting, publish, complete, parse, ai_ready, subscribe=None, guest_factory=None, copy=None, now=time.time, start_worker=True)`:
  - `state() -> dict`: `{id, mode, ready, busy, notes, cards, tasks, guest: {state, detail, progress, can_join}}`.
  - `set_mode(mode)`, `now_()`, `answer()`, `catch_up(minutes=5)`, `task()`, `task_take(index)`, `task_remove(task_id)`, `dismiss(card_id)`, `copy(text)`, `say(card_id)`, `join()`, `leave()`, `apply_setting(path, value)`, `shutdown()`, `guest_name() -> str` (`''` when she isn't joining), `tile(width, height)`.
  - `step() -> bool`: runs one queued job, for tests.
- Cards: `{id, kind: 'answer'|'catch_up'|'ask'|'said'|'error', title, text, points, at}`, the newest first, at most 6.

- [ ] **Step 1: tests** with a fake meetings object (a `session` with `meeting`, `state`, `set_tasks`; `meet.call`) and a fake `complete`: a turn event only queues; an update runs after `update_seconds` and 25 new words, not before; `mode: off` makes no AI call at all; `reload` after the turn list got shorter re-reads without an error; an `ask` in Active becomes a card once (the same question again adds none); `task()` adds a task with `source: 'you'` through `set_tasks`; an AI failure becomes an `error` card and `busy` goes back to False; the meeting ending tells the guest to leave and clears the notes; `say(card_id)` sends the card's text only when the guest is in the call; a line that speaks to Koli posts to the chat only in Active with `chat` on, and not twice within 20 s; `guest_name()` is `''` unless she is joining or in the call.
- [ ] **Step 2–4: fail, implement, pass.**

### Task 8: The extension and the feed

**Files:**
- Modify: `chrome-extension/meet.js` (status: `guest_tile`; read `guest_name` from Koli's reply), `chrome-extension/manifest.json` (version `0.3.0`), `src/koli/meeting/meet_feed.py`
- Test: `tests/test_companion.py` (`MeetFeedGuestTests`)

**Interfaces (produces):**
- `MeetFeed(now=time.time, guest_name=lambda: '')`: people whose name equals the guest's (case-insensitive) are left out of `people`; `MeetCall.guest_tile` (`[w, h]` or None) is in `joined_calls()` rows as `guest_tile`.
- In the extension: the tile whose name equals `reply.guest_name` is measured with `getBoundingClientRect()` and sent as `guest_tile: [w, h]` (rounded), or left out when not on screen.

- [ ] **Step 1: tests**: a status with people `Noa (self)`, `Dana`, `Koli` and `guest_name` → `Koli` gives people without Koli; `guest_tile: [258, 305]` is returned by `call(code)`; without a guest name nothing is filtered.
- [ ] **Step 2–4: fail, implement, pass.**

### Task 9: Wiring (claimed files, after the owner's OK)

**Files:**
- Modify: `src/koli/services/meetings.py` (build `self.companion` after `self.session`; `_meet_status` adds `guest_name`; `MeetFeed(guest_name=…)`; `apply_setting` passes `meeting.companion.*`; `shutdown` stops it; the guest's tile from `_meet_events`), `src/koli/config.defaults.yaml`, `src/koli/settings/schema.py`, `src/koli/ui/host.py` (`PANEL_TOPICS`), `src/koli/ui/bridge.py` (the `companion_*` calls), `src/koli/ui/web/js/screens/usage.js` (label `companion`)
- Test: `tests/test_companion.py` (`WiringTests`), `tests/test_smoke.py` (run only)

- [ ] **Step 1: tests**: a `MeetingService` built the way `tests/test_meet_extension.py` does has `companion`; its `_meet_status()` has `guest_name`; `apply_setting('meeting.companion.mode', 'insights')` reaches the companion; every bridge call returns `{ok: True}` on an idle service.
- [ ] **Step 2–4: fail, implement, pass.**
- [ ] **Step 5:** `python -m unittest tests.test_smoke tests.test_meeting tests.test_meet_extension tests.test_companion tests.test_guest tests.test_browser`: all pass (the smoke test checks every default has a schema entry).

### Task 10: The panel

**Files:**
- Create: `src/koli/ui/web/js/meeting_companion.js`
- Modify: `src/koli/ui/web/js/meeting.js` (store field `companion`, the `meeting_companion` subscription, `<Companion>` above `<Turns>`), `src/koli/ui/web/css/meeting.css`

- [ ] **Step 1:** `Companion({ m, state })`: the header row (mode picker as a button menu like `TemplatePick`, the guest's status with Bring in / Take out), the Now line, the cards (Copy, Send to chat when in the call, Task, ×), the tasks and decisions line that opens a list, and the buttons row. With `mode: off` only the header row shows. Errors from a call show as a card, never a dialog.
- [ ] **Step 2:** CSS: reuse `.btn`, `.menu`, `.mi`, `.sub`, `.notice`, `.grow`, `.tpl-pick`; add only `.koli` (the section), `.koli-now`, `.card` and `.card .row`.
- [ ] **Step 3:** open `meeting.html` through the mock page server and check the section in the three modes with sample state, English and Hebrew; no console errors.

### Task 11: Documents and the last run

**Files:**
- Modify: `CHANGELOG.md`, `docs/project-index.md`, `docs/design/meet-extension.md` (the guest's tile and name), `chrome-extension/README.md`
- Modify: the registry task's `files[]`

- [ ] **Step 1:** changelog entry under `[Unreleased] › Added`; project-index rows for Koli in the meeting and Koli's browser.
- [ ] **Step 2:** the full run of Task 9 Step 5 again, and `tools/guest_local_check.py` once more.
- [ ] **Step 3:** report to the owner what was built, what was verified and how, and what only a live call can show; propose the explicit `git add` list.
