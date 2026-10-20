# Meeting screenshots

Status: built on `feat/meeting-screenshots` (plan:
`docs/plans/2026-10-19-meeting-screenshots-implementation.md`); design agreed
in chat on 2026-10-19. Mockup of how the brief shows
them: https://claude.ai/artifact/MVAG73s6WYgkmqWV5qdi9G (the "Example"
placeholders). Extends `docs/design/meeting-mode.md`.

## Goal

While a meeting is recording, a screenshot you take becomes part of the
transcript: a timestamped entry between the spoken turns, with the PNG kept
beside the meeting. Later, a reader following the meeting sees the screen as
it was when that part was said: in the live panel, on the Meetings screen,
in the exports, and in any brief or AI pass built from the meeting file.

Why: the transcript says what people said; it loses what they were looking
at. The S3 docs David showed, the SEO report page, the vendor pricing: those
are what the words were about.

## Goals, non-goals, anti-goals

Goals
- You choose, snip by snip, what the meeting gets. Nothing is captured
  without you pressing something.
- The snipping experience stays the Windows one you already know. Koli opens
  the same overlay; it never draws its own.
- A screenshot survives everything a turn survives: pause, resume, "continue
  previous", "split", rename, delete.
- The meeting JSON stays the single source for the brief and for agents.

Non-goals (now)
- Reading the image with AI (a description, the text on it). The data shape
  reserves a field; nothing fills it yet.
- Video or periodic automatic captures.
- macOS capture. The platform module is a stub; the setting is hidden there.
- A size cap or eviction for the PNGs. They are deleted with the meeting.

Anti-goals
- Grabbing images the user copied for other reasons (an image copied in a
  browser) while a meeting runs. Only snips count.
- Counting Koli's own snip twice (the key flow and the watcher both see it).
- A capture while no meeting is live doing anything at all.

## Behaviour (agreed)

**Setting: `meeting.screenshots`**, segmented, Simple mode:

| value | label | what happens while a meeting is live |
|---|---|---|
| `off` | Never | nothing; no button, no key |
| `all` | Every Win+Shift+S snip | any snip taken with the Windows overlay joins the meeting |
| `key` | Only with my key | only snips started by Koli (its hotkey or its button) join; a plain Win+Shift+S stays private |

Default: `key`. (My call: it is the private-by-default choice and still gives
the button.)

**Hotkey: `hotkey.meeting_screenshot_hotkey`**, unbound by default, in the
hotkeys section next to the meeting hotkeys. Pressing it while a meeting is
live opens the Windows snip overlay; the snip you take joins the meeting.
Outside a meeting the key does nothing (my call: no overlay, so the key can't
be mistaken for a general screenshot tool).

**Bubble button.** While a meeting is live or paused and the setting is not
`off`, the bubble's hover row and quick-actions menu get "Screenshot" next
to "Stop meeting". It does what the key does. After the snip lands, the
bubble flashes "Screenshot added" the way it flashes "Meeting saved".

**Panel button.** The meeting panel footer gets the same button, same
condition.

**What a screenshot entry is.** The meeting file gets a second list next
to `turns`, `screenshots`, one entry per image:

```json
"screenshots": [
  {"at": 279.4, "image": "0279.40.png", "caption": "", "description": null}
]
```

- `at` is the meeting offset at the moment the image landed on the
  clipboard (not when the overlay opened), seconds from `started_at` like
  every time in the file.
- `image` is the file name inside the meeting's shots folder, and the key
  a caption edit uses (list indices shift; names don't).
- `caption` is empty until the user writes one. It is what exports print
  and what the brief uses as the title.
- `description` is reserved for a later AI pass; `null` now.

Why a separate list and not a turn (my call, after the reviewer's check):
`turns` is append-only in arrival order and the session leans on that
(resume indexes, the echo window, the pages appending live turns). Spoken
turns from the mic and the loopback already interleave out of `start`
order, so every reader of the file already orders by time when it shows a
transcript; a screenshot list with `at` joins that merge with no special
case, and the brief's mockup reads exactly this shape. The echo filter and
the speaker merge never see screenshots at all.

**Where the PNG lives.** `<data>/meetings/<id>.shots/<offset>.png`, a
sibling of `<id>.audio/`, named by the offset with two decimals so the
folder sorts in meeting order. The store deletes it with the meeting.

**Ordering when shown.** Every view (panel, Meetings screen, exports, the
brief) merges `turns` and `screenshots` by time: a screenshot sits before
the first spoken turn whose `start` is later than its `at`. Transcription
runs behind real time, so a spoken turn that arrives after the screenshot
can still belong before it; the merge handles that because it is done at
display time, not at insert time.

**Resume and split.** `continue_previous` moves the new meeting's shots
into the previous meeting's folder before deleting the new meeting (today
`delete` rmtrees the audio folder; the shots folder gets the same
treatment, so the move must come first); a name collision (both meetings
have a `0010.00.png`) renames the moved file and updates its entry.
`split_resumed` moves the entries whose `at` falls in the resumed part and
their files. Offsets shift with the turns.

**Edge cases the builder settles as stated here:** a snip that lands after
the meeting ended is stored into the meeting that was live when the key
was pressed (carry the id from press time, as audio chunks do). The `all`
watcher keeps running through a pause (a paused meeting still accepts
turns). A capture during a gap is kept; its `at` places it.

**Live panel.** A screenshot renders as a row with a thumbnail (about
160 px wide) and the caption if any. Clicking it opens the full image in an
in-page overlay (no new window). The panel stays hidden from sharing as it
is today, so the thumbnails are too.

**Meetings screen.** Same row in the stored transcript, plus "Add a caption"
inline on the row (a text field, saved on blur or Enter through a new
bridge call). Full image on click, in-page overlay.

**Images reach the pages** as `data:image/png;base64,...` from a bridge call
`meeting_image(meeting_id, image)`, the same pattern Training uses for
clips. The pages' CSP already allows `img-src data:`. Thumbnails: the page
scales the full image with CSS; no separate thumbnail files (my call: the
meeting has a handful of shots, not hundreds).

**Exports.**
- `txt`: `[04:39] Screenshot: <caption or image name>`.
- `md`: `![caption](<export-basename>-shots/0279.40.png)` on its own line,
  and the bridge copies the PNGs into `<export-basename>-shots/` next to the
  file the user picked. Export stays "one text file" when there are no
  screenshots.

**Brief and agents.** The meeting JSON already carries everything: a reader
(the hand-built brief of 2026-10-19, or an agent) places each screenshot by
`start` inside the topic that covers it and at its moment in the transcript,
exactly as the mockup does. No new export format.

**Sound.** None. The Windows overlay has its own feedback; the bubble flash
is the confirmation. (My call.)

## Capture mechanics (Windows)

New module `platform/windows/screenshots.py`, mirrored by a macOS stub
(`available() -> False`, everything else a no-op). The contract:

- `open_overlay()`: `os.startfile('ms-screenclip:')`. Called after waiting
  for held modifiers to release, as paste does, so the user's own key chord
  does not leak into the overlay.
- `clipboard_sequence() -> int`: `GetClipboardSequenceNumber`.
- `overlay_open() -> bool`: whether the Windows snip overlay is on screen
  (enumerate top-level windows, match the owner process name).
- `grab_snip() -> PIL.Image | None`: `PIL.ImageGrab.grabclipboard()` if the
  clipboard holds a bitmap **and** the clipboard owner's process is the
  Snipping Tool (`ScreenClippingHost.exe` or `SnippingTool.exe`, via
  `GetClipboardOwner` → `GetWindowThreadProcessId` → process name). The
  owner check is what makes "only snips count" true. If the owner cannot be
  read (no window, access denied), the image is accepted (my call: better
  one stray image than a missed snip).

**The key flow** (hotkey, bubble button, panel button all call
`MeetingService.screenshot()`):
1. Not live or paused, or setting `off`: return `{'ok': False, 'reason':
   'no meeting'}`; the UI shows nothing.
2. Remember the clipboard sequence number, mark "a snip is expected", open
   the overlay.
3. Poll every 150 ms, for up to 90 s: the clipboard sequence number and
   whether the overlay window is still there (`overlay_open()`: a top-level
   window owned by `ScreenClippingHost.exe` or `SnippingTool.exe`). On a
   sequence change, try `grab_snip()`. On an image: save the PNG, add the
   entry, publish `meeting_shot` (the pages add the row; the bubble
   flashes), clear the mark. When the overlay is gone with no image (the
   user pressed Esc) the mark clears within one poll, so a plain
   Win+Shift+S a second later is private again in `key` mode. The 90 s is
   only the backstop for an overlay Koli cannot see.
4. Second press while a snip is expected: ignored.

**The watcher** (`all` mode only): a thread the service starts when a
meeting goes live and stops when it ends, polling the sequence number every
250 ms. On change, `grab_snip()`; if it returns an image and no snip is
expected by the key flow, it records it the same way. While the key flow
expects a snip, the watcher yields, so one snip is never two entries.

Resource cost of polling: one Win32 call every 250 ms while a meeting runs;
no window, no message loop, no hooks. If this ever shows in the profiler,
`AddClipboardFormatListener` on a hidden window is the upgrade.

**Both modes:** keys Koli pressed itself (`keyboard.sent_recently()`) are
never the trigger; the clipboard is.

## Settings and plumbing

- `config.defaults.yaml`: `meeting.screenshots: key` with a comment;
  `hotkey.meeting_screenshot_hotkey: ""`.
- `settings/schema.py`: the segmented entry (section `meetings`, Simple
  mode, read-on-use) and the hotkey entry (nullable, like
  `meeting_mute_hotkey`).
- `settings/appliers.py`: the hotkey joins `_LISTENER_KEYS`;
  `meeting.screenshots` is `_READ_ON_USE` except that switching to or from
  `all` during a live meeting starts or stops the watcher
  (`MeetingService.apply_setting`).
- `hotkey_listener.py`, `main.py`, `settings/service.py`,
  `state_manager.py`: the new hotkey follows `meeting_mute_hotkey` exactly;
  the handler calls `state_manager.meeting_screenshot()` on a thread.
- `services/meetings.py`: `screenshot()`, `set_caption(meeting_id, image,
  text)`, `image(meeting_id, name)`, the watcher, and `state_payload` gains
  `screenshots: <mode>` so the bubble and panel know whether to show the
  button. The setting is hidden on macOS through `SettingsService.has`.
- `ui/bridge.py`: `meeting_screenshot`, `meeting_caption`, `meeting_image`.
  `meeting_export` copies the shots folder for `md`. `meeting_get` and
  `bootstrap('meeting')` return `screenshots` with the turns.
- `ui/events.py` and `ui/host.py`: new topic `meeting_shot` carrying
  `{'id', 'shot': {...}}`; it joins `BAR_TOPICS` and `PANEL_TOPICS`.
- `meeting/store.py`: `new_meeting` adds `"screenshots": []`, `shots_dir`,
  `save_shot(meeting_id, offset, image) -> name`, `move_shots(from_id,
  to_id, names)`, delete covers the folder, `header` adds a `screenshots`
  count, `export_text` merges by time and prints screenshot lines;
  `duration_s` takes the later of the last turn and the last screenshot
  while a meeting is open.
- `meeting/session.py`: `add_screenshot(meeting_id, offset, name)`;
  `continue_previous` and `split_resumed` shift `at` with the turns and
  move files. Echo removal and speaker merge are untouched.
- Pages: a shared `mergeByTime(turns, screenshots)` helper in
  `js/screens/shared.js` (or next to `who()`), used by `js/meeting.js` and
  `js/screens/meetings.js` for the row list; `js/meeting.js` screenshot row,
  `js/screens/meetings.js` row + caption + overlay, `js/bar.js` button +
  flash, `mock-api.js` and `mock-data.json` with one example screenshot so
  `?mock=1` shows it.
- Docs: `docs/design/meeting-mode.md` JSON block gets the `screenshots` list;
  `docs/project-index.md` platform table gets `screenshots.py`; CHANGELOG.

## Tests

- `tests/test_meeting.py`: `export_text` places a screenshot at 100 s
  before a spoken turn at 120 s and after one at 90 s, even when the 90 s
  turn was added after the screenshot; `at` shifts on `continue_previous`
  and a colliding file name is renamed with its entry updated;
  `split_resumed` carries the right entries and files; delete removes the
  folder; a caption edit by image name survives an echo removal that shifts
  turn indices.
- `tests/test_smoke.py`: schema covers the new keys; the bridge has the
  three new methods.
- Capture itself (overlay, clipboard owner) is tested by hand on Windows:
  all three modes, Esc in the overlay, a second press during an open
  overlay, an image copied from a browser in `all` mode (must be ignored).

## Left to the builder

- Where the merge-by-time helper lives on the Python side (store, since
  export needs it) and whether the pages get the merged list from Python
  or merge themselves (the doc assumes the pages merge, so live events stay
  one list each).
- Thumbnail CSS and the overlay markup; reuse the panel's existing styles.
- How the panel's 200-row window counts screenshot rows (they count as
  rows).
- The caption field's exact bridge signature.
- Whether `grab_snip` converts palette or RGBA images before saving
  (save whatever Pillow gives as PNG).
