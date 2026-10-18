# Self-learning: Koli learns your voice by itself

*Design spec, 2026-10-16. Visual plan approved in conversation:
https://claude.ai/artifact/2EBah7P4hmoBfkuiU6tjjv. Phase 1 is built; later
phases are outlined here so the on-disk record does not change under them.*

## Goal

Koli gets better at your words, names, accent and Hebrew/English mixing from
the dictations you already make. **No human involvement is required**: you
never label, review or train anything. Local and free by default; a paid cloud
teacher (Gemini) is an optional vote, never needed.

## The rule everything follows

A model can't learn from its own output. Every clip needs a better "teacher"
text than the live pass, and only clips the teachers clearly agree on are
learned from. Uncertain clips are skipped, not guessed.

## Phases

| Phase | What | Status |
|---|---|---|
| 1 | Keep clips: opt-in switch, FLAC store, disk cap and eviction ladder, delete buttons, free signals, Training screen | **Built** |
| 2 | Night re-listen: idle/locked detection, re-transcribe with full large-v3 (+ Hebrew model, + optional Gemini), local AI judge, vote → trusted label or skip, dashboard counts | Next |
| 3 | Auto-learn: favored words, corrections, prompt terms and per-mic thresholds from trusted labels; every change gated on the holdout set and watched for a week, undone automatically if your fix rate rises | Planned |
| 4 | Smart correction: the local AI fixes low-confidence words that don't fit the sentence, with your past corrections as examples | Planned |
| 5 | Your voice model: LoRA on the training pool, overnight on the GPU, merged and converted to CTranslate2; gated like phase 3 | Planned |

## Phase 1 (built)

### Behaviour

| Situation | What Koli does |
|---|---|
| `learning.enabled` off (default) | Nothing is kept. The recorder is subscribed but ignores every dictation. |
| A dictation (hotkey, auto-dictation, continuous) | Its raw microphone audio (before noise reduction and Whisper-mode gain) is kept as FLAC, with one record of what happened. Written on a worker thread; the dictation never waits. |
| Voice command or rephrase | Never kept. |
| App rule `keep_audio: false` | Never kept in that app. |
| Shorter than 0.3 s | Not kept (a tap on the keys). |
| Whisper or a guard drops the clip | Kept with `outcome: "dropped: <why>"`. These are the only way later phases can measure real speech that was dropped. |
| You dictate the same thing again within 30 s in the same window | The new record gets `signals.repeats` (the earlier clip's id) and the similarity. |
| You dictate within 15 s after a dropped clip in the same window | The new record gets `signals.after_drop`. |
| Disk use goes over `learning.disk_cap_gb` (default 2, 0.5–20, decimal GB) | Audio is deleted by the eviction ladder; records stay. |

### Architecture

```
state_manager._transcription_pipeline_locked
  └─ _begin_clip(raw audio, kind, mode, mic)  ──► learning/recorder.py  ClipRecorder.begin
  └─ _emit_pipeline('transcribed' | 'empty' why= | 'delivered' | 'error')
                                     │ (event bus, 'pipeline' topic)
                                     ▼
                        ClipRecorder.on_event ── finish ──► worker thread
                                                             └─ learning/clips.py  ClipStore.add (FLAC + jsonl)
                                                             └─ learning/retention.py  enforce (over cap / every 6 h)
services/learning.py  LearningService ◄── ui/bridge.py learning_* ◄── ui/web/js/screens/training.js
```

- `learning/clips.py`: `ClipStore`. `learning\clips.jsonl` (append for new
  clips, atomic rewrite on delete) and `learning\clips\YYYY-MM\<id>.flac`.
  FLAC through PyAV, which faster-whisper already installs (no new
  dependency); WAV fallback. `read_audio` reads with PyAV directly, because
  faster-whisper's `decode_audio` passes an option PyAV 19 dropped.
- `learning/recorder.py`: `ClipRecorder`. The state manager hands over the
  audio once; everything else comes from the pipeline events it already
  publishes. `_pipeline_lock` serialises dictations, so one pending clip is
  enough. Each `'empty'` emit now carries `why`.
- `learning/retention.py`: `enforce(store, cap_gb)`.
- `services/learning.py`: state for the screen, delete by span or date range.
- `transcript_log.record_transcript(..., clip_id=)`: History can find a
  dictation's clip.
- Settings: `learning.enabled`, `learning.disk_cap_gb` (section `learning`,
  edited on the Training screen). Lowering the cap cleans up at once.
- `keep_audio` is a rule key (`services/rules.py`), with a "Keep clips"
  choice on the Apps screen.
- Not touched: `settings_io` export and `bundle_logs` copy fixed file lists,
  so `learning\` never leaves the PC through them; `--uninstall` removes the
  whole settings folder.

### The record (`clips.jsonl`, one line per dictation)

```json
{"id": "20261016-101500-a1b2c3", "ts": "2026-10-16T10:15:00",
 "kind": "hotkey|auto|continuous", "phrase": false, "mic": "Microphone (EMEET Piko+)",
 "app": "discord.exe", "title": "#general", "window": {"hwnd": 0, "pid": 0},
 "speaker": "you|unverified", "seconds": 4.9, "language": "he",
 "live": {"text": "<what Whisper heard>", "info": {"language", "language_probability",
          "language_raw_probability", "audio_seconds", "model",
          "segments": [{"text", "start", "end", "avg_logprob", "no_speech_prob", "compression_ratio"}]}},
 "final_text": "<what was typed>", "outcome": "typed|failed|error|dropped: <why>",
 "why": "", "delivery": "pasted|copied|streamed|fallback|suppressed|failed",
 "signals": {"repeats": "<id>", "similarity": 0.71, "after_drop": "<id>"},
 "split": "train|holdout", "trust": "none", "distilled_at": null,
 "audio": "clips/2026-10/<id>.flac", "bytes": 81234}
```

- `speaker: you` only for hotkey clips; only those may enter the phase 5
  training pool.
- `split`: fixed by a hash of the id, 30% holdout. Holdout clips are never
  learned from; every phase 3+ score is measured on them.
- `trust` (`none` → `agreement` → later phases) and `distilled_at` are
  filled by phases 2–3; audio may go once a clip is distilled, the record stays.

### Retention (the ladder)

| Order | What | Rule |
|---|---|---|
| 1st | Typed train clips (later: clips already learned from) | Oldest first when over the cap |
| 2nd | Dropped clips | Audio after 14 days; over the cap before holdout |
| last | Holdout clips | Only when nothing else is left over the cap |
| records | Any record without audio | Kept up to 10,000, oldest without audio go first |

Phase 2 adds "the models disagreed" (kept 30 days) and phase 3 "already
learned from" (audio 7 days after `distilled_at`) to `eviction_rank`.

### Recent clips and Test (a preview of phase 2)

The Training screen lists the newest clips (20 at a time; All, Typed,
Dropped, Said again) with what was typed, or heard and why it was dropped.

- **Play**: the clip as a data URL (`learning_audio`); one plays at a time.
- **Test**: `learning/night.py` hears the clip now the way the night pass
  will: official large-v3 (`large`), or the Hebrew large-v3 for Hebrew; an
  unknown language is detected by large-v3 first. Beam 8, no prompt, no
  vocabulary, word confidence on. The night text is compared with what was
  typed (`compare`, practice's normalisation); changed words are marked.
  Dropped clips get a verdict instead: real speech (night words average
  ≥ 0.5 confidence) or noise. Nothing is saved or learned; the result is kept
  in memory for the screen only.
- The night model loads on the first test, on the live engine's device and
  precision, through the engine thread (a dictation waits for a test in
  progress), and unloads after 3 quiet minutes. Missing on this computer:
  Test answers `needs` and the screen offers the existing model download.
- Checked on two Practice readings (CPU, int8, about 40 s with loading):
  English came back exactly; Hebrew had one wrong word ("שמחטפים" for
  "שמחליפים"). The night model is a better teacher, not a perfect one,
  which is why phase 2 votes and skips instead of trusting one model.

### Privacy

Off by default. Clips stay in `%APPDATA%\koli\learning`. No cloud call in
phase 1. Text already sits in plain text in `transcripts.jsonl`, so the clips
are not encrypted separately.

## Later phases (outline)

- **Phase 2.** `platform/*/idle.py` (Windows `GetLastInputInfo`; macOS stub).
  A scheduler runs while the PC is locked or idle ≥ 10 min, no dictation or
  meeting, not on battery; stops between clips on any input. The teacher
  model loads at job start and unloads at the end. Per clip: full large-v3
  with a wider beam, the Hebrew model on Hebrew, optionally Gemini; a local
  model (Ollama / LM Studio) only *chooses* between candidates. Trusted = all
  agree with good confidence and no free signal against it. The two ivrit.ai
  models share training data, so their agreement alone is not enough on
  Hebrew: a different-family vote is required there.
- **Phase 3.** Trusted labels vs. the live text through `practice/align.py`
  and `practice/findings.py`. A change needs ≥ 3 independent clips, must score
  better on holdout, then a trial week watching fixes-per-100-dictations
  (repeats, after-drops); worse → undone automatically. Thresholds move from
  module constants to config with a per-mic override.
- **Phase 4.** Low-confidence words + context + examples from trusted labels
  → the local AI, constrained to marked words.
- **Phase 5.** LoRA (PyTorch + peft) on the hotkey training pool, mixed with
  public he/en data against forgetting; merged, converted to CTranslate2,
  swapped in through the engine tasks; gated like phase 3.

## Testing

Unit tests (`tests/test_smoke.py`: `ClipStoreTests`, `RetentionTests`,
`ClipRecorderTests`, `LearningServiceTests`, `LearningPipelineHookTests`):
FLAC round trip, records surviving audio deletion and restarts, the fixed
holdout split, delete by span and range, the eviction ladder, dropped-clip
expiry, the switch, never-kept modes and apps, drop reasons, both free
signals, and the dictation kind from the pipeline.

Real-app test (the user, before commit): turn the switch on, dictate with
the hotkey and with auto-dictation, say one sentence twice, let one be
dropped, check the Training screen counts and the folder, lower the cap,
delete the last hour, and set an app rule to Never.
