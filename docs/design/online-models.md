# Online models: transcription in the cloud, and what every model costs

*Design spec, 2026-10-28. Approved in conversation. Step 4 of the AI roadmap
(providers → smart correction → teaching sessions → cloud transcription).*

## Goal

Koli can send dictation and meetings to an online speech-to-text model
(OpenAI or Gemini, with the API keys already saved in Settings › AI) and shows
what each call costs as it happens, so models can be compared on quality and
price. Every meeting records which models transcribed and summarized it and
what that cost.

## What stays local

- The wake word, voice commands, quick commands, Practice and the vocabulary
  "say it" check always use the local Whisper engine: they run on every burst
  of speech, so online they would be slow and run a bill all day.
- The local engine stays loaded. When an online call fails (no network, a bad
  key, a quota), the same clip is transcribed locally and the reason is shown.
- Voice activity detection runs before any upload: silence is never sent.

## Choosing

| Setting | What it picks | Empty |
|---------|---------------|-------|
| `whisper.online_model` | dictation's model, `provider/model` | local Whisper |
| `meeting.model` | meetings' model, `provider/model` | local Whisper |

Dictation's online models sit in the Model table of Settings › Model &
engine, below the local Whisper models, one group per provider with the price
an hour: an online model replaces the local one, so it is picked in the same
place. Picking one sets `whisper.online_model` (the local model in use stays
loaded and is marked "Backup · commands"); picking a local model clears it.
Meetings have their own picker in Settings › Meetings. Live-stream models
(WebSocket / Live API) are listed with "live, coming next" and can't be chosen
yet; upload models can, once their provider's key is saved.

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

Live-stream models, batch/flex discounts, currencies other than USD, cost
limits.
