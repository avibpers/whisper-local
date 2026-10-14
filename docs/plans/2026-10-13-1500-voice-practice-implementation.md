# Practice: implementation plan

Spec: `docs/design/voice-practice.md`. Worktree `S:\Koli-voice-practice`,
branch `feat/voice-practice` from master.

1. `learned_vocabulary.py`: the store (load/save with a lock, items, notes,
   sessions, intake), per-language extras, corrections merge, delete.
2. Engine: `transcribe_words()` (word timestamps, confidence, with or without
   vocabulary), `vocabulary_extras` hook in decode; state_manager wires the
   hook and merges learned corrections for the dictation's language.
3. `wake_word.capture_word()`: `end_silence_s`, `stop` event.
4. `practice/align.py` and `practice/findings.py` with tests.
5. `practice/intake.py` (history terms) and `practice/texts.py` +
   `texts.yaml` (packs, templates, model prompts, parsing) with tests.
6. `practice/session.py` and `services/practice.py` (round flow, recording,
   both transcriptions, analysis, apply, quiet pause).
7. Config `ai.practice.*`, schema; bridge methods; package data.
8. Pages: `practice.js`, My words tabs, welcome offer, routes, mock API.
9. Docs, CHANGELOG; full tests; mock UI check; a live round with Luna on a
   recorded voice; then the real test.
