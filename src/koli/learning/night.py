# learning/night.py
# The night model: a bigger, slower Whisper that hears a clip again with a
# wider search and no prompt, the way the night pass will (phase 2). Today it
# backs the Training screen's Test button: you see how the night pass would
# hear one clip, and nothing is saved or learned. Hebrew goes to the Hebrew
# model, everything else to the official large-v3. The model loads on the
# first test and unloads after a few quiet minutes, so it never sits in
# memory next to your dictation model for long.
# Design: docs/design/self-learning.md › Phase 2.

import difflib
import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

TEACHERS = {'he': 'hebrew-large-v3'}   # by language; anything else uses DEFAULT_TEACHER
DEFAULT_TEACHER = 'large'               # the official large-v3
NIGHT_BEAM = 8                          # wider than dictation's 5: slower, more careful
IDLE_UNLOAD_S = 180                     # unload after this long without a test
SPEECH_MIN_PROB = 0.5                   # a dropped clip whose words average this is real speech


def teacher_for(language: Optional[str]) -> str:
    return TEACHERS.get(language or '', DEFAULT_TEACHER)


# The night text's words, each marked when it differs from what Koli heard.
# Both sides are normalised like practice's comparison (case and punctuation
# don't count). Returns (words, same).
def compare(live_text: str, night_text: str) -> tuple:
    from ..practice.align import tokens
    live = tokens(live_text or '')
    night = tokens(night_text or '')
    matcher = difflib.SequenceMatcher(a=[t[1] for t in live], b=[t[1] for t in night], autojunk=False)
    words = []
    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        for j in range(b0, b1):
            words.append({'text': night[j][0], 'changed': op != 'equal'})
    same = [t[1] for t in live] == [t[1] for t in night]
    return words, same


class NightModel:
    def __init__(self, engine_provider: Callable[[], object], clock: Callable[[], float] = time.monotonic):
        self._engine = engine_provider       # the live WhisperEngine: device, precision, registry
        self._clock = clock
        self._models = {}                    # key → WhisperModel, while loaded
        self._lock = threading.Lock()
        self._last_used = 0.0
        self._timer: Optional[threading.Timer] = None

    def _live(self):
        engine = self._engine()
        if engine is None or getattr(engine, 'model', None) is None:
            raise ValueError('The speech model is still loading')
        return engine

    # Night models not on this computer yet, for the clip's language.
    def missing(self, language: Optional[str]) -> list:
        registry = getattr(self._live(), 'registry', None)
        keys = [teacher_for(language)] if language else [DEFAULT_TEACHER]
        if registry is None:
            return []
        return [k for k in keys if not registry.is_model_cached(k)]

    # ── Loading ─────────────────────────────────────────────────────────────

    def _model(self, key: str):
        from ..whisper_engine import ENGINE_THREAD
        with self._lock:
            model = self._models.get(key)
        if model is not None:
            return model
        engine = self._live()
        source = engine._get_model_source(key)
        from faster_whisper import WhisperModel
        print(f"🌙 Loading the night model [{key}] for a test...")
        model = ENGINE_THREAD.run(WhisperModel, source, device=engine.device,
                                  device_index=engine.device_index if engine.device == 'cuda' else 0,
                                  compute_type=engine.compute_type)
        with self._lock:
            self._models[key] = model
        return model

    def unload(self):
        from ..whisper_engine import ENGINE_THREAD
        with self._lock:
            models, self._models = self._models, {}
            self._timer = None
        if not models:
            return

        def free():
            models.clear()
            import gc
            gc.collect()
        ENGINE_THREAD.run(free)
        print("🌙 Night model unloaded")

    def _schedule_unload(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(IDLE_UNLOAD_S, self.unload)
            self._timer.daemon = True
            self._timer.start()

    # ── Hearing a clip ──────────────────────────────────────────────────────

    def _decode(self, key: str, audio, language: Optional[str]) -> dict:
        from ..whisper_engine import ENGINE_THREAD
        model = self._model(key)

        def run():
            segments, info = model.transcribe(audio, beam_size=NIGHT_BEAM, language=language or None,
                                              task='transcribe', condition_on_previous_text=False,
                                              word_timestamps=True, vad_filter=False)
            words, texts = [], []
            for s in segments:
                texts.append(s.text)
                for w in s.words or []:
                    words.append({'word': w.word, 'prob': round(float(w.probability), 3)})
            return {'text': ''.join(texts).strip(), 'words': words, 'language': info.language,
                    'language_probability': round(float(info.language_probability or 0), 3)}
        return ENGINE_THREAD.run(run)

    # How the night pass would hear `audio`. An unknown language is detected
    # by the official model first; Hebrew is then heard again by the Hebrew one.
    def hear(self, audio, language: Optional[str]) -> dict:
        started = self._clock()
        key = teacher_for(language) if language else DEFAULT_TEACHER
        result = self._decode(key, audio, language)
        if not language and teacher_for(result['language']) != key:
            key = teacher_for(result['language'])
            result = self._decode(key, audio, result['language'])
        probs = [w['prob'] for w in result['words']]
        result.update(model=key, seconds=round(self._clock() - started, 1),
                      mean_prob=round(sum(probs) / len(probs), 3) if probs else 0.0)
        self._last_used = self._clock()
        self._schedule_unload()
        return result
