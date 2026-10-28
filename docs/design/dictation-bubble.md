# The dictation bubble: see the words while you speak, fix one before it is written

*Design spec, 2026-10-28. Agreed in conversation; implementation plan to follow
in `docs/plans/`. Background: `docs/research/claude-code-voice-dictation.md`.
Replaces the earlier "live typing" draft, which pasted into the app while you
spoke; that idea died on the held hotkey (Koli can only type through the
keyboard, and Ctrl+V under a held Ctrl+Win is not a paste).*

## Goal

Today a dictation shows up in the app only after you stop, and then you hunt
for the mistakes. Koli's floating bubble already follows the text field you
are writing in and already shows the last few words of a live transcript.
This design makes that bubble the place where the dictation lives until it is
written:

- the words appear in the bubble beside the field as you say them, the
  settled ones in normal type and the ones still being recognised in a
  lighter italic, the way Claude Code shows them;
- when you see a wrong word, you select it in the bubble and say the right
  one, click at the end and keep talking;
- when you finish, the text is written into the app once, where your caret
  is, exactly as a dictation is written today.

Nothing is typed into the other app while you speak. The bubble is Koli's own
window, so it can show revising text, a caret and a selection, which Notepad
or Chrome never could.

Three engines can feed the bubble, chosen in Settings and per app rule. After
a week or two with each, the usage and cost numbers Koli keeps decide which
stay and which are removed.

## Non-goals

- Typing into the other app while you speak. Dropped for the reason above and
  because Koli does not own that text box.
- A Hebrew on-device streaming model. None exists that is worth shipping; the
  local engine stays English until one does.
- Editing after the text has been written. Once it is in the app it is the
  app's text; History's "Fix this everywhere" covers the rest.
- Meeting mode. It has its own transcript panel and is untouched.
- Auto-dictation (a recording that starts when a text field gets focus). It
  keeps pasting phrase by phrase as it does today and never opens a bubble
  session. *(My call: it starts on every click into a field, often with
  nothing said, and must not cost money or stream the microphone.)*

## The setting: which engine fills the bubble

`live_words.engine`, shown in Settings as **Words while you speak**, with
four choices. The old `streaming.streaming_enabled` and
`streaming.deliver_to_cursor` are removed (no migration; the project breaks
old formats freely). `streaming.streaming_model` stays, as the local engine's
model.

| Engine | What the bubble shows | Language | Audio leaves the PC | What is written at the end |
|--------|-----------------------|----------|---------------------|----------------------------|
| **Off** | A timer, as today | – | no | Whisper's text, as today |
| **Local words** | Words as you say them, from the sherpa-onnx streaming model. A preview only: no fixing in the bubble | English only | no | Whisper's full pass over the recording, as today |
| **Phrases** | Each phrase a second or two after you pause, from faster-whisper | any | no | The bubble's text; no second pass |
| **Cloud** | OpenAI: words as you say them from its streaming speech-to-text. Gemini: each phrase a second or two after you pause, transcribed by a Gemini model | any | **yes**, labelled as such | The bubble's text; faster-whisper does not run |

**Local words is preview-only.** Its words come from a 20 MB English model and
are noticeably worse than Whisper's; a session that wrote them would trade
Whisper's accuracy everywhere for one fixed word. So a click into the bubble
does nothing in this engine, the help text says "to fix words in the bubble
use Phrases or Cloud", and Whisper writes the text at the end exactly as
today. *(My call.)*

**Gemini has no streaming dictation endpoint** on the Gemini API key Koli
holds (its Live API is a conversation that answers every turn; Google's
streaming speech-to-text is a different product with a different key). So
the Gemini engine works like Phrases: at each pause the phrase's audio goes
to the chosen Gemini model with a transcription instruction, and the answer
is the phrase. Gemini bills audio per token at a fixed rate per second, so
the per-minute price table still fits. The comparison the user wants, words
and minutes and cost per provider over a week each, holds; what differs is
that OpenAI shows words as you speak and Gemini shows phrases. *(My call.)*

**Only my voice** (dropping quieter speakers) works per Whisper segment, so
it keeps working in Phrases and in Off, and is off in Local words and in
OpenAI's streaming, where there are no segments. The setting's help text
says so. *(My call; a level gate on the recorder before the audio reaches
the engine can be added later if it is missed.)*

An app rule can set `live_words: off | local | phrases | cloud` to override the
global engine in that app. A rule without the key inherits the global engine.

The settled-versus-in-flight distinction comes from the engine: sherpa-onnx
and OpenAI send interims and finals; the phrase engines (Phrases, Gemini)
have only finals, so their bubble shows settled text and a small "listening"
mark.

**Language.** The cloud providers get a language hint resolved in this
order: the app rule's `language`, then `whisper.language` when it is not
`auto`, then the first entry of `whisper.languages`, else no hint and the
provider detects it. Local words is English only: when the resolved language
is not English, or is `auto` with no English in the list, the recording runs
as **Phrases** with a one-time notice, and the setting's help text says so.
*(My call.)*

## The bubble during a dictation

Applies to hotkey dictations (held, locked or hands-free) with any engine
other than Off. Command mode, rephrase and the translate task keep today's
pill: there is nothing to fix by hand in a command.

| Moment | What the bubble does |
|--------|----------------------|
| Recording starts | Moves beside the focused text field when Koli knows one, even if its resting spot is an edge or the bottom of the screen. Today the field's rectangle is only known while "Follow the text field" is on, because `input_watch` probes on focus events for a registered listener; the recording start adds a one-shot probe of the focused element (same UI Automation code) so the bubble can find the field either way. With no known field it opens where it rests. Returns to its resting spot after the write. *(My call.)* |
| First words arrive | The pill opens into a reading pane: the bubble's full width, a few lines tall, scrolled to the newest words, `dir="auto"` so Hebrew reads right to left. Settled words in normal type, in-flight words in a lighter italic. A new `overlay.live_opacity` slider (default 90%) sets its translucency; the existing `rest_opacity` is untouched. |
| Nothing said yet | The existing "waiting for speech" look. |
| Recording ends without a click in the bubble | Today's flow, unchanged in feel: the final text is written at the caret, auto-send presses Enter if the rule says so, the pane closes. |

## Fixing a word: the bubble session

Clicking into the bubble turns the dictation into a **session**. The bubble
becomes a small text editor that only you and Koli type into, and the text
waits there until you finish it.

**What a click does.** The bubble takes keyboard focus (today it is a
no-focus window; for a session the host makes it activatable, and makes it
no-focus again at the end). Selecting text in the pane first commits the
words still in flight, so the selection does not move under you. From then
on, whatever Koli recognises goes in at the caret, replacing any selection:
select a wrong word, say the right one, click after the last word, carry on.
Typing on the keyboard in the pane works too; it is a text box.

**Listening during a session.** Selecting a word does not stop the ears; it
only redirects where the words go. The hotkey keeps its meaning: in
push-to-talk, letting go pauses listening and pressing again resumes it at
the caret; in locked or hands-free recordings, listening simply continues
and a tap pauses it. Silence does not end a session, because you are using
the mouse. The pane shows a small mic mark for listening and a paused mark
for not, and a mic button to toggle it. *(My call.)*

**Finishing.** Any of:

- **Let go of the hotkey while the app's text field has focus.** You clicked
  back into the app, pressed the hotkey, said the last words, and released:
  the text is written, and the app rule's auto-send then applies exactly as
  for a dictation without a session, countdown included. (A release while
  the bubble itself has focus only pauses, see above.) In locked recordings,
  the stopping tap does the same. *(My call on auto-send: the rule is the
  user's own choice for that app, and the countdown leaves room to cancel.)*
- **Enter in the bubble.** Writes the text and then presses Enter in the
  app, so it is sent. Shift+Enter puts a line break in the pane. *(My call
  that Enter also sends; the user asked for "Enter to send".)*
- **The Write button in the pane.** Writes without sending.
- **Esc, or Cancel.** Discards; the text stays in History under "cancelled".

**The other global keys** (the stop key, Ctrl alone by default; the send
key, Alt alone; the cancel key, Esc) follow the same focus rule as the
hotkey. While the bubble has focus they are ignored by the global listener,
so Ctrl+A, Ctrl+C and Alt in the pane are plain editing keys and the pane's
own Enter and Esc apply. While the app has focus they act as today: the stop
key writes, the send key writes and sends, Esc discards. *(My call.)*

Writing means: give focus back to the field's window (the way a voice command
returns focus to its app, `platform/*/window_focus.py`), then paste at its
caret with today's delivery path. The bubble's text is written as it stands:
Koli's local post-processing has already run on each settled piece
(corrections, snippets, vocabulary, inline formatting), and the AI polish is
**skipped after a session**, so a word you fixed by hand is never un-fixed.
Without a session, the AI polish runs on the final text as today. *(My
call.)*

**Keystrokes in the app during a session** are left alone; the write lands
at the field's caret wherever it is when you finish. *(My call.)*

**History and stats.** One entry per recording with the written text; a
session also keeps the engine's original text so the Training screen can
learn from the fix later. Learned clips and `keep_audio` rules behave as for
any dictation.

**Floating bar.** The pane shows which engine is on, and a small cloud mark
in cloud mode so you always know when audio is leaving the PC.

## Engine 1: Phrases

This exists today for auto-dictation only (`handle_vad_event`'s
`PHRASE_PAUSE` branch runs the pipeline with `phrase=True` and keeps the
microphone open). The engine reuses it to feed the bubble instead of the app.

- The pause length is `live_words.phrase_pause_seconds`, default 1 s.
  Auto-dictation's own `phrase_pause_seconds` is removed and reads this one,
  so there is one number. *(My call.)*
- Each phrase is transcribed with the previous phrases of the same recording
  appended to Whisper's initial prompt (capped, newest last) so names and
  spellings stay consistent.
- Post-processing runs per phrase. Joining is new work: today `phrase=True`
  only adds a trailing space. A phrase after a sentence end starts with a
  capital, a phrase mid-sentence does not, and the closing period is added
  only to the last phrase of the recording.
- The final Whisper pass over the whole recording does not run; the pane's
  text is the result.

## Engine 2: Local words

The existing sherpa-onnx streaming path (`streaming_manager.py` partials and
finals through `handle_streaming_result`). Partials become the italic
in-flight text, finals become settled text. `streaming_delivery.py`, which
typed finals into the app, is removed. Whisper's full pass writes the final
text as today; the pane's words are only a preview, and the pane does not
take a click in this engine.

## Engine 3: Cloud

No Whisper pass; the provider's text is the text. `live_words.cloud.provider`
picks **OpenAI** or **Gemini**; each has a model setting with a sensible
default. The builder confirms current model names, endpoints and event
formats from the providers' docs at build time (context7), not from memory.
Keys come from the AI providers already set up (`llm/credentials.py`, the
`openai` and `gemini` entries); if the chosen provider has no key, the
recording runs as **Phrases** with one notification.

**OpenAI, words as you speak.** Microphone audio is streamed as 16 kHz mono
16-bit PCM over the Realtime transcription WebSocket, buffered while the
socket connects and flushed on open. Interims and finals feed the same
`handle_streaming_result` as the local engine; finals get Koli's local
post-processing per piece. Connection errors: one retry after 250 ms if
nothing was transcribed yet; otherwise the pane keeps what it has and the
audio since the last final is transcribed by faster-whisper and appended, so
a dropped connection never loses words.

**Gemini, phrase by phrase.** The Phrases engine's flow with Gemini in
Whisper's seat: at each pause the phrase's audio (16 kHz WAV, a few seconds)
goes to the chosen Gemini model with a short instruction to transcribe it
verbatim in the resolved language, plus the previous phrases of the recording
as context for names and spellings. The answer is the phrase. A failed
request falls back to faster-whisper for that phrase, so nothing is lost.

## Usage and cost tracking

Every recording with an engine on appends to `live_words_usage.json` in the
config folder, by day and by engine (`local`, `phrases`,
`cloud/openai/<model>`, `cloud/gemini/<model>`):

- recordings, seconds of audio sent or processed, words written, sessions
  (recordings where a word was fixed in the bubble),
- estimated cost: seconds ÷ 60 × the price per audio minute in
  `live_words.prices`, a user-editable table in the config; the builder fills
  the defaults from the providers' price pages at build time and dates them
  in the comment. Local engines cost 0.

The Insights screen gets a **Words while you speak** card: per engine, the
days used, minutes of audio, words, fixes made in the bubble, and cost so
far, with the date you switched engines so a week per provider can be
compared. The comparison is done by hand: use one engine for a week, switch,
read the card.

## What exists and what is new

| Piece | Today | Change |
|-------|-------|--------|
| `ui/web/js/bar.js` | pill with the last 48 characters of the stream | reading pane with settled and in-flight spans; editable during a session; mic, Write and Cancel controls; Enter / Shift+Enter / Esc |
| `ui/host.py` + `ui/win32.py` | no-focus bubble, follows a field when `overlay.follow_focus` | moves beside the field for a recording regardless of the resting spot; toggles activatable for a session; `live_opacity` |
| `ui/bridge.py` | – | session calls: text changed, caret or selection changed, write, write-and-send, pause or resume listening, cancel |
| `state_manager.py` | phrase delivery for auto-dictation; `handle_streaming_result` to the bar | a `DictationSession` owning the pane's text, the insertion point, listening state and the finish rules; phrases for any dictation when the engine is `phrases` |
| `streaming_delivery.py` | types finals into the app | removed |
| new `cloud_stt/` package | – | the OpenAI streaming client (`connect(language, on_interim, on_final, on_error)`, PCM buffering, retry) and the Gemini phrase transcriber (`transcribe(audio, language, context)`) |
| `platform/*/input_watch.py` | probes the focused element on focus events for a registered listener | also a one-shot `probe_focused()` for the recording start |
| `settings/schema.py`, `config.defaults.yaml` | `streaming.streaming_enabled`, `streaming.deliver_to_cursor`, `auto_dictation.phrase_pause_seconds` | `live_words` section (engine, phrase pause, cloud provider and models, prices); `overlay.live_opacity`; the old keys removed |
| `app_rules.py` and its defaults header | – | `live_words` rule key |
| new `live_words_usage.py` | – | counters and the Insights card data |
| `ui/web/js/screens/insights.js` | – | the card |
| `hotkey_listener.py` / `hotkey_gestures.py` | release stops the recording | release pauses when a session is open and the bubble has focus |
| `doctor.py` | – | reports the engine, the local model and the cloud keys found |

## Left to the builder

- Exact Realtime and Live API message formats and model identifiers, checked against current docs (context7).
- How the bubble takes and gives back focus without flashing or leaving the app's window behind another; `window_focus.py` has the pieces.
- Mapping an engine's final into the pane when the user has moved the caret mid-phrase: insert at the caret, replace the selection, never touch text elsewhere.
- Where interim-to-final promotion happens when a cloud provider never sends an explicit endpoint (its completed event, else a 1 s quiet timer).
- Tests: the engine decision per setting and rule, the session's finish rules, the pane's insert-and-replace logic, the usage counters, and a fake WebSocket for each provider client.
