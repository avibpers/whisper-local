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
| `active` | Insights, and when someone other than you asks a question she shows the question with an answer you could give. In the call she answers on her tile: whoever speaks to her by name, and anything you say when you are alone with her (see "Talking with her"). |

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
7. **Answers that go to the call never use the search over your other
   meetings.** A guest's question must not pull out what was said elsewhere.
   General knowledge is fine ("when was the state founded?").
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
- Being spoken to by name: a line that contains her name (or how Whisper and
  Meet write it: Koli, Coley, Collie, Kohli, קולי) and asks something. See
  "Talking with her"; an answer is at most 400 characters.

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

**Two faces of the board** *(2026-11-19, after a five-person meeting: "like a
prompter, not a slideshow, and her text was cut in the middle")*:

- **Her notes**, while she has nothing to say: the topic, decisions, tasks,
  open questions. Nothing moves, no frames are asked for.
- **Her words**, while she says something (`say` in the public view): the
  whole tile is hers. A line at the top says whom she answers (`say_to`) or
  that she has something to add (`say_kind: 'chime'`), the frame of the tile
  pulses for 3 s so eyes go to her, and the words come in at reading pace
  (42 characters a second, faster when the model is ahead) and flow upwards
  once the tile is full. Nothing is cut: a long answer glides by again from
  the top (1.7 s a line, 3 s of rest at each end) for as long as it is up.
  The old board showed the first 9 lines and dropped the rest.

Who draws the frames, measured on the hidden page (2026-11-19): the page's
own timer gives 1.5 frames a second and `requestAnimationFrame` 1.1, which is
the slideshow; calls from the app, one after the other, give 10.8 a second at
16% of one core. As the app drives it (`MeetGuest.animate`: a wait of 0.1 s,
then the call) it is 7 to 9 a second, measured over 150 s. So the board keeps its
slow timer for the notes, and while it says it is moving the guest's thread
asks for frames, and stops when the board has come to rest. Showing the page
again would give 70 frames a second and cost the 49% of a core that hiding
it saved.

Chrome stops a hidden page for 7 to 8 seconds once, about a quarter of a
minute after it is hidden, with or without a call or any drawing (measured:
one stall in 150 s). A frame that gets no answer in 5 s stops the redrawing;
the next look at the call picks it up again while she is still saying
something.

In a real call (2026-11-19, her page hidden, Meet asking for a 320x240
picture): 9.8 video frames a second reached Meet while she spoke, at 22% of
one core for her whole browser, against 9% while her notes are up; 1.2 GB.
A short remark comes to rest after 3 to 4 s and the frames stop.

At every look the guest also notes the tallest picture of her that Meet
asks for (`MeetGuest.watched`). Meet asks for none when her tile is on
nobody's screen, and each answer's log line says which it was.

**Her hand** *(2026-11-19: "some animation that people will look on her")*.
The pulse of her tile only helps those who have her tile on screen. Meet's
own signal reaches everyone: a raised hand puts a hand on her tile, plays a
sound, tells the call, and brings her tile into view. Measured in a real
call: a guest has the button ("Raise hand (ctrl + alt + h)"); it toggles and
looks the same either way, and the page says which it is in a line for
screen readers ("Your hand is raised at position 1." / "Your hand is
lowered.").

- `MeetGuest.hand(True)` notes the wish; her own thread (`keep_hand`) looks
  for the button, presses Meet's shortcut Ctrl+Alt+H
  (`Hands.shortcut`), and lowers the hand again after 20 s (`HAND_UP_S`): it
  calls eyes to her tile, it is not a request to speak. In the call: up
  within 2 s of her remark, down at 21 s.
- Not a click. A pointer path across her hidden page took over a minute:
  Chrome hands mouse moves to a page with its frames, and a hidden page has
  about one a second. Keys are not tied to frames. Anything she does after
  the page is hidden must be keys, inserted text or a script, never the
  pointer.
- At the next look at the call the page's line is read; a press that did not
  take (or took twice) is made again. A call without the button is left
  alone for the rest of the call.
- When (`CompanionService._hand`, `meeting.companion.raise_hand`, on by
  default): with every remark of her own; with an answer only when Meet
  asks for no picture of her, since the one who asked is usually looking.
  Down at once when her words leave the tile.

She leaves when the meeting ends, when you leave the call, when you press Take
out, or when Koli quits (`MeetingService.shutdown`). Continue previous and
Start new instead change the meeting, not the call: she stays, and her notes
start over.

### Talking with her

*Added 2026-11-18, after the owner sat alone with her in a call, asked her
questions aloud and got nothing on her tile: the answer was tied to the chat
setting (off), and she waited for Meet to close each caption.*

In Active mode, while she is in the call:

- **Who she answers.** A line that speaks to her by name, from anyone. When
  Meet's people list has only you, the call is a conversation: everything you
  say is to her.
- **When.** Meet closes a caption about 8 seconds after the speaker stops, and
  only then is it a line of the transcript. The service looks at the captions
  still open every second (`MeetFeed.open_captions`): words that may be for
  her and have not changed for 1.5 s are put to the model. A finished caption
  is not looked at again; lines Koli's own Whisper transcribed are looked at
  when they arrive.
- **Whether.** The model decides, each time (`companion.reply`): it answers,
  or says `[wait]` (the speaker only paused in the middle of a sentence: the
  words stay and are judged again with what follows, or once the speaker has
  been quiet for 3 s, when waiting is no longer an answer), or `[skip]` (not
  for her: said to someone else, thinking aloud). The first hour of real use
  showed why: one question got three answers, one per breath, and lines said
  to someone else in the room got answers too. The model is told whether the
  call is just the two of them and whether the speaker has stopped.
- **Follow-ups.** For 45 s after she answered someone by name (or alone
  with her), that person's next words need no name. In the first meeting
  with five people this window renewed itself with every answer and every
  breath went to the model: 12 answers, 21 waits, 7 skips in half an hour,
  and an answer of 31 words was replaced 7 s later by the next one. So,
  since 2026-11-19: a follow-up is judged only once the speaker has stopped;
  a follow-up she answers does not renew the window; a `[skip]` closes it
  (the talk moved on, she waits for her name); and a follow-up does not
  replace an answer before it could be read (14 characters a second,
  between 6 and 25 s). Her name always gets a look at once.
- **Her name anywhere is enough for a look**, question or not ("… Koli, give
  me a list of models" got nothing on 2026-11-19, when a line also had to
  ask something). Whether it is for her is the model's call.
- **A thread of her own.** Listening and answering run on
  `meeting-companion-talk`, looking every 0.5 s; the notes keep the worker
  thread. In a live call an answer had waited 5.6 s behind a notes update.
- **Where.** On her tile: three dots at once, then her words as the model
  writes them (the text reaches the tile every 0.4 s and flows in between,
  see "Two faces of the board"), then the answer for a minute, up to 900
  characters; when she waits or stays out, the dots go and nothing else shows.
  An answer already on the tile stays while the next words are judged, and
  goes only when a new answer starts: in a live call it kept vanishing behind
  the dots every time someone spoke ("she erases the answer").
  With `meeting.companion.chat` the answer is written in the chat too.
- **What she says.** `companion.reply`: plain text, at most three short
  sentences, in the speaker's language, from the meeting and from general
  knowledge; nothing about the people beyond this meeting. The model gets the
  public part of the notes, the lines the call heard, and her talk so far
  (the last 12 lines, her own answers among them: they are in no transcript).
- **The web.** With `meeting.companion.web` (on by default) her answer may
  come from a web search: `llm.complete(..., web=True)` adds the Responses
  API's `web_search` tool where the provider can (the ChatGPT login, measured
  2026-11-19: the weather in 3.7 s, against "I can't access live weather"
  without it). The model decides when to search; other providers answer from
  what they know. Only her answers in the call search, not her notes.
- **How fast.** The wait is the pause (1.5 s), up to a second for the caption
  to arrive, and the model's time to its first words. Measured with the local
  `gemma4:e4b`: 0.1 to 2 s to the first words. `ai.companion.model` picks it.
- Two answers are at least 2 seconds apart.
- **Kept with the meeting.** Each answer goes into the meeting's file
  (`remarks`: when, to whom, the words she answered, her words;
  `MeetingSession.add_remark`) and the summary reads them at their moment
  as "Koli (the AI assistant in the call)". Before, her words were on the
  tile for a minute and nowhere else.

### What she adds on her own

*Added 2026-11-19: "she was not participating or barging in the middle".
Until then Active mode only answered: she spoke when spoken to.*

In Active mode, in a call with other people (`meeting.companion.chime`, on by
default), she may say something nobody asked for. `companion.chime` asks the
model, which is told to speak only when a question was left without an
answer and she knows it, a wrong fact matters for a decision, something said
earlier in the meeting is recalled wrongly or not at all, a decision is
about to contradict an earlier one, or the group is stuck and she knows one
concrete thing that moves them on (a fact, a pitfall, a simpler way: not an
opinion); in every other case `[skip]`. No comments, summaries, praise or
offers of help. At most two short sentences.

Every look carries the last three minutes again, so the lines are sent in
two parts, "earlier (judged already, for context)" and "said since you last
looked" (`first_new`), and she is told to judge the new part only. Without
the mark a question she let pass would be judged again at every look and
could come up two minutes late.

When the model is asked (`CompanionService._consider_chime`), all of:

- nobody has spoken for 2 s (no open caption changed): she waits for a gap.
  The gap is read off Meet's captions, so the new lines must hold captions
  of Meet's (or one must be open): lines Koli transcribed herself arrive
  seconds late and say nothing about who speaks now, and with those alone
  she stays out rather than cut in in the middle of a sentence;
- at least 40 new words were heard since her last look;
- her last look was at least 30 s ago, her last remark of her own at least
  90 s ago, and anything else she said at least 30 s ago;
- the call is not just you and her (there, everything you say is to her).

So an hour of lively talk is at most 120 looks and 40 remarks; each look is
one call to her model with the last three minutes. The limits of a ChatGPT
subscription for this are not known: the caps are the knob.

The model is asked on a thread of its own (`meeting-companion-chime`), so a
question put to her by name never waits behind it. An answer always wins the
tile: a remark whose answer came in after she began on someone's question is
dropped. The remark is shown like an answer (the prompter, the pulse) under
"has something to add" (`say_kind: 'chime'`), kept in the meeting's
`remarks` with `kind: 'chime'`, remembered in her talk so she doesn't repeat
it, and written in the chat too when her answers are. It is built from what
the call heard only, like everything on her tile. A model that fails is a
line in the log, not a card: nobody asked.

Measured on `codex/gpt-6.1-terra` (2026-11-19):

- 12 short talks, English and Hebrew, clean text, run twice: 11 as a person
  would want each time; the miss was a silence where a remark was wanted (a
  different talk each time), never a remark nobody needed. Two more for the
  stuck-on-a-problem case (a fix offered; a matter of taste left alone):
  both right. 1.3 to 5.7 s each.
- The owner's meeting of that morning (five people, 34 minutes, 2,931 words
  of mixed Hebrew and English captions), replayed through the same gate
  with empty notes: 14 looks in 28 minutes (the talk had 42 gaps of 2 s),
  no remark. The captions of that meeting are too garbled to be sure of
  anything, and it was mostly one person presenting: silence is the right
  answer there, and it also means she speaks up only as well as she hears.

### Only what the call heard

Koli's own Whisper hears your microphone whether or not it is muted in Meet:
an aside to someone in your room is in the transcript. So from the moment she
is brought into a call, and for the rest of that meeting:

- her notes start over and are made only from lines the call heard: other
  people's words, and yours only when your microphone was on in Meet. The
  extension reports the microphone button's state (`muted`, read from
  `data-is-muted`) on every change and every 5 s. `MeetFeed.heard_state`
  answers for a line when it is read: yes only if the microphone was on for
  all of it (a mute counts from 1.5 s before it was noticed) and a report
  dated after the line ended says so; not yet, while that report hasn't come
  (the line and what follows wait); no after 15 s without one, or when the
  state was unknown (no button, you not in the call, an older extension). An
  old "microphone on" is never carried forward. A turn from Meet's captions
  counts as heard. No captions are needed: it works the same when Koli
  transcribes the audio herself;
- notes the model was still writing when she was brought in are thrown away;
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
| `meeting.companion.chat` | false | Active: her answers are written in the chat too |
| `meeting.companion.chime` | true | Active, with others in the call: she adds something on her own in a gap |
| `meeting.companion.raise_hand` | true | she raises her hand in Meet for 20 s when she speaks up |
| `meeting.companion.web` | true | her answers in a call may come from a web search |
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
- She adds something on her own only where Meet's captions are on (they
  tell her when nobody is speaking), and only as well as the captions are
  written: a call in two languages at once gives her little to be sure of.
- When her tile is on nobody's screen her words are seen by no one until
  they look: she raises her hand then, and the log says so for each answer
  ("Meet asks for no picture of her"). The hand's words on Meet's page are
  read in English (`?hl=en`).
- Not in this version: her voice, answers that draw on your earlier meetings
  (the search waits on a local model, too slow for a live answer; the brain
  already takes a `past` text), "explain this term", a warning before the end
  about questions nobody answered, calls other than Google Meet for the tile.
