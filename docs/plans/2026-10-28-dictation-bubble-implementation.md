# Dictation bubble Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While you dictate, the words appear in the bubble beside the text field; with the Phrases or Cloud engine you can click into the bubble, fix a word by voice or keyboard, and the text is written into the app once at the end.

**Architecture:** A pure `DictationSession` model (`dictation_session.py`) holds the bubble's text, the in-flight words, the caret and the session flags. One of four engines feeds it per recording (`live_words.py` decides which): local sherpa-onnx partials, Whisper per VAD phrase pause, OpenAI's realtime transcription over a stdlib WebSocket, or Gemini per phrase. At the end the pipeline delivers the session's text instead of running Whisper (Phrases, Cloud) or runs Whisper as today (Off, Local). The bubble page renders the session snapshot from `live_words` events and calls the bridge to edit, write, pause and cancel.

**Tech Stack:** Python 3.13 stdlib (`ssl`, `socket`, `base64`, `json`), numpy, soxr (resampling), faster-whisper, sherpa-onnx, pywebview + Preact/htm pages, unittest.

**Spec:** `docs/design/dictation-bubble.md`

## Global Constraints

- Setting `live_words.engine`: `off | local | phrases | cloud`, label **Words while you speak**, default `off`.
- App rule key `live_words: off | local | phrases | cloud`; a rule without it inherits.
- `live_words.phrase_pause_seconds` default `1`; auto-dictation reads it; `auto_dictation.phrase_pause_seconds` is removed.
- `live_words.cloud.provider`: `openai | gemini`, default `openai`; `live_words.cloud.openai_model` default `gpt-live-transcribe`; `live_words.cloud.gemini_model` default `gemini-3.6-flash`.
- `overlay.live_opacity` default `90` (percent, 20–100).
- Removed keys, no migration: `streaming.streaming_enabled`, `streaming.deliver_to_cursor`, `auto_dictation.phrase_pause_seconds`; `streaming_delivery.py` is deleted.
- Keys come from `llm.credentials.load('openai'|'gemini')['key']`; never logged, never in a URL.
- OpenAI: `wss://api.openai.com/v1/realtime?intent=transcription`, header `Authorization: Bearer <key>`; `session.update` with `session.type = "transcription"`, `audio.input.format = {"type": "audio/pcm", "rate": 24000}`, `audio.input.transcription = {"model", "languages": [lang], "delay": "low"}`, `audio.input.turn_detection = null`; audio via `input_audio_buffer.append` (base64 PCM16 24 kHz mono), turns closed by `input_audio_buffer.commit` at each phrase pause and at stop; server events `conversation.item.input_audio_transcription.delta` (`item_id`, `delta`) and `.completed` (`item_id`, `transcript`), `error`. Checked against developers.openai.com on 2026-10-28.
- Gemini: `POST {BASE_URL}/models/{model}:generateContent` with `inline_data` `audio/wav` base64, header `x-goog-api-key`; usage from `usageMetadata`.
- Default prices (dated 2026-10-28 in the YAML comment): `openai/gpt-live-transcribe` 0.017 per audio minute, `openai/gpt-transcribe` 0.0045 per minute; Gemini per 1M tokens: `gemini-3.6-flash` in 0.75 out 3.75, `gemini-3.5-flash-lite` in 0.30 out 2.50, `gemini-2.5-flash` in 1.00 out 2.50.
- Auto-dictation, command mode, rephrase and `task: translate` never use an engine or a session.
- Local words is English only and never opens a session; non-English resolved language runs Phrases with a one-time notice.
- Every new module starts with a 2–4 line header comment; non-trivial functions get a one-line intent comment (project CLAUDE.md).
- No `alert()`/`confirm()`/`prompt()` in pages.
- Tests: `PYTHONUTF8=1 S:/Koli/.venv/Scripts/python.exe -m unittest discover -s tests` from the worktree root; new tests in `tests/test_dictation_bubble.py`.

## Review Focus

1. **A recording that ends before any word arrived** (empty pane, cloud slow): nothing is written, the bubble flashes "No speech detected" as today. Test in Task 7.
2. **The cloud is unreachable or the key is missing at start, or drops mid-recording**: the recording continues as Phrases (start) or the unsent audio goes to Whisper (mid); no word is lost. Tests in Tasks 5 and 8.
3. **You click into another window before finishing a session**: the write goes back to the window the dictation started in; if that window is gone, the text goes to the fallback window. Test in Task 7.
4. **Emoji or other astral characters before the caret** (JS counts UTF-16 units, Python code points): the pane sends code-point offsets, so an edit after an emoji lands in the right place. Test in Task 2 (Python side) and the pane helper in Task 10.
5. **A phrase finishes transcribing after the session was cancelled or written**: it is dropped, never pasted into the next recording's pane. Test in Task 7.

---

### Task 1: Settings, app rule key and the engine decision

**Files:**
- Create: `src/koli/live_words.py`
- Modify: `src/koli/config.defaults.yaml` (streaming section ~lines 160–185, auto_dictation ~258–270, overlay ~569–590; new `live_words:` section after `streaming:`)
- Modify: `src/koli/settings/schema.py:117-119,217-226` (+ overlay group ~366)
- Modify: `src/koli/settings/appliers.py:29,267-298,519-520`
- Modify: `src/koli/main.py:213-217` (`setup_streaming` reads `live_words.engine == 'local'`)
- Modify: `src/koli/app_rules.defaults.yaml` header, `src/koli/app_rules.py` header comment
- Modify: `src/koli/state_manager.py:1049-1054` (`_auto_phrase_pause` reads `live_words.phrase_pause_seconds`)
- Test: `tests/test_dictation_bubble.py`

**Interfaces:**
- Produces: `resolve_language(whisper_cfg: dict, rule: dict | None) -> str` ('' = no hint); `choose_engine(cfg: dict, rule: dict | None, *, mode: str, language: str, has_key: Callable[[str], bool], local_ready: bool) -> EngineChoice`; `@dataclass EngineChoice(engine: str, provider: str = '', model: str = '', notice: str = '')` where `engine` is `'off' | 'local' | 'phrases' | 'openai' | 'gemini'`; `usage_key(choice: EngineChoice) -> str` (`'local'`, `'phrases'`, `'cloud/openai/<model>'`, `'cloud/gemini/<model>'`).

- [ ] **Step 1: Write the failing tests** (`LiveWordsDecisionTests`)

```python
def test_rule_overrides_global_engine(self):
    c = choose_engine({'engine': 'off'}, {'live_words': 'phrases'}, mode='dictate', language='he', has_key=lambda p: True, local_ready=True)
    self.assertEqual(c.engine, 'phrases')
def test_command_rephrase_translate_and_auto_get_off(self):
    for mode in ('command', 'rephrase', 'translate', 'auto'):
        self.assertEqual(choose_engine({'engine': 'cloud'}, None, mode=mode, language='he', has_key=lambda p: True, local_ready=True).engine, 'off')
def test_local_non_english_runs_phrases_with_notice(self):
    c = choose_engine({'engine': 'local'}, None, mode='dictate', language='he', has_key=lambda p: True, local_ready=True)
    self.assertEqual(c.engine, 'phrases'); self.assertIn('English', c.notice)
def test_local_not_loaded_runs_phrases(self):
    self.assertEqual(choose_engine({'engine': 'local'}, None, mode='dictate', language='en', has_key=lambda p: True, local_ready=False).engine, 'phrases')
def test_cloud_without_key_runs_phrases_with_notice(self):
    c = choose_engine({'engine': 'cloud', 'cloud': {'provider': 'gemini'}}, None, mode='dictate', language='he', has_key=lambda p: False, local_ready=False)
    self.assertEqual(c.engine, 'phrases'); self.assertIn('Gemini', c.notice)
def test_cloud_openai_default_model(self):
    c = choose_engine({'engine': 'cloud', 'cloud': {}}, None, mode='dictate', language='he', has_key=lambda p: True, local_ready=False)
    self.assertEqual((c.engine, c.model), ('openai', 'gpt-live-transcribe'))
def test_language_order(self):
    self.assertEqual(resolve_language({'language': 'auto', 'languages': ['he', 'en']}, {'language': 'en'}), 'en')
    self.assertEqual(resolve_language({'language': 'he'}, None), 'he')
    self.assertEqual(resolve_language({'language': 'auto', 'languages': ['he', 'en']}, None), 'he')
    self.assertEqual(resolve_language({'language': 'auto'}, None), '')
def test_usage_keys(self):
    self.assertEqual(usage_key(EngineChoice('openai', 'openai', 'gpt-live-transcribe')), 'cloud/openai/gpt-live-transcribe')
def test_removed_keys_are_gone_and_new_ones_have_schema(self):
    # config.defaults.yaml has live_words.engine, no streaming.deliver_to_cursor / streaming_enabled / auto_dictation.phrase_pause_seconds;
    # settings.schema has entries for every live_words.* key and overlay.live_opacity
```

- [ ] **Step 2: Run to see them fail** — `PYTHONUTF8=1 S:/Koli/.venv/Scripts/python.exe -m unittest tests.test_dictation_bubble -v` → ImportError on `koli.live_words`.
- [ ] **Step 3: Implement** `live_words.py`; YAML section with comments (engine, phrase_pause_seconds, cloud.provider, cloud.openai_model, cloud.gemini_model, prices map with the dated comment); schema: a new `group='Words while you speak'` in section `general` (engine `segmented` with the four choices, mode SIMPLE, help naming that Local is English only and preview-only and that Cloud sends audio; phrase pause `number` 0.5–5; provider `segmented`; two model `text` fields; prices `readonly`), `overlay.live_opacity` `range` 20–100 step 5. Remove the three old schema entries and their applier entries. The streaming applier becomes `live_words_engine` on `live_words.engine`: loads/unloads the sherpa model when the engine becomes/stops being `local` (same job body, `streaming_enabled = value == 'local'`). App-rule header documents `live_words`.
- [ ] **Step 4: Run** the new tests and the whole suite → all pass (fix any existing test that asserted the old keys; `DefaultsTests` checks schema coverage).
- [ ] **Step 5: Commit** `feat(live-words): engine setting, app-rule key and the per-recording decision`

### Task 2: The session model

**Files:**
- Create: `src/koli/dictation_session.py`
- Test: `tests/test_dictation_bubble.py`

**Interfaces:**
- Produces: `class DictationSession(engine: str, session_id: int, target_window: dict | None)` with attributes `opened: bool`, `edited: bool`, `listening: bool`, `engine`, `session_id`, `target_window`; methods `set_interim(text: str) -> None`, `add_final(text: str) -> None`, `commit_interim() -> None`, `open() -> bool` (False for engine `local`), `edit(text: str, start: int, end: int) -> None` (code-point offsets from the pane; sets `edited`), `text() -> str` (settled text, interim not included), `snapshot() -> dict` (`{'id', 'engine', 'settled', 'interim', 'start', 'end', 'opened', 'listening'}`), `words() -> int`, `original() -> str` (concatenation of the engine's finals, for History/Training).
- Produces: `join_piece(before: str, piece: str, after: str) -> str` — the piece as it should be inserted between `before` and `after`.

Join rule (not determined by the signature): strip the piece; prefix a space unless `before` is empty or ends in whitespace; suffix a space if `after` is non-empty and does not start with whitespace or punctuation; if `before.rstrip()` is empty or ends in `.!?` (or a newline), capitalise the piece's first letter; otherwise lowercase it only when the first word is Titlecase, longer than one letter, not `I`/`I'm`/`I've`/`I'll`/`I'd`, and its remaining letters are lowercase.

- [ ] **Step 1: Failing tests** (`DictationSessionTests`)

```python
def test_finals_append_with_spaces_and_capitals(self):
    s = DictationSession('phrases', 1, None); s.add_final('Hello there.'); s.add_final('how are you?')
    self.assertEqual(s.text(), 'Hello there. How are you?')
def test_mid_sentence_piece_is_lowercased_but_not_I_or_names_in_caps(self):
    s = DictationSession('phrases', 1, None); s.add_final('I went to the'); s.add_final('Store')
    self.assertEqual(s.text(), 'I went to the store'); s.add_final('I think')
    self.assertEqual(s.text(), 'I went to the store I think')
def test_selection_is_replaced_by_the_next_final(self):
    s = DictationSession('phrases', 1, None); s.add_final('Send it to Jon today.'); s.open()
    s.edit(s.text(), 11, 14); s.add_final('John')
    self.assertEqual(s.text(), 'Send it to John today.')
def test_caret_in_the_middle_inserts_there(self):
    s = DictationSession('openai', 1, None); s.add_final('one three'); s.open(); s.edit('one three', 4, 4); s.add_final('two')
    self.assertEqual(s.text(), 'one two three')
def test_emoji_before_caret_uses_code_points(self):
    s = DictationSession('phrases', 1, None); s.open(); s.edit('🙂 hi', 4, 4); s.add_final('there')
    self.assertEqual(s.text(), '🙂 hi there')
def test_hebrew_join(self):
    s = DictationSession('phrases', 1, None); s.add_final('שלום'); s.add_final('מה נשמע')
    self.assertEqual(s.text(), 'שלום מה נשמע')
def test_interim_is_shown_not_settled(self):
    s = DictationSession('openai', 1, None); s.set_interim('hel'); self.assertEqual(s.text(), ''); self.assertEqual(s.snapshot()['interim'], 'hel')
    s.commit_interim(); self.assertEqual(s.text(), 'Hel')
def test_local_engine_never_opens(self):
    self.assertFalse(DictationSession('local', 1, None).open())
def test_edit_marks_edited_and_original_keeps_engine_text(self):
    s = DictationSession('phrases', 1, None); s.add_final('teh cat'); s.open(); s.edit('the cat', 7, 7)
    self.assertTrue(s.edited); self.assertEqual(s.original(), 'teh cat')
```

- [ ] **Step 2: Run** → fails (module missing).
- [ ] **Step 3: Implement** with a `threading.Lock` around every method (engines call from audio and worker threads, the bridge from pywebview threads). The caret is `(start, end)` in code points; `add_final` inserts `join_piece(text[:start], piece, text[end:])` and moves the caret to after the insertion; without `opened`, the caret follows the end of the text.
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat(live-words): the dictation session model`

### Task 3: Usage and cost counters

**Files:**
- Create: `src/koli/live_words_usage.py`
- Test: `tests/test_dictation_bubble.py`

**Interfaces:**
- Produces: `record(key: str, seconds: float, words: int, fixed: bool, tokens_in: int = 0, tokens_out: int = 0, path: Path | None = None, day: str | None = None) -> None`; `cost(key: str, seconds: float, tokens_in: int, tokens_out: int, prices: dict) -> float`; `summary(prices: dict, path: Path | None = None) -> list[dict]` — one row per engine key: `{'key', 'label', 'first_day', 'last_day', 'days', 'recordings', 'minutes', 'words', 'words_per_minute', 'fixes', 'cost'}`, newest `last_day` first. File: `<user app data>/live_words_usage.json`, `{"days": {"2026-10-28": {"<key>": {"recordings", "seconds", "words", "fixes", "tokens_in", "tokens_out"}}}}`.
- Prices shape: `{'openai/gpt-live-transcribe': {'per_minute': 0.017}, 'gemini/gemini-3.6-flash': {'input_per_million': 0.75, 'output_per_million': 3.75}}`; a key `cloud/<provider>/<model>` looks up `<provider>/<model>`; local engines and unknown models cost 0.

- [ ] **Step 1: Failing tests** (`LiveWordsUsageTests`): two records on one day add up; `cost('cloud/openai/gpt-live-transcribe', 120, 0, 0, prices)` == 0.034; Gemini cost from tokens (1_000_000 in, 0 out → 0.75); `summary` rows carry days, words per minute (`words / minutes`, 0 when no minutes) and fixes; a corrupt file reads as empty and is overwritten on the next record.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement** with a module lock, atomic write (`tmp` + `os.replace`), `get_user_app_data_path()` default.
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat(live-words): usage and cost counters`

### Task 4: A small WebSocket client

**Files:**
- Create: `src/koli/cloud_stt/__init__.py` (header comment only), `src/koli/cloud_stt/ws.py`
- Test: `tests/test_dictation_bubble.py`

**Interfaces:**
- Produces: `encode_frame(opcode: int, payload: bytes, mask_key: bytes) -> bytes`; `read_frame(read_exact: Callable[[int], bytes]) -> tuple[int, bool, bytes]` (opcode, fin, payload); `class WebSocket` with `connect(url: str, headers: dict, timeout: float = 10) -> WebSocket` (classmethod), `send_text(s: str)`, `recv() -> tuple[str, str | bytes] | None` (`('text', str)`, `('binary', bytes)`, None on close; answers pings, joins continuation frames), `close()`; constants `TEXT=1, BINARY=2, CLOSE=8, PING=9, PONG=10`.

- [ ] **Step 1: Failing tests** (`WebSocketFramingTests`): a masked 5-byte text frame round-trips through `read_frame` after unmasking; payload lengths 125, 126 and 70000 use the 7-bit, 16-bit and 64-bit length forms; a server frame (unmasked) is read; the handshake's `Sec-WebSocket-Accept` check rejects a wrong value (`_check_accept(key, accept) -> bool` with the RFC 6455 example `dGhlIHNhbXBsZSBub25jZQ==` → `s3pPLMBiTxaQ9kYGzzhZRbK+xOo=`).
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement** over `socket.create_connection` + `ssl.create_default_context().wrap_socket` for `wss`; HTTP/1.1 upgrade with a random 16-byte key; client frames always masked (`os.urandom(4)`); `recv` handles PING by sending PONG and CLOSE by replying and returning None; a lock around sends.
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat(cloud-stt): a standard-library WebSocket client`

### Task 5: OpenAI live transcription client

**Files:**
- Create: `src/koli/cloud_stt/openai_live.py`
- Test: `tests/test_dictation_bubble.py`

**Interfaces:**
- Consumes: `ws.WebSocket.connect`.
- Produces: `class OpenAILive(key: str, model: str, language: str, on_interim: Callable[[str], None], on_final: Callable[[str], None], on_error: Callable[[str], None], connect=WebSocket.connect)` with `start() -> None` (connects on a thread; audio fed before open is buffered), `feed(mono16k: np.ndarray) -> None` (float32 16 kHz → PCM16 24 kHz, coalesced into ≥100 ms `input_audio_buffer.append` events), `commit() -> None` (closes the current turn), `finish(timeout: float = 5.0) -> None` (commit, wait until every committed item has its `completed`, then close), `cancel() -> None`, `seconds_sent: float`, `transcribed_any: bool`, `failed: bool`, `unsent_audio() -> np.ndarray | None` (16 kHz audio since the last completed turn, for the Whisper fallback).
- Events in: `delta` → per-`item_id` interim text, `on_interim(text_so_far_of_newest_item)`; `completed` → `on_final(transcript)` in commit order (completions may arrive out of order: hold later ones until earlier ones arrive); `error` → `on_error(message)`.

- [ ] **Step 1: Failing tests** (`OpenAILiveTests`, a fake `connect` returning an object with `send_text`, a scripted `recv` queue and `close`): the first message is the exact `session.update` from Global Constraints with `languages: ['he']` (no `languages` key when language is ''); audio fed before the socket opens arrives after `session.update`; 1 s of 16 kHz audio becomes 48000 samples = 96000 bytes of PCM16 across the appends; deltas for item A produce growing interims; `completed` for item B before A is delivered after A; a connect failure before any transcript retries once after 250 ms (the fake fails the first call) and the second failure sets `failed` and calls `on_error`; `unsent_audio()` returns the audio of turns without a `completed`.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement** (resample with `soxr.resample(x, 16000, 24000)`, clip to int16; reader thread loops `recv()`; the key only in the header).
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat(cloud-stt): OpenAI live transcription client`

### Task 6: Gemini phrase transcriber

**Files:**
- Create: `src/koli/cloud_stt/gemini_phrase.py`
- Test: `tests/test_dictation_bubble.py`

**Interfaces:**
- Consumes: `llm.http.post_json`, `llm.transports.gemini.BASE_URL`.
- Produces: `transcribe(audio: np.ndarray, language: str, context: str, key: str, model: str, post=http.post_json, timeout: float = 20) -> tuple[str, int, int]` (text, prompt tokens, output tokens); `wav_bytes(mono16k: np.ndarray) -> bytes`.

Instruction text (exact): system `"You transcribe dictated speech. Reply with only the words spoken, verbatim, in the language spoken, with normal punctuation. No quotes, no notes. If nothing is said, reply with nothing."`; user parts: `"Language: <language name or 'detect it'>. What was said just before, for names and spelling: <context or 'nothing'>."` then the audio part `{'inline_data': {'mime_type': 'audio/wav', 'data': <base64>}}`.

- [ ] **Step 1: Failing tests** (`GeminiPhraseTests`, fake `post`): the URL is `.../models/<model>:generateContent`, the key is only in `x-goog-api-key`, the body holds one `inline_data` part of `audio/wav` whose bytes start with `RIFF`; text parts marked `thought` are dropped; usage tokens are returned; an empty candidate list returns `('', in, out)`.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement** (`wave` module into a `BytesIO`, PCM16 16 kHz mono).
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat(cloud-stt): Gemini transcribes a phrase`

### Task 7: The session in the dictation flow (Phrases engine, writing, pause, cancel)

**Files:**
- Modify: `src/koli/state_manager.py` — `_begin_recording` (542), `handle_vad_event` PHRASE_PAUSE and SILENCE_TIMEOUT (240–298), `stop_recording` (327), `cancel_active_recording` (353), `cancel_recording_hotkey_pressed` (409), `start_recording` (454), `handle_max_recording_duration_reached` (236), `_transcription_pipeline(_locked)` (1271–1650)
- Create: `src/koli/live_session_flow.py` — the engine-facing glue kept out of the 2,500-line state manager: `LiveWordsFlow(sm)`
- Delete: `src/koli/streaming_delivery.py`; remove `_setup_streaming_delivery`, `_finalize_streaming_delivery` and their state (`streaming_delivery`, `_streaming_delivery_active`) and the existing `StreamingDelivery*` tests in `tests/test_smoke.py:1959-2057`
- Modify: `src/koli/ui/events.py` (topic `live_words`)
- Test: `tests/test_dictation_bubble.py`

**Interfaces:**
- Consumes: `choose_engine`, `resolve_language`, `usage_key` (Task 1); `DictationSession` (Task 2); `live_words_usage.record` (Task 3).
- Produces on `StateManager`: `live: LiveWordsFlow`; `bubble_has_focus: Callable[[], bool]` (set by the host, default `lambda: False`); `bubble_focus: object | None` (host hook with `take() -> None` and `give_back(window: dict | None) -> bool`); `bubble_open() -> bool`; `bubble_edit(text: str, start: int, end: int) -> None`; `bubble_write(send: bool) -> bool`; `bubble_listen(on: bool) -> bool`; `bubble_cancel() -> None`.
- Produces on `LiveWordsFlow`: `begin(mode: str) -> None` (called from `_begin_recording` for hotkey dictations; decides the engine, creates or continues the session, publishes), `session: DictationSession | None`, `on_phrase(audio) -> None`, `on_stream(text: str, is_final: bool) -> None`, `on_recorder_stopped(audio) -> None` (transcribes the tail into the pane), `finish(send: bool) -> None`, `pause() -> None`, `cancel() -> None`, `publish() -> None` (`publish('live_words', session.snapshot() | {'notice': ...})`; `{'id': None}` when there is none).
- Produces on the pipeline: `_transcription_pipeline(audio_data, use_auto_enter=False, phrase=False, phrase_session=None, written: dict | None = None)` where `written = {'text': str, 'edited': bool, 'engine': str, 'original': str, 'duration': float}` skips Whisper, the speech checks and post-processing (already done per piece), runs the AI polish only when `not edited`, and continues at the rule/delivery block unchanged.

Flow decisions (from the spec; the implementer needs these, not more):
- Phrases engine on a hotkey dictation: `_begin_recording` passes `phrase_pause=live_words.phrase_pause_seconds`; PHRASE_PAUSE with a live session calls `live.on_phrase(take_recorded_audio())` instead of the auto-dictation branch; `on_phrase` runs on its own thread under `_pipeline_lock`: Whisper with the session's settled text appended to the initial prompt (last 200 characters), `_postprocess_piece` (rule formatting, learned corrections, no polish, no `strip_trailing_period`), then `session.add_final`. A phrase whose `session_id` is no longer current is dropped.
- `stop_recording(use_auto_enter)` with a live session: if `session.opened and bubble_has_focus()` → `live.pause()` (stop the recorder, tail into the pane, keep the session); else → `live.finish(send=use_auto_enter)`.
- `start_recording()` with an open, paused session → resumes listening into it (new recorder run, same session).
- SILENCE_TIMEOUT with an opened session → `live.pause()`; without → finish as today.
- `finish(send)`: wait for in-flight phrases (≤10 s), `session.commit_interim()`; empty text → `_emit_pipeline('empty', why='No speech detected')` + `flash_failure("No speech detected")`; if the session was opened, `bubble_focus.give_back(session.target_window)` (False → deliver to the fallback window path); then `_transcription_pipeline(None, send, written=...)`; record usage (`live_words_usage.record(usage_key, seconds, words, fixed=session.edited)`); History keeps the written text and `original` (`record_transcript(..., original=...)` — add the optional field).
- Off and Local engines: no change to the write path (Whisper as today); Local only shows the stream in the pane through `on_stream`.
- `cancel()`/Esc while the app has focus: drop the session, recorder cancelled, `record_transcript` with `cancelled=True` when the pane had text.
- `cancel_recording_hotkey_pressed` returns early when `bubble_has_focus()`.

- [ ] **Step 1: Failing tests** (`LiveWordsFlowTests`, a `StateManager.__new__` with mocks for recorder, engine, clipboard, config; `whisper_engine.transcribe_audio` returns scripted phrases):
  - phrases engine: two PHRASE_PAUSE events then stop → one `deliver_transcription` call with `'Hello there. How are you?'` and Whisper is not called for the final audio beyond the tail;
  - stop with an empty pane → no delivery, `flash_failure('No speech detected')` (Review Focus 1);
  - opened session + `bubble_has_focus()` True → stop pauses (no delivery, session kept); then `start_recording()` resumes the same session id; then stop with focus False → delivered once;
  - opened session → `give_back` is called with the start window; `give_back` returning False → the fallback window is shown instead of a paste (Review Focus 3);
  - a phrase finishing after `bubble_cancel()` is not added to the next session (Review Focus 5);
  - edited session → polish not called; unedited with `ai.polish.enabled` → polish called once on the whole text;
  - `bubble_write(send=True)` → `deliver_transcription(..., True, ...)`; hotkey release with an app rule `auto_send: true` → the rule's auto-send plan applies;
  - Esc via `cancel_recording_hotkey_pressed` while `bubble_has_focus()` → nothing cancelled;
  - auto-dictation recording → `live.session` stays None and phrase pastes behave as before.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement** `LiveWordsFlow` and the StateManager hooks; delete streaming delivery.
- [ ] **Step 4: Run** the new tests and the whole suite → pass.
- [ ] **Step 5: Commit** `feat(live-words): the bubble session drives the dictation; streaming delivery removed`

### Task 8: Local, OpenAI and Gemini feeds

**Files:**
- Modify: `src/koli/live_session_flow.py`, `src/koli/state_manager.py:300-314` (`handle_streaming_result` → `live.on_stream`), `src/koli/audio_recorder.py:361-362` (feed sherpa only while `stream_partials` is True, set per recording by the flow)
- Test: `tests/test_dictation_bubble.py`

**Interfaces:**
- Consumes: `OpenAILive` (Task 5), `gemini_phrase.transcribe` (Task 6), `llm.credentials.load`.
- Produces: in `LiveWordsFlow` — engine `openai`: an `OpenAILive` started at `begin`, fed by a recorder tap (`audio_recorder.add_tap`) only while recording, `commit()` on each PHRASE_PAUSE, `finish()` at stop, interims → `session.set_interim`, finals → `_postprocess_piece` → `session.add_final`; on `failed` mid-recording the flow takes `unsent_audio()` (plus the recorder tail) through Whisper into the pane; engine `gemini`: the Phrases path with `gemini_phrase.transcribe(audio, language, settled[-200:], key, model)` in Whisper's place, falling back to Whisper for that phrase on any exception; tokens summed for usage; engine `local`: `stream_partials` on, partials → `set_interim`, finals → `add_final` (display only).

- [ ] **Step 1: Failing tests**: OpenAI engine with a fake `OpenAILive` factory: interims update the snapshot, finals land post-processed, stop calls `finish` and the written text is the session's; a `failed` client mid-recording sends the unsent audio to Whisper and the result is appended (Review Focus 2); Gemini engine: a raising transcriber falls back to Whisper for that phrase; usage records `cloud/gemini/<model>` with summed tokens; Local engine: the final write is Whisper's full pass, not the stream.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → pass, whole suite green.
- [ ] **Step 5: Commit** `feat(live-words): local, OpenAI and Gemini engines feed the bubble`

### Task 9: Bridge, host focus and placement, hotkey focus rule

**Files:**
- Modify: `src/koli/ui/bridge.py` (calls next to `dictation_stop`), `src/koli/ui/host.py` (`BAR_TOPICS` + `live_words`; `_bar_geometry` uses a recording field; focus take/give back; `live_opacity`; set `sm.bubble_has_focus` and `sm.bubble_focus` in `attach`), `src/koli/ui/win32.py` (`set_activatable(hwnd, on: bool)`), `src/koli/platform/windows/input_watch.py` + `src/koli/platform/macos/input_watch.py` (`probe_focused() -> dict`), `src/koli/hotkey_listener.py:240,287` (stop and send keys return early when `state_manager.bubble_has_focus()`)
- Test: `tests/test_dictation_bubble.py`

**Interfaces:**
- Produces bridge calls: `bubble_open() -> bool`, `bubble_edit(text, start, end)`, `bubble_write(send=False) -> bool`, `bubble_listen(on) -> bool`, `bubble_cancel()`.
- Produces host: `_recording_field: tuple | None` set at recording start from `input_watch.probe_focused()['rect']` when editable (any `overlay.follow_focus` setting), cleared when the session ends; `take_bubble_focus()` (remember `win32.foreground()` as the target if it is not ours, `set_activatable(bar, True)`, `SetForegroundWindow(bar)`), `give_back_focus(window) -> bool` (`set_activatable(bar, False)`, `restore_foreground(hwnd)`, sleep 0.15, True when that window is in front); `bubble_has_focus() -> bool` (`win32.foreground() == bar_hwnd`); opacity: `live_opacity` while a pane is shown (page reports `resting=False` and a new `pane=True` flag in `bar_resize`).

- [ ] **Step 1: Failing tests**: the stop key and the send key do nothing while `bubble_has_focus()` is True (HotkeyListener built as in existing tests with a mock state manager); `bar_origin` with a recording field and position `bottom-center` returns the field-adjacent spot; `probe_focused` exists on both platform modules (contract test as in `test_screenshots_platform_module_has_the_contract`); bridge `bubble_write(True)` calls `sm.bubble_write(True)`.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat(live-words): bubble focus, placement beside the field and the hotkey focus rule`

### Task 10: The bubble pane

**Files:**
- Modify: `src/koli/ui/web/js/bar.js` (store `live: null`; `on('live_words')`; a `LivePane` component used by `Status` while recording or while a session is open), `src/koli/ui/web/css/bar.css` (pane styles, reuse existing tokens; check for an existing class before adding one), `src/koli/ui/web/js/mock-api.js` (a `live_words` sample for `?mock=1`)
- Test: manual in the mock page (`index.html?mock=1` has no bar; serve `bar.html?mock=1` the same way) and `tests/test_dictation_bubble.py` (a static check that `bar.js` calls each bridge name)

Pane behaviour (exact):
- Width 420 page px, 3–6 lines tall, scrolls to the newest words; `dir="auto"`; settled text normal, interim in a `<span class="interim">` (italic, secondary colour).
- Not opened: a read-only view; a `mousedown` on the text (engines other than `local`) calls `bubble_open`, then the view becomes a `contenteditable="plaintext-only"` box holding the settled text; the interim stays outside the editable box, after it.
- Opened: every `input`/`selectionchange` sends `bubble_edit(text, start, end)` with code-point offsets (`[...text.slice(0, i)].length`), debounced 120 ms; incoming snapshots replace the box's text only when it differs, keeping the caret at the snapshot's `start/end`.
- Keys in the box: Enter → `bubble_write(true)`; Shift+Enter → newline; Esc → `bubble_cancel()`.
- Footer: mic toggle (`bubble_listen`), engine name (+ a cloud mark for openai/gemini), Write (`bubble_write(false)`), Cancel.
- A `notice` in the snapshot shows once as a muted line.

- [ ] **Step 1: Failing test**: `bar.js` source contains `call('bubble_open'`, `'bubble_edit'`, `'bubble_write'`, `'bubble_listen'`, `'bubble_cancel'`, and listens on `'live_words'`.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement**; check in the mock page that the pane renders a sample with Hebrew and an interim, and that Enter, Esc and the buttons call the mock.
- [ ] **Step 4: Run** the test → pass.
- [ ] **Step 5: Commit** `feat(live-words): the bubble pane`

### Task 11: Insights card, doctor and docs

**Files:**
- Modify: `src/koli/ui/bridge.py` (`live_words_usage()` → `live_words_usage.summary(prices)`), `src/koli/ui/web/js/screens/history.js` (a **Words while you speak** card under the charts in Advanced mode, hidden when there are no rows), `src/koli/doctor.py` (engine, local model loaded, OpenAI/Gemini key found yes/no), `docs/project-index.md` (rows for Dictation bubble, Live words engines, Cloud STT; remove `streaming_delivery`), `docs/design/dictation-bubble.md` (status line), `CHANGELOG.md`
- Test: `tests/test_dictation_bubble.py`

- [ ] **Step 1: Failing tests**: `doctor` report includes a `Words while you speak` line naming the engine and whether the chosen provider has a key; the bridge call returns the summary rows.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement.** Card columns: engine, days used (first–last day), minutes, words, words per minute, fixes, cost (`$0.00` with 2–4 decimals).
- [ ] **Step 4: Run** the whole suite → pass; launch check with `/test-from-wsl`-equivalent `PYTHONUTF8=1 S:/Koli/.venv/Scripts/python.exe run-koli.py --doctor`.
- [ ] **Step 5: Commit** `feat(live-words): usage card, doctor line and docs`
