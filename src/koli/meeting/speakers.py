# meeting/speakers.py
# Who is talking, from the voice alone. Each loopback chunk becomes a
# voiceprint (a sherpa-onnx speaker embedding); voiceprints close to a known
# speaker's average join that speaker, the others start "Speaker N". The
# averages live in the meeting file, so a resumed meeting keeps its labels.
# Pure numpy apart from the embedding model, so the clustering is testable.

import logging
import threading
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Cosine between voiceprints. Measured on two synthetic voices with the
# VoxCeleb ERes2Net model: the same voice scores 0.8 on 4 s clips and 0.6 on
# 1.5 s crops, different voices under 0.3; real Discord audio of one person
# scored 0.26 on median between 1-2 s chunks, so the threshold is low and
# short chunks never open a speaker on their own.
SIMILARITY = 0.30
STICKY_MARGIN = 0.10     # the speaker who just spoke keeps the turn this far under the threshold
WEAK_MATCH = 0.15        # a short chunk joins its best match from here up
MIN_NEW_SPEECH_S = 2.0   # speech needed in a chunk before it may open a new speaker
COLOURS = 7              # colours 1..7 for the others; 0 is "You"
RATE = 16000


class SpeakerTracker:
    UNKNOWN = '?'

    def __init__(self, embed: Optional[Callable[[np.ndarray], Optional[np.ndarray]]] = None,
                 similarity: float = SIMILARITY):
        self.embed = embed
        self.similarity = similarity
        self._lock = threading.Lock()
        self._speakers = []       # [{'id','name','colour','centroid': np.ndarray,'count'}]
        self._last = None         # the speaker of the previous loopback chunk

    # Speakers from a meeting file (the "you" entry is ignored).
    def load(self, speakers: list):
        with self._lock:
            self._speakers = []
            self._last = None
            for s in speakers:
                if s.get('id') == 'you' or not s.get('centroid'):
                    continue
                self._speakers.append({'id': s['id'], 'name': s.get('name', s['id']), 'colour': int(s.get('colour', 1)),
                                       'centroid': self._unit(np.asarray(s['centroid'], dtype=np.float32)),
                                       'count': int(s.get('count', 1))})

    @staticmethod
    def _unit(v: np.ndarray) -> np.ndarray:
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else v

    def _to_dict(self, s: dict) -> dict:
        return {'id': s['id'], 'name': s['name'], 'colour': s['colour'],
                'centroid': [round(float(x), 5) for x in s['centroid']], 'count': s['count']}

    # The speaker of `audio` (mono float32 at 16 kHz): an existing id or a new
    # one, plus the speaker entry to store (None when nothing changed);
    # UNKNOWN when no voiceprint could be made. `speech_s` is how much speech
    # the chunk holds: a short chunk's voiceprint is unreliable, so it joins
    # its best match when there is any resemblance, else whoever spoke last,
    # and never opens a new speaker. Near the threshold the last speaker
    # keeps the turn. Only similarity joins move a centroid, so a chunk kept
    # by continuity can't pull a voiceprint off course.
    def label(self, audio, speech_s: Optional[float] = None) -> tuple:
        if self.embed is None:
            return self.UNKNOWN, None
        try:
            vec = self.embed(audio)
        except Exception as e:
            logger.debug(f"Speaker embedding failed: {e}")
            vec = None
        if vec is None:
            return self.UNKNOWN, None
        vec = self._unit(np.asarray(vec, dtype=np.float32))
        short = speech_s is not None and float(speech_s) < MIN_NEW_SPEECH_S
        with self._lock:
            best, best_sim = None, -1.0
            for s in self._speakers:
                sim = float(np.dot(s['centroid'], vec))
                if sim > best_sim:
                    best, best_sim = s, sim
            last = self._last if self._last in self._speakers else None
            last_sim = float(np.dot(last['centroid'], vec)) if last is not None else -1.0
            if best is not None and best_sim >= self.similarity:
                return self._join(best, vec)
            if last is not None and last_sim >= self.similarity - STICKY_MARGIN:
                return self._keep(last)
            if short and best is not None:
                if best_sim >= WEAK_MATCH:
                    return self._keep(best)
                return self._keep(last or best)
            n = self._next_number()
            s = {'id': f's{n}', 'name': f'Speaker {n}', 'colour': 1 + (n - 1) % COLOURS, 'centroid': vec, 'count': 1}
            self._speakers.append(s)
            self._last = s
            return s['id'], self._to_dict(s)

    # A similarity join: the voiceprint moves toward the new sample.
    def _join(self, speaker: dict, vec: np.ndarray) -> tuple:
        speaker['centroid'] = self._unit(speaker['centroid'] * speaker['count'] + vec)
        speaker['count'] += 1
        self._last = speaker
        return speaker['id'], self._to_dict(speaker)

    # A continuity join: the label only, the voiceprint stays as it was.
    def _keep(self, speaker: dict) -> tuple:
        self._last = speaker
        return speaker['id'], None

    def _next_number(self) -> int:
        numbers = [int(s['id'][1:]) for s in self._speakers if s['id'][1:].isdigit()]
        return (max(numbers) + 1) if numbers else 1

    def export(self) -> list:
        with self._lock:
            return [self._to_dict(s) for s in self._speakers]


# A voiceprint function over a sherpa-onnx speaker-embedding model. One
# extractor, one stream per call, serialised: the extractor is not
# thread-safe and only the transcriber calls it anyway.
def make_embedder(model_path: str, num_threads: int = 1) -> Callable[[np.ndarray], Optional[np.ndarray]]:
    import sherpa_onnx
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model_path, num_threads=num_threads, provider='cpu')
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
    lock = threading.Lock()

    def embed(audio: np.ndarray) -> Optional[np.ndarray]:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if len(samples) < RATE // 2:          # under half a second says nothing
            return None
        with lock:
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=RATE, waveform=samples)
            stream.input_finished()
            if not extractor.is_ready(stream):
                return None
            return np.asarray(extractor.compute(stream), dtype=np.float32)
    return embed
