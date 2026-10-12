# meeting/speakers.py
# Who is talking, from the voice alone. Each voice in a loopback chunk (see
# diarize.py) becomes a voiceprint (a sherpa-onnx speaker embedding);
# voiceprints close to a known speaker's average join that speaker, the
# others start "Speaker N". A speaker whose voice was heard in an earlier
# meeting is linked to that saved voice's id, and takes its name when it
# has one (voices.py). The averages live in the meeting file, so a resumed
# meeting keeps its labels. Pure numpy apart from the embedding model, so
# the clustering is testable.

import logging
import threading
from typing import Callable, Iterable, Optional

import numpy as np

from .voices import is_default_name

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
# The splitter sometimes cuts one person in two inside a chunk. Such a part
# still matches the speaker it was cut from: a real Meet made "Speaker 3"
# out of Speaker 1 (their voiceprints 0.86 alike), while the three real
# people stayed 0.12-0.30 apart. At this similarity the split is ignored.
SAME_VOICE = 0.7
FIRM_COUNT = 3            # chunks before a speaker's voiceprint is checked against saved voices again
COLOURS = 7              # colours 1..7 for the others; 0 is "You"
RATE = 16000


class SpeakerTracker:
    UNKNOWN = '?'

    def __init__(self, embed: Optional[Callable[[np.ndarray], Optional[np.ndarray]]] = None,
                 similarity: float = SIMILARITY, voices=None):
        self.embed = embed
        self.similarity = similarity
        self.voices = voices      # the VoiceBook of saved voices, or None
        self._lock = threading.Lock()
        # [{'id','name','colour','centroid': np.ndarray,'count','voice_id' (when known)}]
        self._speakers = []
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
                                       'count': int(s.get('count', 1)), 'voice_id': s.get('voice_id')})

    @staticmethod
    def _unit(v: np.ndarray) -> np.ndarray:
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else v

    # `voice`, when given, tells the session a saved voice's name was matched.
    def _to_dict(self, s: dict, voice: Optional[str] = None) -> dict:
        entry = {'id': s['id'], 'name': s['name'], 'colour': s['colour'],
                 'centroid': [round(float(x), 5) for x in s['centroid']], 'count': s['count']}
        if s.get('voice_id'):
            entry['voice_id'] = s['voice_id']
        if voice:
            entry['voice'] = voice
        return entry

    # The speaker of `audio` (mono float32 at 16 kHz): an existing id or a new
    # one, plus the speaker entry to store (None when nothing changed);
    # UNKNOWN when no voiceprint could be made. `speech_s` is how much speech
    # the chunk holds: a short chunk's voiceprint is unreliable, so it joins
    # its best match when there is any resemblance, else whoever spoke last,
    # and never opens a new speaker. Near the threshold the last speaker
    # keeps the turn. Only similarity joins move a centroid, so a chunk kept
    # by continuity can't pull a voiceprint off course. `exclude` holds the
    # speakers already given to other voices of the same chunk: the splitter
    # heard those as different people, so this voice can't be one of them.
    # The entry carries 'voice' when a remembered name fits a "Speaker N".
    def label(self, audio, speech_s: Optional[float] = None, exclude: Iterable[str] = ()) -> tuple:
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
        exclude = set(exclude)
        with self._lock:
            candidates = [s for s in self._speakers if s['id'] not in exclude]
            best, best_sim = None, -1.0
            for s in candidates:
                sim = float(np.dot(s['centroid'], vec))
                if sim > best_sim:
                    best, best_sim = s, sim
            last = self._last if self._last in candidates else None
            last_sim = float(np.dot(last['centroid'], vec)) if last is not None else -1.0
            twin, twin_sim = None, SAME_VOICE
            for s in self._speakers:
                if s['id'] in exclude:
                    sim = float(np.dot(s['centroid'], vec))
                    if sim >= twin_sim:
                        twin, twin_sim = s, sim
            if twin is not None and (best is None or twin_sim > best_sim):
                return self._join(twin, vec)
            if best is not None and best_sim >= self.similarity:
                return self._join(best, vec)
            if last is not None and last_sim >= self.similarity - STICKY_MARGIN:
                return self._keep(last)
            if short and best is not None:
                if best_sim >= WEAK_MATCH:
                    return self._keep(best)
                return self._keep(last or best)
            n = self._next_number()
            s = {'id': f's{n}', 'name': f'Speaker {n}', 'colour': 1 + (n - 1) % COLOURS,
                 'centroid': vec, 'count': 1, 'voice_id': None}
            named = self._link_saved_voice(s, vec, first=True)
            self._speakers.append(s)
            self._last = s
            return s['id'], self._to_dict(s, voice=named)

    # A similarity join: the voiceprint moves toward the new sample. A
    # speaker not yet linked to a saved voice is checked again once its
    # voiceprint is firm: the first chunks of a voice are often too short.
    def _join(self, speaker: dict, vec: np.ndarray) -> tuple:
        speaker['centroid'] = self._unit(speaker['centroid'] * speaker['count'] + vec)
        speaker['count'] += 1
        self._last = speaker
        named = None
        if not speaker.get('voice_id') and speaker['count'] >= FIRM_COUNT:
            named = self._link_saved_voice(speaker, speaker['centroid'])
        return speaker['id'], self._to_dict(speaker, voice=named)

    # Link `speaker` to the saved voice its voiceprint matches (skipping
    # voices already given to others in this meeting). A "Speaker N" takes
    # that voice's name; returns the name taken, if any. Under the lock.
    def _link_saved_voice(self, speaker: dict, vec: np.ndarray, first: bool = False) -> Optional[str]:
        if self.voices is None:
            return None
        try:
            found = self.voices.match(vec, taken=[s['voice_id'] for s in self._speakers if s.get('voice_id')],
                                      first=first)
        except Exception as e:
            logger.debug(f"Saved voices unavailable: {e}")
            return None
        if not found:
            return None
        speaker['voice_id'] = found['id']
        if found['name'] and is_default_name(speaker['name']):
            speaker['name'] = found['name']
            return found['name']
        return None

    # The saved voice a speaker was kept as when the meeting ended.
    def set_voice_id(self, speaker_id: str, voice_id: str):
        with self._lock:
            for s in self._speakers:
                if s['id'] == speaker_id:
                    s['voice_id'] = voice_id

    # You renamed a speaker: later matches against remembered voices must
    # skip that name, and a new speaker can't take it.
    def rename(self, speaker_id: str, name: str):
        with self._lock:
            for s in self._speakers:
                if s['id'] == speaker_id:
                    s['name'] = name

    # Two labels you said are one person: `from_id` joins `into_id`, their
    # voiceprints combined by how many chunks made each.
    def merge(self, from_id: str, into_id: str):
        with self._lock:
            source = next((s for s in self._speakers if s['id'] == from_id), None)
            target = next((s for s in self._speakers if s['id'] == into_id), None)
            if source is None or target is None or source is target:
                return
            target['centroid'] = self._unit(target['centroid'] * target['count'] + source['centroid'] * source['count'])
            target['count'] += source['count']
            target['voice_id'] = target.get('voice_id') or source.get('voice_id')
            self._speakers.remove(source)
            if self._last is source:
                self._last = target

    # A speaker's voiceprint and how many chunks made it, for remembering.
    def voiceprint(self, speaker_id: str) -> Optional[tuple]:
        with self._lock:
            for s in self._speakers:
                if s['id'] == speaker_id:
                    return [float(x) for x in s['centroid']], s['count']
        return None

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
