# services/practice.py
# Practice, assembled for the app window: the intake, a round's readings
# (written by the AI model or taken from the built-in packs), recording one
# reading from the microphone, Whisper's two passes over it, the comparison,
# the findings, and saving the ones you keep as Learned vocabulary. While a
# round is open, hotkeys are paused quietly and the wake word sleeps, so
# nothing is typed anywhere. Design: docs/design/voice-practice.md.

import logging
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from .. import learned_vocabulary, llm
from ..practice import findings as rules
from ..practice import intake as intake_mod
from ..practice import texts
from ..practice.session import FIRST, PARAGRAPH, RESULTS, SECOND, Round, trim_silence

logger = logging.getLogger(__name__)

SENTENCE_LIMIT_S, PARAGRAPH_LIMIT_S = 30, 150     # longest reading
SENTENCE_PAUSE_S, PARAGRAPH_PAUSE_S = 1.5, 2.5     # quiet that ends a reading
WAIT_TO_START_S = 10                               # time to start speaking after Record


class PracticeService:
    def __init__(self, sm, config_manager, store=None, folder: Optional[Path] = None,
                 history_loader=None, capture=None, engine=None):
        self.sm = sm
        self.cm = config_manager
        self.store = store or learned_vocabulary.get()
        if folder is None:
            from ..utils import get_user_app_data_path
            folder = Path(get_user_app_data_path()) / 'practice'
        self.folder = Path(folder)
        self._history = history_loader
        self._capture = capture            # (seconds, pause, stop) -> audio; tests pass a fake
        self._engine = engine              # tests pass a fake; else the state manager's
        self.round: Optional[Round] = None
        self._stop = threading.Event()
        self._was_paused = None
        self._lock = threading.Lock()

    # ── Setup ───────────────────────────────────────────────────────────────

    def _setting(self, path, default=None):
        value = self.cm.get_path(path, default)
        return default if value is None else value

    def _default_languages(self) -> list:
        chosen = self._setting('whisper.languages') or []
        language = self._setting('whisper.language') or 'auto'
        return list(chosen) or ([language] if language != 'auto' else ['en'])

    def intake(self) -> dict:
        return intake_mod.normalize_intake(self.store.intake(), self._default_languages())

    def _terms(self, intake: dict) -> list:
        history = self._history() if self._history else self._load_history()
        return intake_mod.personal_terms(intake, history, self._setting('postprocess.corrections') or {},
                                         self._setting('postprocess.replacements') or [], self.store.items())

    @staticmethod
    def _load_history() -> list:
        from ..transcript_log import load_transcripts
        try:
            return load_transcripts()
        except Exception:
            return []

    # What the intake screen shows: your answers, the terms that would be sent,
    # the model that will write and analyse (or none), and which languages have
    # built-in texts.
    def state(self) -> dict:
        intake = self.intake()
        model = llm.model_for('practice')
        return {
            'intake': intake, 'has_intake': bool(self.store.intake()), 'domains': intake_mod.DOMAINS,
            'terms': self._terms(intake), 'model': model, 'model_kind': llm.kind_of(model) if model else '',
            'packs': {lang: texts.has_pack(lang) for lang in intake['languages']},
            'word_confidence': hasattr(self._whisper(), 'transcribe_words'),
            'round': self.round.view() if self.round else None,
            'notes': self.store.snapshot().get('notes') or {},
        }

    def save_intake(self, raw: dict) -> dict:
        intake = intake_mod.normalize_intake(raw, self._default_languages())
        self.store.set_intake(intake)
        return intake

    def _whisper(self):
        return self._engine or getattr(self.sm, 'whisper_engine', None)

    # ── A round ─────────────────────────────────────────────────────────────

    def start(self) -> dict:
        intake = self.intake()
        model = llm.model_for('practice')
        with self._lock:
            self.round = Round(self.folder, intake, self._terms(intake), self._mic(), model)
            self.round.planned = sum(p['count'] for p in texts.plan(intake['languages']))
            self._pause(True)
        missing = [l for l in intake['languages'][:2] if not texts.has_pack(l)]
        if missing and not model:
            self.round.notes.append(f"No built-in texts for {', '.join(missing)}: connect an AI model in "
                                    "Settings › AI to practise them.")
        self._write_batch(second=False)
        return self.round.view()

    def _mic(self) -> str:
        try:
            return self.sm._mic_key()
        except Exception:
            return 'default'

    def _write_batch(self, second: bool):
        r = self.round
        batch = texts.half(texts.plan(r.intake['languages']), second)
        written, error = texts.sentences(batch, r.intake, r.terms, r.failed_words(), r.already_read(),
                                         use_model=bool(r.model))
        if error:
            r.notes.append(error)
        r.add(written)

    # Where the round goes after the current stage is read: the second batch,
    # then the paragraph (in the language with more mistakes, aimed at what
    # failed), then the results.
    def next(self) -> dict:
        r = self._round()
        if not r.stage_done():
            return r.view()
        if r.stage == FIRST:
            r.stage = SECOND
            self._write_batch(second=True)
        elif r.stage == SECOND:
            r.stage = PARAGRAPH
            accuracy = r.accuracy()
            languages = [l for l in r.intake['languages'] if l in accuracy] or r.intake['languages']
            language = min(languages, key=lambda l: accuracy.get(l, 1.0))
            targets = list(dict.fromkeys(r.failed_words() + [t for i in r.items for t in i['targets']]))[:15]
            p, error = texts.paragraph(r.intake, targets, language, r.already_read(), use_model=bool(r.model))
            if error:
                r.notes.append(error)
            if p:
                r.add([p], kind='paragraph')
            else:
                r.stage = RESULTS
        elif r.stage == PARAGRAPH:
            r.stage = RESULTS
        return r.view()

    def skip(self, index: int) -> dict:
        r = self._round()
        r.item(index)['skipped'] = True
        return r.view()

    # "This text has a mistake": the reading counts for nothing (the AI that
    # wrote it can misspell a word, and Koli would learn the misspelling).
    def flag(self, index: int) -> dict:
        r = self._round()
        r.item(index)['flagged'] = True
        r.save()
        return r.view()

    def _round(self) -> Round:
        if self.round is None:
            raise ValueError('No practice round is open')
        return self.round

    # ── Reading one text ────────────────────────────────────────────────────

    def stop(self):
        self._stop.set()

    # Records one reading until you pause (or Stop), then Whisper's two
    # passes and the comparison. Returns the reading with its results.
    def record(self, index: int) -> dict:
        r = self._round()
        item = r.item(index)
        paragraph = item['kind'] == 'paragraph'
        self._stop.clear()
        audio = self._record_audio(PARAGRAPH_LIMIT_S if paragraph else SENTENCE_LIMIT_S,
                                   PARAGRAPH_PAUSE_S if paragraph else SENTENCE_PAUSE_S)
        if audio is None or len(audio) == 0:
            raise ValueError("Didn't hear anything. Press Record, then read the text.")
        audio = trim_silence(audio)
        started = time.monotonic()
        audio_file = r.save_audio(index, audio)
        dictated, bare = self._transcribe(audio)
        if not (dictated or {}).get('text') and not (bare or {}).get('text'):
            raise ValueError("Whisper didn't make out any words. Try again, a little closer to the microphone.")
        return r.set_result(index, dictated, bare, audio_file, time.monotonic() - started)

    def _record_audio(self, limit: float, pause: float):
        if self._capture is not None:
            return self._capture(limit, pause, self._stop)
        listener = getattr(self.sm, '_wake_listener', None)
        if listener is None:
            raise ValueError("Recording isn't available in this mode")
        return listener.capture_word(timeout=WAIT_TO_START_S, max_seconds=limit, end_silence_s=pause, stop=self._stop)

    # As you dictate (your Language setting, context prompt and vocabulary)
    # and bare (nothing but your voice).
    def _transcribe(self, audio) -> tuple:
        engine = self._whisper()
        if engine is None:
            raise ValueError('The speech model is still loading')
        language = self._setting('whisper.language') or 'auto'
        prompt = self._setting('whisper.initial_prompt') or ''
        if hasattr(engine, 'transcribe_words'):
            dictated = engine.transcribe_words(audio, language=language, prompt=prompt, with_vocabulary=True)
            bare = engine.transcribe_words(audio, language=language, with_vocabulary=False)
            return dictated, bare
        # whisper.cpp: text only, no word confidence.
        text = engine.transcribe_audio(audio, language=None if language == 'auto' else language, quiet=True) or ''
        bare_text = engine.transcribe_audio(audio, language=None if language == 'auto' else language,
                                            use_prompt=False, hotwords='', quiet=True) or ''
        return {'text': text, 'words': None}, {'text': bare_text, 'words': None}

    # ── Results ─────────────────────────────────────────────────────────────

    # The rules' findings and, with a model, its own and its notes; minus what
    # you already have. Each finding gets an id for Apply.
    def results(self) -> dict:
        r = self._round()
        r.stage = RESULTS
        readings = r.readings()
        found = rules.rule_findings(readings, r.intake.get('work', ''))
        if r.model and readings:
            answer = llm.complete('practice', rules.model_messages(r.intake, readings, r.context_effects()), json=True)
            if answer.ok:
                model_findings, notes, text_errors = rules.parse_model_answer(answer.text)
                found = rules.drop_text_errors(rules.merge(found, model_findings), text_errors)
                r.model_notes = notes
                if text_errors:
                    r.notes.append(f"The AI found mistakes in its own texts ({', '.join(text_errors)}); "
                                   "nothing is learned from those words.")
            elif answer.error not in r.notes:
                r.notes.append(f"The AI analysis didn't run: {answer.error}")
        found = rules.drop_known(found, self._setting('whisper.hotwords') or [],
                                 self._setting('postprocess.corrections') or {}, self.store.items())
        for f in found:
            f['id'] = uuid.uuid4().hex[:10]
        r.findings = found
        r.save()
        return {'round': r.view(), 'findings': found, 'notes': r.model_notes,
                'context': r.context_effects(), 'accuracy': r.accuracy()}

    # Saves the findings you kept as Learned, with the notes and the round.
    def apply(self, ids: list) -> int:
        r = self._round()
        wanted = set(ids or [])
        chosen = [f for f in r.findings if f['id'] in wanted]
        added = self.store.add(chosen, mic=r.mic, session=r.id)
        for language, note in (r.model_notes or {}).items():
            self.store.set_note(language, note)
        self.store.add_session({'id': r.id, 'date': r.started, 'mic': r.mic, 'model': r.model,
                                'languages': r.intake['languages'], 'accuracy': r.accuracy(),
                                'readings': len(r.read()), 'kept': added})
        self.close()
        return added

    def close(self):
        self._stop.set()
        with self._lock:
            self.round = None
            self._pause(False)

    # Hotkeys off while practising, without the "Hotkeys paused" notice; your
    # own pause is left as it was.
    def _pause(self, on: bool):
        sm = self.sm
        if sm is None or not hasattr(sm, 'is_paused'):
            return
        if on:
            if self._was_paused is None:
                self._was_paused = bool(sm.is_paused)
                sm.is_paused = True
        elif self._was_paused is not None:
            sm.is_paused = self._was_paused
            self._was_paused = None

    # ── Learned ─────────────────────────────────────────────────────────────

    def learned(self) -> dict:
        data = self.store.snapshot()
        return {'items': data['items'], 'notes': data.get('notes') or {}, 'sessions': data.get('sessions') or []}

    def delete(self, ids: list) -> int:
        return self.store.delete(ids)

    # Start fresh: every learned item, note and round, and the recordings.
    def delete_all(self):
        self.store.delete_all()
        if self.round is None and self.folder.exists():
            shutil.rmtree(self.folder, ignore_errors=True)
