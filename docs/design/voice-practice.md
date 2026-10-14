# Practice: teach Koli your voice

*Design spec, 2026-10-13. Decisions made in conversation; implementation plan
in `docs/plans/2026-10-13-1500-voice-practice-implementation.md`. Builds on
`docs/design/ai-providers.md`.*

## Goal

Most people who dictate to Koli speak a language that isn't their first one,
or speak their own with an accent, and Whisper mishears the same words again
and again ("תמונת כריכה" written "תמונת בריכה" on one microphone). Practice is
a short guided session, for every user, in which Koli has you read texts
aloud, compares what it heard with what was written, word by word, and turns
what it finds into vocabulary you approve: favored words, corrections, and
terms for each profile's prompt. It also writes a short "how you speak" note
per language, which smart correction (the next step) will hand to the LLM.

## What was agreed

| Question | Decision |
|----------|----------|
| Who it is for | Every user. A welcome-flow offer and a Practice button on My words. A round takes about 3 minutes. |
| Without an AI model | Works: built-in texts per language and domain, and Koli's own word comparison. With a model (any provider in Settings › AI), texts are written for you and the analysis goes deeper. Development uses `codex/gpt-5.6-luna`; later checked on a local model. |
| Your words | Your answers, plus terms from your dictation history, your corrections and replacements, and earlier results. No folders or files. Only terms are sent to the model, never whole dictations, and the intake shows which. |
| Reading | Short sentences first (pronunciation, one word at a time), then one paragraph that reuses the same target words (what context does to them). Every reading shows Whisper's raw output. |
| Findings | You review them; all are checked by default; Apply saves the checked ones. |
| My words | Two tabs: Mine (typed by you) and Learned (from practice). Learned items can be deleted one by one, several at once, or all ("start fresh"). |
| Where learned items apply | By language, on every microphone. Each item remembers the microphone and session it came from. |

## Intake (first time; editable later)

1. Languages you dictate in (pre-filled from Languages I speak / Language).
2. Native language or accent, free text ("Hebrew; English with an Israeli accent").
3. What you do: software, business and email, medicine, law, studies,
   creative, other (free text).
4. Words Koli gets wrong (optional).

Added without asking: up to 40 terms from your history (names, jargon, mixed
case, words outside the common list), the targets of your corrections and
replacements, and words that failed in earlier rounds.

## A round

1. **Sentences.** 8 per round with one language; with two, 4 in each plus 2
   mixed ("אני צריך לעשות refactor ל-API"). Written in two batches: the second
   batch is written after the first is read, aimed at what failed.
2. **Reading a sentence.** Record (or Space), read, and it stops after 1.5 s
   of quiet (Stop ends it sooner). Koli shows the sentence as Whisper heard it,
   with each word marked right, wrong, missing or unsure. Again and Skip.
3. **The paragraph.** 60 to 90 words in your main language (the one with more
   mistakes), reusing the round's target words. The result notes where
   context fixed a word that failed alone, and where it changed one that
   was right.
4. **Results.** Accuracy per language, the context effects, the "how you
   speak" note, and the findings with checkboxes. Apply.

While practice is open, hotkeys are paused (quietly, restored after) and the
wake word sleeps: nothing is typed anywhere.

## Texts

**With a model** (`llm.complete('practice', …, json=True)`): the model gets the
intake, the terms, the round's failures so far, and the rules: sentences
people really dictate, no digits or abbreviations (Whisper writes "3" for
"three"), no niqqud, sounds that speakers with this accent tend to blur, and
the target words used naturally. It answers
`{"sentences": [{"language", "text", "targets": [...]}]}` or
`{"paragraph": {"language", "text", "targets"}}`.

**Without a model**: `practice/texts.yaml` ships sentences and a paragraph per
language (English, Hebrew) and domain (general, software, business); other
domains use general. Your own words go into short templates per language.
A model that fails or answers badly falls back to the same packs, so a round
never stops.

## Analysis

Each reading is transcribed twice, with word timestamps and Whisper's
confidence per word, the way dictation does it: your Language setting (or
auto-detect, so a Hebrew sentence with English terms isn't forced into
Hebrew letters), and the same model routing (the Hebrew second model only
when the clip is detected as Hebrew with enough confidence):

- **As you dictate**: your context prompt (`whisper.initial_prompt`, which
  holds the active profile's), favored words and learned items. Built
  explicitly, so whatever window was focused before practice doesn't leak in.
  This is what Koli would have typed.
- **Bare**: no prompt, no favored words. What Whisper hears from your voice
  alone.

With the whisper.cpp engine there are no word confidences: readings compare
text only and practice says so.

Both are lined up against the text (normalised: lowercase, punctuation
dropped, Hebrew kept as written) with a word-sequence diff:

| Result | Meaning |
|--------|---------|
| right | same word; *unsure* when Whisper's confidence was under 0.45 |
| wrong | another word in its place (`replace`) |
| missing | dropped (`delete`) |
| extra | added words (shown, not counted) |

**Rule findings** (always):

- A wrong word becomes a **correction**: write the text's word when Whisper
  writes what it heard. A single heard word may be a real word, so it takes
  its neighbour along ("תמונת בריכה" → "תמונת כריכה") and a real "בריכה"
  elsewhere is never replaced; several heard words for one ("cooper net is"
  for "Kubernetes") are specific enough alone. When the text's word is itself short (2 letters) or common
  ("את" heard "אתה"), there is no correction at all: it would fire on every
  real "אתה".
- A wrong, missing or unsure word that isn't common becomes a **favored
  word** for that language.
- No correction when what was heard is ordinary words ("כמו איזה"), or the
  stretch is longer than two words of the text (too specific to match
  again). Corrections of the same text are joined, with every way it was
  heard. An English term's Hebrew prefix is dropped for favored words
  ("ה-clipboard" → "clipboard") but kept in corrections.
- **Prompt terms** come from the model only: it knows which profile a term
  belongs to, and favored words already reach every profile.

*Learned from the first real round (2026-10-14):* two real words that sound
alike to the speaker (מאלף / מעלף, with א and ע pronounced the same) must
never get a one-word correction: it would break the other word every time.
Only the phrase corrections are safe; choosing by context is smart
correction's job, and the note describes the pair. Texts can still be
wrong (an ordinary English word inside Hebrew, a typo), so each reading has
**The text has a mistake**, which makes it count for nothing, and the model
lists real typos as `text_errors`. Whisper invents words in trailing silence
("תודה רבה" six times), so a reading is trimmed to 0.35 s after its last
sound before it is transcribed.

**With a model**, it receives the intake and the comparisons (text, as
dictated, bare, each word's result) and answers
`{"findings": [{"kind": "favored|correction|prompt", "language", "term",
"heard": [...], "profile", "reason"}], "notes": {"<language>": "..."}}`.
Its findings are merged with the rules' (same item once, the model's reason
kept); the note is saved per language. Already-known items are left out.

## Storage

`%APPDATA%\koli\learned.json`:

```json
{"version": 1,
 "intake": {"languages": ["en", "he"], "accent": "...", "work": "software", "work_other": "", "words": []},
 "items": [{"id", "kind": "favored|correction|prompt", "language", "term",
            "heard": [], "profile": "", "mic", "session", "created", "reason", "source": "rules|ai"}],
 "notes": {"he": "..."},
 "sessions": [{"id", "date", "mic", "languages", "accuracy": {"he": 0.82}}]}
```

It sits outside `user_settings.yaml` on purpose: Reset all settings keeps it
(as it keeps your favored words), and settings export/import carries it
(`settings_io.EXPORTABLE_FILES`). Models answer JSON loosely (Codex has no JSON
mode; a ```json fence is common), so every answer is parsed from its first
`{…}` block, and a bad one falls back to the packs and the rule findings.

Recordings and per-reading results are kept in
`%APPDATA%\koli\practice\<session>\` (`NN.wav`, `session.json`), the
material a real fine-tune of Whisper on your voice would need later. Delete
all learned removes them too.

## How learned items reach dictation

- **Favored words**: the engine adds the learned words for the language it
  is about to transcribe (all languages when it doesn't know yet) to your own.
- **Prompt terms**: added to the main model's prompt ("Terms: …") for the
  active profile (or every profile) and the language. The Hebrew second model
  still gets no prompt, only favored words.
- **Corrections**: before post-processing, the learned corrections for the
  dictation's language are merged into yours.
- **Notes**: stored for smart correction; shown on Learned.

## Settings

`ai.practice.model` and `ai.practice.timeout` (90 s) join the AI section, so
practice can use its own model (Luna while developing).

## Code

- `practice/` package: `intake.py` (answers, personal terms), `texts.py`
  (packs, model prompts, parsing), `texts.yaml`, `align.py` (comparison),
  `findings.py` (rules, model merge), `session.py` (a round: items,
  recordings, results).
- `learned_vocabulary.py`: the store and what the engine and post-processing
  read from it.
- `services/practice.py`: the one object the bridge talks to.
- `whisper_engine.transcribe_words()` and a `vocabulary_extras` hook;
  `wake_word.capture_word()` gains an end-of-speech pause and a stop signal.
- Pages: `screens/practice.js` (intake, reading, results); My words gets the
  Mine / Learned tabs; the welcome flow's last step offers Practice.

## Testing

Unit tests: normalisation and the diff (Hebrew, mixed, punctuation, dropped
and extra words), rule findings (neighbour on short words, common words
skipped), merging model findings, parsing a model's text answer (and falling
back to packs), the store (add, delete selected, delete all, per-language
extras, corrections merge), and a full round driven by fake audio and a fake
engine. The real check is a round on your microphone with Luna.
