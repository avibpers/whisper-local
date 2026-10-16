# agent/approval.py
# Your spoken password for dangerous AI actions. The phrase is kept encrypted
# with the AI keys (llm/credentials.py), never in the settings file. You
# record it RECORD_TAKES times, like the wake word: Koli keeps the ways
# Whisper wrote it (it may never write it the way you typed it) and your
# voice saying it, per microphone. To approve, you say it once: every word
# must match the phrase or one of those spellings (words_match), and the voice
# must match your recordings of it (or, without them on this microphone, your
# wake word's voiceprint). Without either, the bubble's Approve button is the
# fallback, and the bubble says why.

import difflib
import logging
import re
from typing import Callable, List, Optional

import numpy as np

from ..utils import normalize_spoken

logger = logging.getLogger(__name__)

CREDENTIAL = 'agent_password'
RECORD_TAKES = 3
MIN_TAKE_MATCH = 0.6        # a recording must roughly be the phrase, or it was something else
MIN_WORD_MATCH = 0.7        # each word, so Whisper's spelling may vary
MIN_JOINED_MATCH = 0.9      # the whole phrase, when Whisper split or joined words
# The voice check never goes below this, whatever the wake word's own
# setting: it guards deleting. (One account had the wake word at 0.30 while
# other voices scored up to 0.28.)
MIN_VOICE_MATCH = 0.45
MIN_PHRASE_CHARS = 6
LISTEN_SECONDS = 8.0


# Whether what Whisper heard is the password. Word by word: one whole-phrase
# score let "koli approve green" pass for "koli approve blue". When the word
# count differs (Whisper wrote "approveblue"), the joined letters must be
# nearly the same.
def words_match(heard: str, phrase: str) -> bool:
    said, wanted = heard.split(), phrase.split()
    if not said or not wanted:
        return False
    if len(said) == len(wanted):
        return all(difflib.SequenceMatcher(None, a, b).ratio() >= MIN_WORD_MATCH for a, b in zip(said, wanted))
    return difflib.SequenceMatcher(None, ''.join(said), ''.join(wanted)).ratio() >= MIN_JOINED_MATCH


def _valid_phrase(phrase: str) -> str:
    phrase = ' '.join(str(phrase or '').split())
    if len(normalize_spoken(phrase)) < MIN_PHRASE_CHARS:
        raise ValueError(f"Use a phrase of at least {MIN_PHRASE_CHARS} letters, like “Koli approve blue”")
    return phrase


class Approval:
    # `listen(stop)` → audio or None; `transcribe(audio)` → text;
    # `similarity(audio)` → score against your wake word or None;
    # `voice_match()` → the threshold; `has_voice()` → whether your wake
    # voiceprint exists on this microphone; `voiceprint(audio)` → a vector or
    # None; `mic()` → this microphone's key.
    def __init__(self, listen: Callable = None, transcribe: Callable = None, similarity: Callable = None,
                 voice_match: Callable[[], float] = None, has_voice: Callable[[], bool] = None,
                 voiceprint: Callable = None, mic: Callable[[], str] = None, store=None):
        self.listen = listen
        self.transcribe = transcribe
        self.similarity = similarity
        self.voice_match = voice_match or (lambda: 0.45)
        self.has_voice = has_voice or (lambda: False)
        self.voiceprint = voiceprint
        self.mic = mic or (lambda: 'default')
        if store is None:
            from ..llm import credentials as store
        self.store = store
        self._takes: List[dict] = []      # recordings in progress, until saved
        self._new_phrase = ''

    def _data(self) -> dict:
        data = self.store.load(CREDENTIAL)
        return data if isinstance(data, dict) else {}

    def phrase(self) -> str:
        return str(self._data().get('phrase') or '')

    def clear(self) -> None:
        self.store.delete(CREDENTIAL)

    # Whether your voice saying the password is recorded on this microphone.
    def recorded_here(self) -> bool:
        return self._my_voice() is not None

    def can_listen(self) -> bool:
        return self.listen is not None and self.transcribe is not None and (self.recorded_here() or self.has_voice())

    # The language Whisper should hear the phrase in, from its letters.
    def language(self) -> Optional[str]:
        phrase = self._new_phrase or self.phrase()
        return 'he' if re.search(r'[֐-׿]', phrase) else 'en'

    # ── Recording it ──

    def begin_recording(self, phrase: str) -> None:
        self._new_phrase = _valid_phrase(phrase)
        self._takes = []

    # One recording of you saying the phrase: how Whisper wrote it and your
    # voiceprint. Refused when it was something else, or too short for a voice.
    def record_take(self) -> dict:
        if not self._new_phrase:
            raise ValueError('Type the password first')
        audio = self.listen(None) if self.listen else None
        if audio is None:
            raise ValueError("Didn't hear anything. Press Record, then say your password.")
        heard = (self.transcribe(audio) or '').strip()
        said, wanted = normalize_spoken(heard), normalize_spoken(self._new_phrase)
        if not said:
            raise ValueError("Couldn't make out the words. Say it again, a little louder.")
        if difflib.SequenceMatcher(None, said.replace(' ', ''), wanted.replace(' ', '')).ratio() < MIN_TAKE_MATCH:
            raise ValueError(f"That didn't sound like your password (heard “{heard}”). Say it again.")
        vec = self.voiceprint(audio) if self.voiceprint else None
        if vec is None:
            raise ValueError('Too short or too quiet to learn your voice from. Say it again.')
        self._takes.append({'heard': said, 'vec': np.asarray(vec, dtype=np.float32)})
        return {'heard': heard, 'count': len(self._takes), 'needed': RECORD_TAKES}

    # The recordings become the password on this microphone. The same phrase
    # recorded on another microphone adds to it; a new phrase starts over.
    def save_recording(self) -> None:
        if len(self._takes) < RECORD_TAKES:
            raise ValueError(f'Say it {RECORD_TAKES} times first')
        data = self._data()
        same = normalize_spoken(data.get('phrase') or '') == normalize_spoken(self._new_phrase)
        spellings = list(data.get('heard') or []) if same else []
        voices = dict(data.get('voices') or {}) if same else {}
        for take in self._takes:
            if take['heard'] not in spellings:
                spellings.append(take['heard'])
        mean = np.mean([t['vec'] / (np.linalg.norm(t['vec']) or 1.0) for t in self._takes], axis=0)
        voices[self.mic()] = [round(float(v), 6) for v in mean / (np.linalg.norm(mean) or 1.0)]
        self.store.save(CREDENTIAL, {'phrase': self._new_phrase, 'heard': spellings[-12:], 'voices': voices})
        self._takes, self._new_phrase = [], ''

    def _my_voice(self) -> Optional[np.ndarray]:
        vec = (self._data().get('voices') or {}).get(self.mic())
        return np.asarray(vec, dtype=np.float32) if vec else None

    # ── Checking it ──

    # How much `audio` sounds like your recordings of the password here, or
    # like your wake word when the password isn't recorded on this microphone.
    def _voice_score(self, audio) -> Optional[float]:
        mine = self._my_voice()
        if mine is None or self.voiceprint is None:
            return self.similarity(audio) if self.similarity else None
        vec = self.voiceprint(audio)
        if vec is None:
            return None
        vec = np.asarray(vec, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        return float(np.dot(vec / norm, mine)) if norm > 0 else None

    # (ok, why) for one recording of you saying the password.
    def verify(self, audio) -> tuple:
        data = self._data()
        phrase = str(data.get('phrase') or '')
        if not phrase:
            return False, 'no password is set'
        # Whisper isn't told the phrase: a hint would make it hear the
        # password in anything. The ways it wrote your recordings count too.
        heard = normalize_spoken(self.transcribe(audio) or '')
        spellings = [normalize_spoken(phrase)] + list(data.get('heard') or [])
        if not any(words_match(heard, s) for s in spellings):
            return False, "the words don't match"
        score = self._voice_score(audio)
        if score is None:
            return False, "your voice can't be checked on this microphone"
        needs = max(self.voice_match(), MIN_VOICE_MATCH)
        logger.info(f"AI action password: voice {score:.2f} (needs {needs:.2f}, "
                    f"{'your password recordings' if self._my_voice() is not None else 'your wake word'})")
        if score < needs:
            return False, "it doesn't sound like you"
        return True, ''

    # Listens once (until `stop` is set or LISTEN_SECONDS pass) and checks.
    def ask_once(self, stop=None) -> tuple:
        audio = self.listen(stop) if self.listen else None
        if audio is None:
            return False, 'nothing was said'
        return self.verify(audio)
