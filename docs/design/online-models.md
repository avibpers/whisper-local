# Online models: transcription in the cloud, and what every model costs

*Design spec, 2026-10-28. Approved in conversation. Step 4 of the AI roadmap
(providers → smart correction → teaching sessions → cloud transcription).*

## Goal

Koli can send dictation and meetings to an online speech-to-text model
(OpenAI or Gemini, with the API keys already saved in Settings › AI) and shows
what each call costs as it happens, so models can be compared on quality and
price. Every meeting records which models transcribed and summarized it and
what that cost.

## Online mode

Picking an online row in the Model table (Settings › Model & engine) sets
`whisper.online_model`, and dictation and voice commands go there. There is
no separate switch: the row picked is what dictation uses (a switch of its
own disagreed with the table, 2026-10-29). This computer then loads only `whisper.listen_model`
(`base` by default): the main model and the second-language model are not
loaded, so a PC without a GPU can run Koli all day. The listening model:

- hears the wake word (its quick check on each burst of speech, and the quick
  command at a pause, never go online: they run all day),
- transcribes when the online model fails (no network, a bad key, a quota),
- serves Practice, Training and the bubble's Phrases engine.

Voice activity detection runs before any upload: silence is never sent.
Picking a local row empties `whisper.online_model` and loads that model, in
one settings change so the engine is rebuilt once.

Your languages (`whisper.languages`) go to Gemini's upload models as a
neutral hint ("Hebrew or English, sometimes both; write each word in its own
alphabet"). Tested on real clips (2026-10-29): naming a main language made
gemini-3.5-flash-lite write English in Hebrew letters, and a Hebrew lead
sentence in OpenAI's prompt came back as the transcript of a short clip, so
OpenAI gets no hint. On those clips `gpt-transcribe` was right every time
(Hebrew, English and mixed); `gemini-3.5-flash-lite` was weak on Hebrew.
`gemini-3.5-transcribe` refuses JSON mode and answers in
`parts[].audioTranscription`, and ignores the instruction's language hint.

## Choosing

| Setting | What it picks | Empty / off |
|---------|---------------|-------------|
| `whisper.online_model` | dictation and commands online, picked in the Model table | local Whisper (the Model table's model) |
| `whisper.listen_model` | the only local model while an online model is picked | `base` |
| `meeting.model` | meetings' own model, online or local | follow dictation: its online model, else local |

The Model table lists every model of the price list that takes audio, one
group per provider with its price an hour. Live models (`transcribe: live`:
OpenAI `gpt-live-transcribe` and `gpt-realtime-whisper`, Gemini
`gemini-3.5-transcribe-live`) can be picked: they become the Cloud engine of
Words while you speak, so your words show in the bubble as you speak, and the
provider's upload model (`gpt-transcribe`, `gemini-3.5-transcribe`) takes
what can't stream (voice commands, auto-dictation). Leaving a live row turns
Words while you speak off again. Voice chat models (`transcribe: chat`:
gpt-realtime*, gpt-audio*, the Gemini Live chat models) are listed with
their prices but can't be picked. Meetings send chunks, so they use upload
models only.

Gemini Live (`cloud_stt/gemini_live.py`) is Gemini's BidiGenerateContent
WebSocket with its own voice detection off: Koli opens and closes each turn
(activityStart / activityEnd) at its phrase pauses. Checked on the real API
(2026-10-29): a turn opened before the previous turn's final text came back
is ignored, so the next turn opens only after that text, and its audio waits.
Gemini Live reports no tokens; the cost is estimated at 25 audio tokens a
second and about 1.4 output tokens a word.

## Monthly budget

`usage.monthly_budget` ($, 0 = no limit) caps what paid models may cost in a
calendar month (`usage/budget.py`, summed from the ledger). At 80% Koli says
so once; when it is used up, until the month ends:

- with `usage.over_budget_model` set, that local model is loaded and
  dictation, commands and meetings use it;
- with it empty, nothing is transcribed and the bubble says "Online budget
  used up this month";
- AI features on a paid key answer "budget used up" and fall through to a
  subscription or local fallback if the route has one; the bubble's Cloud
  engine shows Phrases.

A new month lifts it by itself; the local model is swapped back when it does.

## The engine

`online_stt/` mirrors `WhisperEngine.transcribe_audio` and `last_info` like
`whisper_engine_cpp.py` does, so callers don't change:

- `online_stt/engine.py` — `OnlineEngine(spec, local)`: VAD, then the
  provider call, then `last_info` with `backend: 'online'`, the model, the
  cost. Falls back to `local` on any error.
- `online_stt/openai.py` — `POST /v1/audio/transcriptions`, multipart WAV.
  `whisper-1` asks for `verbose_json`, so its segments carry the same
  certainty numbers the local guards read; the gpt-4o family answers `json`
  with token `usage`.
- `online_stt/gemini.py` — `generateContent` with the WAV inline and a
  "transcribe verbatim" instruction; `usageMetadata.promptTokensDetails`
  separates audio tokens from text tokens.

Other models give no certainty numbers, so `segments` is empty and the guards
pass the text through (as for whisper.cpp).

## Prices

`usage/prices.yaml` ships the list (both providers' pricing pages,
2026-10-28): per model its kind (`transcribe` / `live` / `chat`), per-minute
price and/or per-1M-token prices (`audio_in`, `text_in`, `text_out`), and
dated `changes` (Gemini 4 Argon doubles on 2026-12-01). A user's own
`prices.yaml` in the settings folder is laid over it, edited from the Usage
screen.

Cost of one call:

1. Tokens known (from the answer) and the model has token prices → tokens ×
   prices; Gemini thinking tokens count as output.
2. Else per-minute price × audio seconds.
3. Local models, Codex and SuperGrok subscriptions: $0. A cloud model missing
   from the list: cost unknown, shown as "no price".

## The usage ledger

`usage/ledger.py` gets one record per online or AI call: feature
(`dictation`, `meeting`, `summary`, `chat`, `polish`, …), model, audio
seconds, tokens, cost, and the meeting id when there is one. It keeps:

- totals for this session (shown live),
- totals per day and model, and per meeting and model, in `usage.json` in the
  data folder (written a few seconds after a change, small).

Local meeting transcription is recorded too ($0), so a meeting always says
which model transcribed it. Each record is published on the `usage` topic.

LLM calls are recorded in `llm.complete()`: `Result` gains `cost`. Meeting
summaries and chat run inside `usage.for_meeting(id)`, so their cost lands on
the meeting.

## Where it shows

- Live screen: each dictation's model and cost; the session total.
- Floating bar: the session's online cost beside the bubble while it is above $0.
- Usage screen (Insights › Usage & cost): totals by model and by day, the
  price list with $/hour, and editing a price.
- Meeting panel: the meeting's running cost.
- Meetings screen: each meeting's models and cost; the meeting's Info shows
  the breakdown (transcription, summary, chat).

## Not now

Live models for meetings, batch/flex discounts, currencies other than USD, cost
limits.
