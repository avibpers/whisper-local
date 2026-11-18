# Koli in the meeting

*2026-11-18. `meeting/companion.py`, `services/companion.py`, `meeting/guest.py`
with `meeting/guest_board.js`, the `browser/` package, the panel section
`ui/web/js/meeting_companion.js`. The owner described the modes and the actions,
asked for a test before any plan, and then said: design, plan and build to the
end. The spike that this rests on is in
[`docs/research/meet-participant-spike/`](../research/meet-participant-spike/).*

## Why

Koli already writes down a meeting. The owner wants her **in** it: following what
is said while it is said, helping him answer, keeping the tasks, and sitting in a
Google Meet call as her own participant whose tile shows what she noted. Text
now, a voice later.

## What it is

Two parts on one "brain". The first works in every call app and needs nothing
new installed. The second is an add-on for Google Meet that may fail without
taking the first down.

| Part | Who sees it | Where |
|---|---|---|
| **Koli beside you** | only you | a section at the top of the meeting panel |
| **Koli in the call** | everyone in the call | her own tile ("Koli"), drawn by her own browser |

### The modes (`meeting.companion.mode`, one picker in the panel)

| Mode | What Koli does |
|---|---|
| `off` — Just listen | As before: the transcript, nothing else. No AI calls. |
| `insights` | Keeps short notes while the meeting runs: the topic now, decisions, tasks, open questions. The buttons work. |
| `active` | Insights, and when someone other than you asks a question she shows the question with an answer you could give. In the call she may also answer in the chat when someone speaks to her by name (`meeting.companion.chat`). |

### Koli beside you

```
┌ Koli · Active ▾                      Koli is in the call  [Take out] ┐
│ Now: pricing for the Pro plan                                        │
│ ❓ Eran asked you: "when can we ship?"                                │
│    QA ends Thursday, so a release on Monday is realistic.            │
│    [Copy] [Send to chat] [Task] [×]                                  │
│ Tasks 3 · Decisions 2 ▸                                              │
│ [What now?] [What should I answer?] [Catch me up] [+ Task] [Ask…]    │
└──────────────────────────────────────────────────────────────────────┘
```

| Button | What happens |
|---|---|
| **What now?** | The notes are brought up to this second and the topic line and summary are shown. |
| **What should I answer?** | A card: what is being asked or argued right now, and two or three sentences you could say. |
| **Catch me up** | A card: the last five minutes in three lines. |
| **+ Task** | The task being talked about in the last minute and a half is added to this meeting's tasks. A task Koli noted by herself has a + to take it. |
| **Ask…** | Opens the meeting in the app window, where the Chat tab takes typed questions. The panel never takes the keyboard, so it has no text box. |
| **Copy** (on a card) | Copies through the app; a window without focus can't use the clipboard. |
| **Send to chat** (on a card, only while Koli is in the call) | Koli writes it in the call's chat. |

Tasks are saved in the meeting (`tasks`), shown in the summary's text for the
AI ("include each in the action items"), and survive Continue previous and
Start new instead.

### Koli in the call

When `meeting.companion.join_call` is on (or you press **Bring Koli in**), and
the meeting is a Google Meet call you joined, Koli opens her own browser with no
window, joins the call as a guest named `meeting.companion.name`, and shows a
board as her camera. The board carries only what the meeting itself said: the
topic, decisions, tasks, open questions. Nothing from the private side
(answers for you, catch-ups, other meetings) can reach it: it is drawn from a
separate public view with a fixed list of fields.

She says who she is: the board's second line is "AI notes · with ‹your name›",
and with `meeting.companion.announce` she writes one chat line when she joins.

## The calls made for the owner

Each is a default he can change, listed here so he can reject any.

1. **Everything is off until turned on.** `mode: off`, `join_call: false`,
   `chat: false`. A participant others see, and words in a chat, are not things
   to start by themselves.
2. **One mode picker**, kept as a setting (the panel changes the setting), not a
   per-meeting state.
3. **A feature of its own for the model** (`ai.companion.model`, else the main AI
   model): notes every 45 seconds want a fast, cheap model, the summary a strong
   one.
4. **Guest, no Google account.** Measured: she joins as a guest.
5. **Her browser is Chrome for Testing**, a fixed version, downloaded once
   (215 MB) into `<data>/browser/`. The small headless-only build was refused.
6. **The board follows your tile.** The extension reports the size of Koli's
   tile on your screen; her frame takes that shape, so you see all of it. Other
   people with another layout may see the sides cut; the text is kept in the
   middle for them.
7. **Answers that go to the call use this meeting only**, never the search over
   your other meetings. A guest's question must not pull out what was said
   elsewhere.
8. **She does not read the chat.** Meet stops drawing a hidden page, and hidden is
   what makes her cheap. She hears through your own transcript.
9. **macOS: the panel part works, the call part says "not on macOS yet".**

## What the test measured (2026-11-18, a real call)

| Fact | Consequence |
|---|---|
| Name pasted and Join clicked in one step: refused 6 of 6 ("You can't join this video call") | Input goes the way a person does it |
| Pointer moved along a path, name typed key by key: joined 8 of 8 | `browser/human.py` |
| Chrome for Testing 158 and Chromium 156 joined with no window; `chrome-headless-shell` refused twice at load | Ship Chrome for Testing |
| After Join, a dialog ("Gemini is taking notes": Leave / Join now) keeps her out until answered | The join presses it |
| The name box exists before it is laid out; a sign-in hint can cover it | Wait for a width, press "Got it" |
| A still canvas sends 0 frames | The board redraws a dot once a second |
| Page marked hidden after the join: CPU 49% → 13% of one core, board still sent, chat still sent | Join visible, then hide |
| A 640×480 page changes the pre-join layout | Join at 1100×760 |
| Meet fills a tile and cuts the rest: a 16:9 frame in a 250×305 tile shows 47% of its width, 1:1 80%, 3:4 all | The frame takes the tile's shape |
| Meet names the sizes it wants (960×540 and 480×270), none when nobody shows her | Big type when only the small one is asked for |
| Memory about 1.3 GB while she is in a call | Said in the setting's help |

The Python port joined the same call on 2026-11-18 (`tools/guest_meet_check.py
--join`): in the call 24 s after it started, the board changed, a chat message
went out while hidden, and she left. Its steps before Join passed on Meet's
real page 8 times, 4 on each of the two pages Meet shows.

Not measured: a meeting where a guest must be admitted ("Ask to join"), a call
longer than about 15 minutes, Hebrew on the board in a real call, her answer
when spoken to, and the whole feature inside the running app.
Vexa reports Meet refusing about half of guest joins at random; eight of eight
here does not prove otherwise, so a refusal is retried and shown, never fatal.

## How it is built

```
transcript turns ──► SessionManager.add_turn ──publish 'meeting_turn'──┐
                                                                       ▼
                               services/companion.py  CompanionService (one worker thread)
                                 │  meeting/companion.py: notes, answer, catch-up, task (prompts, parsing)
                                 │  llm.complete('companion', …)   usage → for_meeting(id, 'companion')
                                 ├─► publish 'meeting_companion' ──► panel section (meeting_companion.js)
                                 ├─► session.set_tasks(...)        ──► the meeting file
                                 └─► meeting/guest.py  MeetGuest ──► browser/ (Chrome for Testing over CDP)
                                        public_view(notes) → guest_board.js draws → Meet sees a camera
```

### The brain (`meeting/companion.py`, no threads, no I/O)

- **Notes** are one small JSON: `topic`, `summary` (≤3 lines), `decisions` (≤8),
  `tasks` (≤10, `{task, owner}`), `questions` (≤5), and in Active mode `ask`
  (`{who, question, for_you, answer, points}` or null).
- **An update** sends the model the notes so far and only the new lines
  (`[m:ss] Name: text`, at most 6,000 characters), and gets the whole notes
  back. So a two-hour meeting costs the same per update as a ten-minute one.
- An update runs when `update_seconds` (45) have passed **and** at least 25 new
  words were said; never while paused; a button forces one.
- **Answers and catch-ups** send the notes and the last minutes of lines.
- Everything is written in the language the meeting is spoken in.
- `public_view(notes, owner)` returns the only fields the board and the chat may
  use. A test holds the list.
- Being spoken to by name: a new line that contains her name (or how Whisper and
  Meet write it: Koli, Coley, Collie, Kohli, קולי) and a question. In Active
  mode with `chat` on and Koli in the call, she answers in the chat, at most
  once every 20 seconds, at most 400 characters.

### The service (`services/companion.py`)

- Subscribes to `meeting` and `meeting_turn` on the bus. The turn subscriber runs
  under the session's lock, so it only notes "something new" and returns. Turns
  have no id: the worker keeps the time of the last line it read and re-reads
  the list when a `reload` arrives.
- One worker thread takes jobs (update, answer, catch-up, task, say). A failed AI
  call becomes a card with the error, not a crash.
- Publishes `meeting_companion`: `{id, mode, ready, busy, notes, cards, tasks,
  guest: {state, detail, progress}}`.
- Bridge calls: `companion_state`, `companion_now`, `companion_answer`,
  `companion_catch_up`, `companion_task`, `companion_task_take`,
  `companion_task_remove`, `companion_dismiss`, `companion_copy`,
  `companion_say`, `companion_join`, `companion_leave`. The mode is changed
  with `settings_set`, like any setting.
- It is fed through `on_event`; in the app the meeting service subscribes it
  to the event bus.

### Koli's browser (`browser/`)

Hers for this and for later actions, like each soul's own Chrome in Soul.

| File | Does |
|---|---|
| `install.py` | Downloads the pinned Chrome for Testing zip with progress (a task), unpacks it into `<data>/browser/chrome-<version>/`, removes older versions |
| `process.py` | Starts it with its own profile (`<data>/browser/profile/`), on a free debugging port; closes it; ends leftovers of that profile from a crashed run |
| `cdp.py` | The DevTools Protocol over `cloud_stt/ws.py`: one reader thread, calls answered by id, sessions |
| `human.py` | A pointer path of small uneven steps then press and release; typing one key at a time with uneven pauses |
| `platform/windows/child_jobs.py` | A job object that ends the browser when Koli ends, however she ends |

Launch flags, as measured: `--headless=new`, `--remote-debugging-port=<free>`,
`--user-data-dir`, `--no-first-run`, `--no-default-browser-check`,
`--mute-audio` (the loopback capture must not hear the call twice),
`--lang=en-US`, `--use-fake-device-for-media-stream` (the real camera and
microphone cannot be opened even if the page script failed),
`--window-size=1100,760`. The user agent has "Headless" removed. `Runtime.enable`
is never sent.

### The guest (`meeting/guest.py`, `meeting/guest_board.js`)

States: `off → installing → starting → joining → asking → in_call → leaving → off`,
and `refused` (retried up to 3 times, 8 s apart), `failed`, `unsupported`.

The join, in the order that worked:
1. Grant camera and microphone to `meet.google.com`; add `guest_board.js` to
   run before the page's scripts; open `https://meet.google.com/<code>?hl=en`.
   A browser that doesn't answer right after it started (one start in ten
   lost an answer) is started again.
2. Wait for the name box to have a width. A refusal screen here → `refused`.
   Meet sometimes shows "Continue as guest" and "Sign in with Google" first:
   she presses the first, once. The box is then labelled "Enter your name".
3. Press "Got it" if the sign-in hint is there. Find the box again (it moves
   while the page settles), click it, type the name, read it back; if it did
   not land, once more. Join is never pressed with no name.
4. Click "Join now" or "Ask to join". With "Ask to join": `asking`, up to 3
   minutes, and the panel says "Koli is asking to join; admit her in Meet".
5. A dialog with "Join now" → press it.
6. In the call when the leave button is there and no refusal text. Mute her
   microphone (it only sends silence until she has a voice), open the chat
   panel, write the announcement, then mark the page hidden.

While in the call, every 5 seconds: still in the call? which sizes Meet asks
for? what shape is her tile on your screen? Then the board is redrawn if any of
the three, or the public view, changed.

`guest_board.js` replaces `getUserMedia` and `enumerateDevices` with a canvas
track ("Koli Board") and a silent audio track ("Koli Voice", where a voice goes
later), refuses screen sharing, and keeps the page's `RTCPeerConnection`s to
read what is being sent. The board wraps its text to the frame, writes Hebrew
right to left, and scales type with the frame.

She leaves when the meeting ends, when you leave the call, when you press Take
out, or when Koli quits (`MeetingService.shutdown`). Continue previous and
Start new instead change the meeting, not the call: she stays, and her notes
start over.

### Only what the call heard

Koli's own Whisper hears your microphone whether or not it is muted in Meet:
an aside to someone in your room is in the transcript. So from the moment she
is brought into a call, and for the rest of that meeting:

- her notes start over and are made only from lines the call heard: other
  people's words, and yours only when your microphone was on in Meet. The
  extension reports the microphone button's state (`muted`, read from
  `data-is-muted`); `MeetFeed.unmuted_during` answers for a stretch of time,
  a mute counting from 1.5 s before it was noticed; the meeting service marks
  such a turn `heard`. A turn from Meet's captions is marked too. No captions
  are needed: it works the same when Koli transcribes the audio herself;
- she answers in the chat only a line the call heard, and the answer is drawn
  only from such lines and from the part of the notes her board shows;
- your private buttons (What should I answer?, Catch me up, + Task) still use
  everything, since only you see them.

### What the extension adds

`meet.js` sends, with each status, the size of the tile whose name is the
guest's (`guest_tile: [w, h]`); the name comes in Koli's reply (`guest_name`).
`MeetFeed` keeps it, and leaves the guest out of the people list, so Koli is
never an attendee, a name for a 1:1 call, or a row in the summary's roster.

## Settings

| Setting | Default | |
|---|---|---|
| `meeting.companion.mode` | `off` | off / insights / active |
| `meeting.companion.update_seconds` | 45 | 20–300 |
| `meeting.companion.join_call` | false | Koli joins Google Meet calls with her own tile |
| `meeting.companion.name` | `Koli` | her name in the call |
| `meeting.companion.announce` | true | one chat line when she joins |
| `meeting.companion.chat` | false | Active: answer in the chat when spoken to by name |
| `ai.companion.model`, `ai.companion.timeout` | '' , 20 | else the main AI model |

## Known limits

- Meet may refuse a guest for no visible reason; she retries three times and the
  panel says so.
- Meet's page changes. The join reads labels in English (`?hl=en`, `--lang`).
  When a step isn't found the guest stops with `failed` and what it looked for;
  the panel part goes on.
- The debugging port is open to any program on this PC while she is in a call.
- The download has no published checksum; every file in the zip is tested
  before it is unpacked.
- While she is in the call, her notes hold only what the call heard (see
  "Only what the call heard"). That needs the extension of this version (0.3.0):
  with an older one Koli can't tell whether your microphone was on in Meet,
  and leaves your own words out of them.
- She can't read the chat or be asked there.
- Not in this version: her voice, answers that draw on your earlier meetings
  (the search waits on a local model, too slow for a live answer; the brain
  already takes a `past` text), "explain this term", a warning before the end
  about questions nobody answered, calls other than Google Meet for the tile.
