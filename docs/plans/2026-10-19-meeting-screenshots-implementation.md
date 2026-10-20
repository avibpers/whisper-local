# Meeting Screenshots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A snip taken during a meeting becomes a timestamped, captioned PNG in the meeting file, shown at its moment in the panel, the Meetings screen and the exports.

**Architecture:** The meeting JSON gains a `screenshots` list beside `turns`; every view merges the two by time. A Windows platform module opens the snip overlay and reads the Snipping Tool's image off the clipboard; `MeetingService.screenshot()` runs the key flow and an optional clipboard watcher for "every snip" mode. Pages get images as `data:` URLs through the bridge.

**Tech Stack:** Python 3.11, Pillow (`ImageGrab.grabclipboard`), pywin32/ctypes (clipboard sequence, window owner), Preact + htm pages, unittest.

**Spec:** `docs/design/meeting-screenshots.md` (read it first; this plan argues from it).

## Global Constraints

- Setting `meeting.screenshots` has exactly the values `off`, `all`, `key`; default `key`.
- Hotkey setting path `hotkey.meeting_screenshot_hotkey`, default `""`.
- PNG files live in `<meetings>/<id>.shots/`, named `<offset with two decimals, zero-padded to 4 integer digits>.png` (e.g. `0279.40.png`).
- A screenshot entry is `{"at": float, "image": str, "caption": str, "description": None}`.
- Times are seconds from `started_at`, like every time in the file.
- Only images whose clipboard owner is `ScreenClippingHost.exe` or `SnippingTool.exe` count; an unreadable owner is accepted.
- Keys Koli pressed itself never trigger anything; the clipboard does.
- macOS: `platform/macos/screenshots.py` is a stub; `available()` is `False` and the two settings are hidden.
- Every module starts with a 2–4 line header comment; non-trivial functions get a one-line intent comment (CLAUDE.md).
- No `alert()`/`confirm()`/`prompt()` in pages.
- Work in `S:\Koli-meeting-screenshots` on `feat/meeting-screenshots`; every git command as `git -C S:\Koli-meeting-screenshots ...`; explicit `git add <files>`.
- Tests run with `S:\Koli\.venv\Scripts\python.exe -m pytest` (cwd the worktree). Smoke test `test_every_default_has_a_schema_entry` must keep passing.

## Review Focus

1. A snip taken in `key` mode after Esc closed a previous overlay must stay private (Task 5 test: watcher ignores an image while no snip is expected; key flow clears `_expecting` when the overlay is gone).
2. A meeting that ends while the overlay is open: the image lands in the meeting that was live at press time (Task 5 test: `screenshot()` carries the id; `add_screenshot` on an ended meeting saves to its file).
3. Echo removal shifting turn indices must not break captions: captions key by image name (Task 2 test).
4. `continue_previous` with the same file name in both meetings must not overwrite (Task 3 test).
5. A huge or palette-mode image (a 4K snip, a GIF-like palette): `save_shot` saves whatever Pillow gives as PNG without converting; the page scales it with CSS (Task 1 test saves a `P` mode image).

---

### Task 1: Store: the shots folder, `screenshots` in the file, exports

**Files:**
- Modify: `src/koli/meeting/store.py` (`new_meeting`, `header`, `duration_s`, `export_text`, `MeetingStore.delete`, new shots functions)
- Test: `tests/test_meeting.py` (class `StoreTests`)

**Interfaces:**
- Produces:
  - `new_meeting(...)` returns a dict that also has `'screenshots': []`.
  - `header(meeting)` adds `'screenshots': len(meeting.get('screenshots', []))`.
  - `duration_s(meeting)` while open: `max(last turn end, last screenshot at, 0.0)`.
  - `shot_name(offset: float) -> str`: `f"{offset:07.2f}.png"` (so `279.4` → `0279.40.png`).
  - `rows_by_time(meeting) -> list[dict]`: turns and screenshots merged, each screenshot as `{'kind': 'shot', 'at': ..., ...entry}` and each turn as `{'kind': 'turn', ...turn}`; a screenshot goes before the first turn whose `start` > its `at`; turns keep their file order. Stable.
  - `export_text(meeting, fmt)` prints screenshot lines: txt `[04:39] Screenshot: <caption or image>`; md `![<caption or image>](<basename>-shots/<image>)` where `basename` is a new keyword arg `shots_folder: str = ''` (the bridge passes the folder name it copied into; empty means `<id>-shots`).
  - `MeetingStore.shots_dir(meeting_id) -> Path`: `folder / f"{id}.shots"`.
  - `MeetingStore.save_shot(meeting_id, offset, image) -> str`: saves `image` (a `PIL.Image.Image`) as PNG under `shots_dir`, returns the file name. If the name exists, appends `-2`, `-3` before `.png`.
  - `MeetingStore.shot_path(meeting_id, name) -> Path` (raises `FileNotFoundError` if the name has a path separator or does not exist).
  - `MeetingStore.move_shots(from_id, to_id, entries: list[dict]) -> None`: moves each entry's file into `to_id`'s folder; on a name clash renames with `-2`, `-3` and updates `entry['image']` in place.
  - `MeetingStore.delete` also rmtrees `shots_dir`.

- [ ] **Step 1: Write the failing tests** in `tests/test_meeting.py` `StoreTests`:

```python
def test_new_meeting_has_empty_screenshots(self):
    from koli.meeting.store import new_meeting
    m = new_meeting('2026-10-11-143012', 'x', {'app': '', 'kind': 'manual', 'title': '', 'code': ''}, at(30, 12))
    self.assertEqual(m['screenshots'], [])

def test_save_shot_names_by_offset_and_delete_removes_folder(self):
    from PIL import Image
    from koli.meeting.store import new_meeting, shot_name
    m = new_meeting('2026-10-11-143012', 'x', {'app': '', 'kind': 'manual', 'title': '', 'code': ''}, at(30, 12))
    self.store.save(m)
    self.assertEqual(shot_name(279.4), '0279.40.png')
    name = self.store.save_shot(m['id'], 279.4, Image.new('P', (4, 4)))
    self.assertEqual(name, '0279.40.png')
    self.assertEqual(self.store.save_shot(m['id'], 279.4, Image.new('RGB', (4, 4))), '0279.40-2.png')
    self.assertTrue(self.store.shot_path(m['id'], name).exists())
    with self.assertRaises(FileNotFoundError):
        self.store.shot_path(m['id'], '../x.png')
    self.store.delete(m['id'])
    self.assertFalse(self.store.shots_dir(m['id']).exists())

def test_rows_by_time_places_a_screenshot_before_later_speech(self):
    from koli.meeting.store import new_meeting, rows_by_time
    m = new_meeting('2026-10-11-143012', 'x', {'app': '', 'kind': 'manual', 'title': '', 'code': ''}, at(30, 12))
    m['turns'] = [{'start': 120.0, 'end': 125.0, 'stream': 'loopback', 'speaker': 's1', 'text': 'late', 'lang': 'en'},
                  {'start': 90.0, 'end': 95.0, 'stream': 'mic', 'speaker': 'you', 'text': 'early', 'lang': 'en'}]
    m['screenshots'] = [{'at': 100.0, 'image': '0100.00.png', 'caption': 'docs', 'description': None}]
    kinds = [(r['kind'], r.get('text') or r.get('image')) for r in rows_by_time(m)]
    self.assertEqual(kinds, [('turn', 'early'), ('shot', '0100.00.png'), ('turn', 'late')])

def test_export_prints_screenshot_lines(self):
    from koli.meeting.store import duration_s, export_text, header, new_meeting
    m = new_meeting('2026-10-11-143012', 'Sync', {'app': '', 'kind': 'manual', 'title': '', 'code': ''}, at(30, 12))
    m['turns'] = [{'start': 3.0, 'end': 7.0, 'stream': 'mic', 'speaker': 'you', 'text': 'hi', 'lang': 'en'}]
    m['screenshots'] = [{'at': 279.4, 'image': '0279.40.png', 'caption': 'S3 docs', 'description': None},
                        {'at': 300.0, 'image': '0300.00.png', 'caption': '', 'description': None}]
    txt = export_text(m, 'txt')
    self.assertIn('[04:39] Screenshot: S3 docs', txt)
    self.assertIn('[05:00] Screenshot: 0300.00.png', txt)
    md = export_text(m, 'md', shots_folder='sync-shots')
    self.assertIn('![S3 docs](sync-shots/0279.40.png)', md)
    self.assertEqual(header(m)['screenshots'], 2)
    self.assertEqual(duration_s(m), 300.0)

def test_move_shots_renames_on_clash(self):
    from PIL import Image
    from koli.meeting.store import new_meeting
    a = new_meeting('2026-10-11-143012', 'x', {'app': '', 'kind': 'manual', 'title': '', 'code': ''}, at(30, 12))
    b = new_meeting('2026-10-11-150000', 'x', {'app': '', 'kind': 'manual', 'title': '', 'code': ''}, at(30, 12))
    self.store.save(a); self.store.save(b)
    self.store.save_shot(a['id'], 10.0, Image.new('RGB', (2, 2)))
    self.store.save_shot(b['id'], 10.0, Image.new('RGB', (2, 2)))
    entries = [{'at': 10.0, 'image': '0010.00.png', 'caption': '', 'description': None}]
    self.store.move_shots(b['id'], a['id'], entries)
    self.assertEqual(entries[0]['image'], '0010.00-2.png')
    self.assertTrue(self.store.shot_path(a['id'], '0010.00-2.png').exists())
    self.assertFalse(self.store.shots_dir(b['id']).exists() and any(self.store.shots_dir(b['id']).iterdir()))
```

- [ ] **Step 2: Run them**: `python -m pytest tests/test_meeting.py -k "screenshots or save_shot or rows_by_time or export_prints or move_shots" -v`. Expected: FAIL (KeyError / ImportError).

- [ ] **Step 3: Implement** in `store.py`: the functions in Interfaces. `rows_by_time` walks turns in order, keeping a pointer into screenshots sorted by `at`, emitting each screenshot once the next turn's `start` exceeds its `at`; leftovers go at the end. Keep PNG saving as `image.save(path, format='PNG')`. Update the module header comment to mention the shots folder. Loading older files without `screenshots` must work: use `meeting.get('screenshots', [])` everywhere in the store.

- [ ] **Step 4: Run** the five tests and the whole `tests/test_meeting.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C S:\Koli-meeting-screenshots add src/koli/meeting/store.py tests/test_meeting.py
git -C S:\Koli-meeting-screenshots commit -m "feat(meeting): the store keeps screenshots beside a meeting and prints them in exports"
```

---

### Task 2: Session: add, caption, shift on resume and split

**Files:**
- Modify: `src/koli/meeting/session.py` (`state_payload`, new `add_screenshot`, `set_caption`, `continue_previous`, `split_resumed`)
- Test: `tests/test_meeting.py` (class `SessionTests`, the one that builds `self.sm` with `FakeClock`)

**Interfaces:**
- Consumes: Task 1 store functions.
- Produces:
  - `SessionManager.add_screenshot(meeting_id: str, image, at_ts: float) -> Optional[dict]`: `at_ts` is wall-clock (`time.time()`); saves through `store.save_shot`, appends the entry to the meeting's `screenshots` (the live one when ids match, else the saved file like `_append_saved`), marks the saver (or `store.save` for a saved meeting), publishes `('meeting_shot', {'id': meeting_id, 'shot': entry})`, returns the entry. Returns `None` when the meeting is unknown.
  - `SessionManager.set_caption(meeting_id: str, image: str, caption: str) -> bool`: live or saved meeting; publishes `('meeting_shot', {'id', 'shot': entry, 'caption': True})`.
  - `state_payload()` gains `'screenshots': self._settings('meeting.screenshots', 'key')`.
  - `continue_previous` shifts `at` by `offset` and calls `store.move_shots(new_id, prev_id, entries)` **before** `store.delete(new['id'])`.
  - `split_resumed` moves entries with `at >= resumed_s` to `fresh`, shifting by `-resumed_s`, and moves their files.

- [ ] **Step 1: Write the failing tests** in `SessionTests`:

```python
def test_add_screenshot_lands_in_the_live_meeting_and_publishes(self):
    from PIL import Image
    m = self.live()
    self.clock.advance(100)
    shot = self.sm.add_screenshot(m['id'], Image.new('RGB', (2, 2)), self.clock.now().timestamp())
    self.assertEqual(shot['at'], 100.0)
    self.assertEqual(shot['image'], '0100.00.png')
    self.assertEqual(self.sm.current()['screenshots'], [shot])
    self.assertIn(('meeting_shot', {'id': m['id'], 'shot': shot}), self.events)
    self.assertEqual(self.sm.state_payload()['screenshots'], 'key')

def test_screenshot_after_end_goes_to_that_meeting_file(self):
    from PIL import Image
    m = self.live()
    self.sm.end()
    self.sm.add_screenshot(m['id'], Image.new('RGB', (2, 2)), self.clock.now().timestamp() + 1)
    self.assertEqual(len(self.store.load(m['id'])['screenshots']), 1)

def test_caption_by_image_name_survives_turn_removal(self):
    from PIL import Image
    m = self.live()
    shot = self.sm.add_screenshot(m['id'], Image.new('RGB', (2, 2)), self.clock.now().timestamp() + 5)
    self.assertTrue(self.sm.set_caption(m['id'], shot['image'], 'S3 docs'))
    self.assertEqual(self.sm.current()['screenshots'][0]['caption'], 'S3 docs')
    self.assertFalse(self.sm.set_caption(m['id'], 'nope.png', 'x'))

def test_continue_previous_moves_screenshots_and_files(self):
    # Build on the existing continue_previous test's setup in this class: end a
    # meeting, start a new one within gap_minutes, add a shot to each, continue.
    from PIL import Image
    first = self.live()
    self.sm.add_screenshot(first['id'], Image.new('RGB', (2, 2)), self.clock.now().timestamp() + 10)
    self.sm.end()
    self.clock.advance(120)
    second = self.live()
    self.sm.add_screenshot(second['id'], Image.new('RGB', (2, 2)), self.clock.now().timestamp() + 10)
    self.sm.continue_previous()
    cur = self.sm.current()
    self.assertEqual(cur['id'], first['id'])
    self.assertEqual([s['at'] for s in cur['screenshots']], [10.0, 130.0 + 3.5])  # adjust to the clock maths of live()
    self.assertTrue(all(self.store.shot_path(first['id'], s['image']).exists() for s in cur['screenshots']))
    self.assertFalse(self.store.shots_dir(second['id']).exists())

def test_split_resumed_carries_later_screenshots(self):
    from PIL import Image
    m = self.live()
    self.sm.pause(); self.clock.advance(60); self.sm.resume()
    self.sm.add_screenshot(m['id'], Image.new('RGB', (2, 2)), self.clock.now().timestamp() + 5)
    self.sm.split_resumed()
    fresh = self.sm.current()
    self.assertNotEqual(fresh['id'], m['id'])
    self.assertEqual(len(fresh['screenshots']), 1)
    self.assertEqual(self.store.load(m['id'])['screenshots'], [])
    self.assertTrue(self.store.shot_path(fresh['id'], fresh['screenshots'][0]['image']).exists())
```

Read the class's existing `continue_previous` / `split_resumed` tests first and copy their exact clock choreography; the `at` values in the assertions must follow from it (compute them, do not guess).

- [ ] **Step 2: Run**: `python -m pytest tests/test_meeting.py -k "screenshot or caption" -v`. Expected: FAIL.

- [ ] **Step 3: Implement** in `session.py` per Interfaces. `add_screenshot` and `set_caption` run under `with self._transition()` for the live meeting; the saved-meeting path mirrors `_append_saved` (use `self._previous` when ids match). `_end` logs the screenshot count next to the turn count.

- [ ] **Step 4: Run** `tests/test_meeting.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C S:\Koli-meeting-screenshots add src/koli/meeting/session.py tests/test_meeting.py
git -C S:\Koli-meeting-screenshots commit -m "feat(meeting): screenshots join the session, with captions, and follow resume and split"
```

---

### Task 3: Windows capture module and its macOS stub

**Files:**
- Create: `src/koli/platform/windows/screenshots.py`, `src/koli/platform/macos/screenshots.py`
- Modify: `src/koli/platform/__init__.py` (add `screenshots` to both import lines)
- Test: `tests/test_smoke.py` (import test only; capture is hand-tested)

**Interfaces:**
- Produces (both platforms, same names):
  - `available() -> bool` (Windows `True`, macOS `False`).
  - `open_overlay() -> None`: Windows `os.startfile('ms-screenclip:')`.
  - `clipboard_sequence() -> int`: `ctypes.windll.user32.GetClipboardSequenceNumber()`; macOS `0`.
  - `overlay_open() -> bool`: any visible top-level window whose process image name (via `GetWindowThreadProcessId` + `psutil`-free: `OpenProcess` + `QueryFullProcessImageNameW`) ends with one of `SNIP_PROCESSES = ('screenclippinghost.exe', 'snippingtool.exe')`.
  - `grab_snip() -> Optional['PIL.Image.Image']`: `None` unless the clipboard has an image (`ImageGrab.grabclipboard()` returns an `Image`) and `clipboard_owner_is_snip()` is `True` or unknown.
  - `clipboard_owner_is_snip() -> Optional[bool]`: `GetClipboardOwner` → process name test; `None` when there is no owner window or the process cannot be opened.
  - Module header comment explains the owner check is what makes "only snips count" true.

- [ ] **Step 1: Write the failing smoke test** in `tests/test_smoke.py` next to the other platform-import tests:

```python
def test_screenshots_platform_module_has_the_contract(self):
    from koli.platform import screenshots
    for name in ('available', 'open_overlay', 'clipboard_sequence', 'overlay_open', 'grab_snip', 'clipboard_owner_is_snip'):
        self.assertTrue(callable(getattr(screenshots, name)), name)
    self.assertIsInstance(screenshots.clipboard_sequence(), int)
```

- [ ] **Step 2: Run**: `python -m pytest tests/test_smoke.py -k screenshots_platform -v`. Expected: FAIL (ImportError).

- [ ] **Step 3: Implement** both modules and the `__init__` import. Windows helpers use `ctypes.windll.user32` / `kernel32` directly (no new dependency). `grab_snip` imports Pillow lazily inside the function.

- [ ] **Step 4: Run** the smoke test and `python -c "from koli.platform import screenshots; print(screenshots.overlay_open(), screenshots.clipboard_owner_is_snip())"` from the worktree with `PYTHONPATH=src`. Expected: PASS; the print shows `False None` (or `True`/`True` if a snip is on the clipboard).

- [ ] **Step 5: Commit**

```bash
git -C S:\Koli-meeting-screenshots add src/koli/platform/windows/screenshots.py src/koli/platform/macos/screenshots.py src/koli/platform/__init__.py tests/test_smoke.py
git -C S:\Koli-meeting-screenshots commit -m "feat(platform): snip overlay, clipboard sequence and Snipping Tool owner check (Windows; macOS stub)"
```

---

### Task 4: Settings: the mode, the hotkey, hidden on macOS

**Files:**
- Modify: `src/koli/config.defaults.yaml` (`hotkey:` block after `meeting_mute_hotkey`; `meeting:` block after `keep_audio`), `src/koli/settings/schema.py` (hotkeys section after `meeting_mute_hotkey`; meetings section after `meeting.keep_audio`), `src/koli/settings/appliers.py` (`_LISTENER_KEYS`), `src/koli/settings/service.py` (`HOTKEY_PATHS`, `has`), `src/koli/hotkey_listener.py`, `src/koli/main.py`, `src/koli/state_manager.py`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Consumes: `platform.screenshots.available()`.
- Produces:
  - Config: `hotkey.meeting_screenshot_hotkey: ""`, `meeting.screenshots: key`.
  - Schema: `_S('hotkey.meeting_screenshot_hotkey', 'Screenshot into the meeting', 'hotkeys', 'hotkey', nullable=True, help='Opens the Windows snip overlay; the snip joins the running meeting. Does nothing outside a meeting.')` and `_S('meeting.screenshots', 'Screenshots in meetings', 'meetings', 'segmented', mode=SIMPLE, choices=[['key', 'Only with my key'], ['all', 'Every Win+Shift+S snip'], ['off', 'Never']], help='...')`.
  - `SettingsService.has(path)` returns `False` for both paths when `screenshots.available()` is `False` (read `has` first; follow how it hides other platform-only keys).
  - `HotkeyListener(..., meeting_screenshot_hotkey=None)`, config name `'meeting screenshot'`, handler `_meeting_screenshot_hotkey_pressed` → thread → `state_manager.meeting_screenshot()`; `'meeting_screenshot_hotkey'` in `valid_settings`.
  - `StateManager.meeting_screenshot() -> bool`: like `toggle_meeting`; calls `self.meetings.screenshot()` (Task 5) and returns its `ok`.
  - `main.py` passes `meeting_screenshot_hotkey=hotkey_config.get('meeting_screenshot_hotkey')`.

- [ ] **Step 1: Write the failing tests** in `tests/test_smoke.py` (near the schema tests at ~line 4119):

```python
def test_meeting_screenshot_settings_are_in_the_schema(self):
    from koli.settings import schema
    s = schema.get('meeting.screenshots')
    self.assertEqual([c[0] for c in s.choices], ['key', 'all', 'off'])
    self.assertIsNotNone(schema.get('hotkey.meeting_screenshot_hotkey'))
    from koli.settings.appliers import _LISTENER_KEYS
    self.assertEqual(_LISTENER_KEYS['hotkey.meeting_screenshot_hotkey'], 'meeting_screenshot_hotkey')
    from koli.settings.service import HOTKEY_PATHS
    self.assertIn('hotkey.meeting_screenshot_hotkey', HOTKEY_PATHS)
```

- [ ] **Step 2: Run**: `python -m pytest tests/test_smoke.py -k "meeting_screenshot_settings or every_default_has_a_schema" -v`. Expected: FAIL.

- [ ] **Step 3: Implement** all seven files per Interfaces. The yaml comment above `screenshots:` states the three values in one line each.

- [ ] **Step 4: Run** `python -m pytest tests/test_smoke.py -k "schema or hotkey or settings" -q`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C S:\Koli-meeting-screenshots add src/koli/config.defaults.yaml src/koli/settings/schema.py src/koli/settings/appliers.py src/koli/settings/service.py src/koli/hotkey_listener.py src/koli/main.py src/koli/state_manager.py tests/test_smoke.py
git -C S:\Koli-meeting-screenshots commit -m "feat(settings): meeting.screenshots mode and the screenshot hotkey"
```

---

### Task 5: Service: the key flow, the watcher, captions and images

**Files:**
- Modify: `src/koli/services/meetings.py`, `src/koli/ui/events.py` (`TOPICS` doc line), `src/koli/ui/host.py` (`BAR_TOPICS`, `PANEL_TOPICS`, `bootstrap('meeting')`)
- Test: `tests/test_meeting.py` (new class `ScreenshotFlowTests`, building `MeetingService` the way the existing service tests do, with injected `shots` module)

**Interfaces:**
- Consumes: Task 2 session methods, Task 3 platform contract, Task 4 setting.
- Produces:
  - `MeetingService.__init__(..., shots=None)`: `self._shots = shots or platform.screenshots`; `self._expecting: Optional[str]` (meeting id while a snip is expected); `self._watcher: Optional[threading.Thread]`; `self._watch_stop: threading.Event`.
  - `MeetingService.screenshot() -> dict`: `{'ok': False, 'reason': 'no meeting'}` unless state live/paused and mode != `off`; `{'ok': False, 'reason': 'busy'}` while expecting; else sets `_expecting = meeting id`, calls `self.ctx.state_manager._wait_for_modifiers_released()` if present, `open_overlay()`, starts a daemon thread running `_await_snip(meeting_id, seq0)`, returns `{'ok': True}`.
  - `_await_snip(meeting_id, seq0, timeout=90.0, poll=0.15)`: loop until timeout: if `clipboard_sequence() != seq0`: `img = grab_snip()`; if `img`: `self.session.add_screenshot(meeting_id, img, time.time())`, break; else `seq0 = current`. If `not overlay_open()` and at least 0.5 s passed since open with no image: break. Always `finally: self._expecting = None`.
  - Watcher: `_start_watcher()` when a meeting begins and mode is `all`; `_stop_watcher()` on end; loop every 0.25 s: on sequence change and `self._expecting is None`, `grab_snip()` and `add_screenshot(current id, img, time.time())`. `apply_setting('meeting.screenshots', value)` starts or stops it while live.
  - `MeetingService.set_caption(meeting_id, image, caption) -> bool` → session.
  - `MeetingService.image(meeting_id, name) -> dict`: `{'url': 'data:image/png;base64,...'}` from `store.shot_path`.
  - `MeetingService.get()` and `turns()` unchanged; `get()` already returns the whole dict (which now has `screenshots`).
  - `bootstrap('meeting')` adds `data['screenshots'] = svc.get(id).get('screenshots', [])` when live.
  - `host.BAR_TOPICS` and `PANEL_TOPICS` gain `'meeting_shot'`; `events.TOPICS` documents it.

- [ ] **Step 1: Write the failing tests** (`ScreenshotFlowTests`). Build a `FakeShots` object with settable `sequence`, `image`, `overlay`, `opened` counter; construct `MeetingService` like the existing service tests do (look for the class that passes `loopback_factory`, `embedder_factory`, `autostart=False`), with `publish` capturing events and a settings dict holding `'meeting.screenshots'`.

```python
def test_key_flow_records_the_snip_once(self):
    self.settings['meeting.screenshots'] = 'key'
    self.start_live()
    r = self.svc.screenshot()
    self.assertEqual(r, {'ok': True})
    self.assertEqual(self.shots.opened, 1)
    self.shots.sequence += 1; self.shots.image = Image.new('RGB', (2, 2))
    self.wait_until(lambda: self.svc.session.current()['screenshots'])
    self.assertEqual(len(self.svc.session.current()['screenshots']), 1)
    self.assertIsNone(self.svc._expecting)

def test_esc_clears_expecting_and_a_later_snip_stays_private(self):
    self.settings['meeting.screenshots'] = 'key'
    self.start_live()
    self.svc.screenshot()
    self.shots.overlay = False
    self.wait_until(lambda: self.svc._expecting is None)
    self.shots.sequence += 1; self.shots.image = Image.new('RGB', (2, 2))
    time.sleep(0.4)
    self.assertEqual(self.svc.session.current()['screenshots'], [])

def test_all_mode_watcher_records_a_plain_snip_and_ignores_non_snips(self):
    self.settings['meeting.screenshots'] = 'all'
    self.start_live()
    self.shots.sequence += 1; self.shots.image = Image.new('RGB', (2, 2))
    self.wait_until(lambda: self.svc.session.current()['screenshots'])
    self.shots.sequence += 1; self.shots.image = None   # grab_snip says: not a snip
    time.sleep(0.4)
    self.assertEqual(len(self.svc.session.current()['screenshots']), 1)
    self.svc.end()
    self.wait_until(lambda: self.svc._watcher is None)

def test_off_and_no_meeting_do_nothing(self):
    self.settings['meeting.screenshots'] = 'off'
    self.assertEqual(self.svc.screenshot()['ok'], False)
    self.start_live()
    self.assertEqual(self.svc.screenshot()['ok'], False)
    self.assertEqual(self.shots.opened, 0)

def test_snip_after_end_lands_in_the_meeting_pressed_in(self):
    self.settings['meeting.screenshots'] = 'key'
    m = self.start_live()
    self.svc.screenshot()
    self.svc.end()
    self.shots.sequence += 1; self.shots.image = Image.new('RGB', (2, 2))
    self.wait_until(lambda: self.svc.store.load(m['id'])['screenshots'])

def test_image_returns_a_data_url_and_caption_saves(self):
    self.start_live()
    shot = self.svc.session.add_screenshot(self.svc.session.current()['id'], Image.new('RGB', (2, 2)), time.time())
    self.assertTrue(self.svc.image(self.svc.session.current()['id'], shot['image'])['url'].startswith('data:image/png;base64,'))
    self.assertTrue(self.svc.set_caption(self.svc.session.current()['id'], shot['image'], 'hi'))
```

- [ ] **Step 2: Run**: `python -m pytest tests/test_meeting.py -k ScreenshotFlow -v`. Expected: FAIL.

- [ ] **Step 3: Implement** per Interfaces. The wait in `screenshot()` for modifiers goes through `getattr(self.ctx.state_manager, '_wait_for_modifiers_released', None)` so tests with a bare ctx pass.

- [ ] **Step 4: Run** `tests/test_meeting.py` and `tests/test_smoke.py -k "events or topics"`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C S:\Koli-meeting-screenshots add src/koli/services/meetings.py src/koli/ui/events.py src/koli/ui/host.py tests/test_meeting.py
git -C S:\Koli-meeting-screenshots commit -m "feat(meeting): the key flow and the every-snip watcher put screenshots into the meeting"
```

---

### Task 6: Bridge and export copy

**Files:**
- Modify: `src/koli/ui/bridge.py` (after `meeting_export`)
- Test: `tests/test_smoke.py` (the bridge method list test; find `def test_bridge` names and extend)

**Interfaces:**
- Produces: `meeting_screenshot()` → `svc.screenshot()`; `meeting_caption(meeting_id, image, caption)` → `svc.set_caption`; `meeting_image(meeting_id, image)` → `svc.image`. `meeting_export(meeting_id, fmt)`: when the meeting has screenshots and `fmt == 'md'`, copies each PNG into `<picked path without extension>-shots/` and calls `svc.export(meeting_id, fmt, shots_folder=<that folder's name>)`; `MeetingService.export` gains the `shots_folder=''` kwarg passed to `store.export` → `export_text`.

- [ ] **Step 1: Write the failing test**: extend the smoke test that asserts bridge methods exist (grep `meeting_export` in `tests/test_smoke.py`) with `meeting_screenshot`, `meeting_caption`, `meeting_image`. Add a store-level test in `StoreTests`: `self.store.export(m['id'], 'md', shots_folder='x-shots')` contains `(x-shots/`.

- [ ] **Step 2: Run** them. Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run** `tests/test_smoke.py -k bridge` and `tests/test_meeting.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C S:\Koli-meeting-screenshots add src/koli/ui/bridge.py src/koli/services/meetings.py src/koli/meeting/store.py tests/test_smoke.py tests/test_meeting.py
git -C S:\Koli-meeting-screenshots commit -m "feat(meeting): bridge calls for screenshots, captions and images; md export copies the PNGs"
```

---

### Task 7: Pages: panel, Meetings screen, bubble, mock

**Files:**
- Modify: `src/koli/ui/web/js/meeting.js`, `src/koli/ui/web/css/meeting.css`, `src/koli/ui/web/js/screens/meetings.js`, `src/koli/ui/web/js/bar.js`, `src/koli/ui/web/js/mock-api.js`, `src/koli/ui/web/js/ui.js` (only if no `camera` icon exists; add one)
- Create: `src/koli/ui/web/js/rows.js` with `export function rowsByTime(turns, screenshots)` (same rule as the Python `rows_by_time`)

**Interfaces:**
- Consumes: events `meeting_shot {id, shot, caption?}`, bridge `meeting_screenshot`, `meeting_caption`, `meeting_image`; bootstrap `screenshots`; `meeting.screenshots` in the `meeting` payload.
- Produces:
  - Panel: store gains `screenshots: []`; `on('meeting_shot')` appends (or replaces by image name when `caption`); `Turns()` renders `rowsByTime(turns, screenshots).slice(-200)`; a shot row is `<div class="turn shot"><span class="who">Screenshot</span><${Thumb} .../></div>`; `Thumb` fetches `meeting_image` once per name (cache in a module map) and shows an `<img>` ~160 px wide; click opens `<div class="lightbox">` with the full image and an Esc/click-to-close. Footer gets `${m.screenshots !== 'off' && html`<button class="btn icon" aria-label="Screenshot" title="Snip into the meeting" onClick=${() => call('meeting_screenshot')}><${Icon} name="camera" /></button>`}` before "Open in Koli".
  - Meetings screen `Transcript`: `on('meeting_shot')` updates `m.screenshots`; rows from `rowsByTime`; a shot row shows time, "Screenshot", the thumb and an inline caption `<input>` (id `cap-<image>`) saved on blur/Enter via `meeting_caption`, then `toast('Caption saved')`. `LiveCard` gets the Screenshot button when `m.screenshots !== 'off'`.
  - Bubble: hover row and quick menu get "Screenshot" (`call('meeting_screenshot')`) when `MEETING_ON.has(meeting.state) && meeting.screenshots !== 'off'`; `on('meeting_shot', (p) => !p.caption && flash('ok', 'Screenshot added'))`.
  - Mock: saved meeting gets one screenshot `{at: 12, image: '0012.00.png', caption: 'The bridge dashboard', description: null}`; `meeting_image` returns a small inline PNG data URL (a 160×100 grey rectangle generated once with a canvas in the mock); `meeting_screenshot` emits `meeting_shot` after 800 ms; `meeting_caption` updates it; `emptyMeeting` gets `screenshots: 'key'`.

- [ ] **Step 1: Write the failing test**: `tests/test_smoke.py` has tests that read page sources for strings (grep `bar.js` in it); add one asserting `meeting.js`, `screens/meetings.js` and `bar.js` contain `meeting_screenshot`, and that `rows.js` exists and exports `rowsByTime`. Also a node check: `node --input-type=module -e "import('./src/koli/ui/web/js/rows.js').then(m => { const r = m.rowsByTime([{start:120},{start:90}], [{at:100,image:'a'}]); if (r.map(x=>x.kind).join() !== 'turn,shot,turn') throw new Error(JSON.stringify(r)) })"` recorded in the plan as the verification command (run it by hand in Step 4; do not add it to the Python tests).

- [ ] **Step 2: Run** the smoke test. Expected: FAIL.

- [ ] **Step 3: Implement.** CSS: `.turn.shot img { max-width: 160px; border-radius: 6px; border: 1px solid var(--line); cursor: zoom-in }`, `.lightbox { position: fixed; inset: 0; background: rgba(0,0,0,.7); display: grid; place-items: center; z-index: 50 } .lightbox img { max-width: 94vw; max-height: 90vh }`. Follow the Meetings screen's inline-style grid for its rows.

- [ ] **Step 4: Verify**: the smoke test passes; the node command prints nothing; open `index.html?mock=1#meetings` and `meeting.html?mock=1&meeting` from a static server (`python -m http.server` in `src/koli/ui/web`) and confirm the shot row, the thumbnail, the lightbox, the caption save and the bubble button in `bar.html?mock=1&meeting`.

- [ ] **Step 5: Commit**

```bash
git -C S:\Koli-meeting-screenshots add src/koli/ui/web/js/meeting.js src/koli/ui/web/css/meeting.css src/koli/ui/web/js/screens/meetings.js src/koli/ui/web/js/bar.js src/koli/ui/web/js/mock-api.js src/koli/ui/web/js/rows.js src/koli/ui/web/js/ui.js tests/test_smoke.py
git -C S:\Koli-meeting-screenshots commit -m "feat(ui): screenshots in the meeting panel, the Meetings screen and the bubble"
```

---

### Task 8: Docs and changelog

**Files:**
- Modify: `docs/design/meeting-mode.md` (JSON block: add `"screenshots": [...]`; Out of scope: remove nothing, add a pointer to the screenshots design), `docs/project-index.md` (Meeting Mode row mentions screenshots; platform table row for `screenshots.py`), `CHANGELOG.md` (Unreleased: one entry), `docs/design/meeting-screenshots.md` (Status line: "built on feat/meeting-screenshots").

- [ ] **Step 1: Edit** the four files.
- [ ] **Step 2: Run** the full suite: `python -m pytest tests/test_meeting.py tests/test_smoke.py -q`. Expected: all PASS.
- [ ] **Step 3: Commit**

```bash
git -C S:\Koli-meeting-screenshots add docs/design/meeting-mode.md docs/project-index.md CHANGELOG.md docs/design/meeting-screenshots.md docs/plans/2026-10-19-meeting-screenshots-implementation.md
git -C S:\Koli-meeting-screenshots commit -m "docs(meeting): screenshots in the meeting design, index and changelog"
```

---

### Task 9: Real test on Windows (by hand, with the user)

Launch from the worktree (`run-koli.py`, `PYTHONUTF8=1`), start a meeting by hand, and check: the hotkey (bind one in Settings first) opens the overlay and the snip appears in the panel within a second; Esc in the overlay leaves nothing; in `all` mode a plain Win+Shift+S appears and a copied browser image does not; the bubble flashes; the Meetings screen shows the row and saves a caption; md export writes the `-shots` folder; delete removes the folder. Report results before merging.
