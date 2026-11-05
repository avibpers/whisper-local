# Smart correction: a small local model fixes misheard words

*2026-11-05. A mode of the existing Polish step (`ai.polish.mode: correct`),
in `smart_correction.py`. Dictation only: meetings never come here.*

## Why after Whisper

Whisper can't follow instructions, and more context in its prompt did not
reduce mishearings (`whisper-context` tests, 2026-11-05). A language model can
read the sentence and choose `role` over `roll`. The price is time, and the
owner's rule is plain: if it means waiting a few seconds, it isn't wanted.

## What it can do

| Setting | What happens to a dictation |
|---|---|
| `ai.polish.mode: correct` | Misheard words are fixed |
| `ai.polish.mode: grammar` | Your English grammar only: `is` to `are`, tenses, articles, plural, never a different word. Hebrew gets `correct` |
| `ai.translate: he_en`, usually from a profile (`docs/design/profiles.md`) | Hebrew is written in English: hold the Translate profile's key, set the profile on an app, or make it the default |

English stays English for now. Whisper's own translation isn't used: on 40 of the
owner's clips `large-v3-turbo` and `hebrew-large-v3` stayed in Hebrew, and only the
official `large-v3` translated, badly ("mass calculators" for tax invoices).
`hebrew-large-v3` with `gemma4:e4b` was clearly better, and also better than the
Opus-MT translation models, which mangled the owner's terms (merge to "Marge").

## What is kept for learning

The AI must not teach Koli its own words. Every clip keeps `live.text` (what Whisper
heard, never touched), `final_text` (what was typed) and `ai`: `kind`, `before`,
`after`, `changes` (`[{"from": "is", "to": "are"}]`), `score`, and `failed` for a
translation that didn't happen. The journal keeps `heard` and `ai` too, and `language`
stays the language you spoke, not the one written. Learning reads `live.text`.

## Does the sentence make sense

Next to each correction a second call scores from 1 to 5 how likely it is that a
person said these words. For now the score is only shown on the consoles and kept
in `ai.score`; holding a sentence at 2 or below for you to check in the bubble is
the next step.

On 240 good and 108 broken sentences (the weak `base` model's hearing of the same
clips): a plain yes or no caught 2 of 108. The 1-to-5 score at 2 or below caught 54
of 108 (51 of 91 Hebrew, 3 of 17 English) and flagged 4 of 240 good ones, two of
which really were broken. Asked in the same call as a rewrite it stopped working
(broken text scored 5), hence the second call; together they take no longer than
the slower one.

## What the model gets

The language, the window (app and title), up to 4 earlier dictations from that
window, and your favored words with the targets of your corrections. The fixed
instruction comes first so Ollama can reuse it between dictations. The window
and the earlier dictations go only to a model on this computer; a cloud model
gets the text, the language and your words.

## How it never makes you wait

| Situation | What happens | Extra wait |
|---|---|---|
| Model loaded | It answers | 0.2 s typical, 1 s at most (`ai.polish.timeout`) |
| No answer within the limit | Your text goes out as it was; a warm-up is started | The limit, once |
| Model not loaded (10–15 s to load) | Not asked; your text goes out as it was | None |
| Over 400 characters (2% of dictations) | Not sent, because it wouldn't fit in a second | None |
| A translation, any length | Gets up to 4 s (872 characters took 2.35 s). If it fails your words are typed and **not sent**, so Hebrew never goes out by accident | Usually 0.35 s |
| Ollama not running | Not asked after the first failure | None |

A warm-up runs at app start and at every recording start: 5 ms when the model is
loaded, and otherwise it loads while you speak. Ollama keeps it for 2 hours after
the last use. Another app using Ollama can push it out (`OLLAMA_MAX_LOADED_MODELS=1`
on this PC); the next recording loads it again. A local model never falls back
to the cloud models in `ai.fallbacks`.

The warm-up and the calls must send the same `num_ctx`, or Ollama loads the model
a second time. That cost 17 s in the first test.

## Seeing it work

Each dictation shows what the model got besides its fixed instruction, and what
became of its answer, on the terminal and in the Live screen's console. Not in
`app.log`, which stays free of your text unless `logging.log_transcriptions` is on.

## What is thrown away

Grammar has a check of its own, because the instruction alone didn't hold it: on the
owner's real dictations `porno` became `promo` and `no messages` became `any messages`.
`keep_grammar_only` goes through the answer change by change and keeps only what is
grammar: small words, another form of the same word, words joined or apart, or one of
your terms that sounds alike. Anything else goes back to what you said, and the clip
says so (`ai.kept_out`). The cost: real fixes like `roll` to `role` go too.

The model's answer is written only if it changes words, keeps the alphabet of the
dictation, adds no third alphabet, and changes at most 30% of the words (or 2); 35%
with grammar, which once made up the end of a sentence. The same words with other
punctuation count as no change. A translation must be English, about the right
length, and not an answer about "the dictation".

## The numbers (RTX 5090, 240 held-out dictations)

| Model | Median | 99% within | Closer / further from the owner's choice by ear | Memory (Ollama's report) |
|---|---|---|---|---|
| **`gemma4:e4b`** | 0.20 s | 0.74 s | 11 / 3 | 3.4 GB |
| `gemma4:e2b` | 0.18 s | 0.60 s | 8 / 5 | 2.7 GB |
| `gemma4:26b` | 0.26 s | 0.86 s | 15 / 16 | 18 GB |
| `qwen3:8b` | 0.15 s | 0.69 s | 1 / 3 (writes foreign letters) | 5.2 GB on disk |

The same `gemma4:e4b` through this code with the guard and the 1-second limit:
median 0.21 s, 99% within 0.67 s, worst 0.99 s; changed 17 of 240; closer 9,
further 4, same 31. With grammar and the score together: median 0.30 s, 99% within
0.77 s, changed 63 of 120 English dictations. Translation with the score: median
0.35 s, 99% within 1.1 s. With the careful check, on the dictations since grammar went
on: 14 of 48 changed against 22 before, median 0.35 s, worst 0.996 s. Graphics memory rose by about 4.8 GB by `nvidia-smi`. No
measurable GPU load while idle. Reading the whole journal took 149–248 ms, so
this reads only its end (1 ms).

These listening numbers are small and come from clips chosen for an earlier
test. Real use decides: `AI polish: ... answered in` in `app.log` has every wait.

## Not done

- It can't tell a real mishearing from a word that merely looks wrong: in the
  test it turned `David` into `Dave` and rejected nothing for it. Your text can
  still come out wrong; `live.text` in the clip record keeps what Whisper heard.
- Only tested with Ollama. Other local providers get no warm-up or speed options.
