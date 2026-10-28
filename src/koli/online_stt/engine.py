# online_stt/engine.py
# An online speech-to-text model in front of the local Whisper engine, with
# the same transcribe_audio / last_info the callers already use (like
# whisper_engine_cpp.py). Silence never leaves the computer (the local VAD
# checks first), each call is written to the usage ledger with its cost, and
# when the provider fails the same clip is transcribed locally, so a dead
# network never loses a dictation. Design: docs/design/online-models.md.

import logging
import time
from typing import Callable, Optional

import numpy as np

from ..llm import describe_error, routing
from ..llm.providers import PROVIDERS
from ..usage import ledger, prices
from . import gemini, openai
from .audio import RATE, to_wav

logger = logging.getLogger(__name__)

TRANSPORTS = {'openai': openai.transcribe, 'gemini': gemini.transcribe}
# A failure is shown once in this many seconds; the rest only logged.
PROBLEM_EVERY_S = 600
_last_problem = {}


# Whether `spec` is a model Koli can send a recording to now: an upload model
# of a provider it speaks. A live-stream model or an unknown one is not.
def usable(spec: str) -> bool:
    parsed = routing.parse(spec)
    if not parsed or parsed[0] not in TRANSPORTS:
        return False
    entry = prices.entry(spec)
    # A model you typed that the list doesn't know is tried as an upload
    # model; a listed one must be one (not text-only, not live).
    return entry is None or entry.get('transcribe') == 'upload'


class OnlineEngine:
    def __init__(self, spec: str, local_provider: Callable[[], object], feature: str = 'dictation',
                 timeout: float = 60):
        self.spec = spec
        self.provider_id, self.model = routing.parse(spec)
        self.local_provider = local_provider
        self.feature = feature
        self.timeout = timeout
        self.last_info = None

    # The local engine's settings carry over: language, prompt, words.
    def __getattr__(self, name):
        if name in ('language', 'initial_prompt', 'hotwords', 'vad_manager', 'task', 'beam_size'):
            return getattr(self.local_provider(), name, None)
        raise AttributeError(name)

    def _prompt(self, local) -> str:
        words = [w for w in (getattr(local, 'hotwords', None) or []) if w]
        prompt = getattr(local, 'initial_prompt', None) or ''
        return ' '.join(p for p in (prompt, ', '.join(words)) if p).strip()

    def transcribe_audio(self, audio_data: Optional[np.ndarray], language: Optional[str] = None,
                         use_prompt: bool = True, beam_size: Optional[int] = None, task: Optional[str] = None,
                         quiet: bool = False, hotwords: Optional[str] = None,
                         pieces: Optional[list] = None) -> Optional[str]:
        local = self.local_provider()
        # Quick checks (wake word, commands) never go online.
        if quiet or (task or getattr(local, 'task', 'transcribe')) == 'translate':
            return local.transcribe_audio(audio_data, language=language, use_prompt=use_prompt, beam_size=beam_size,
                                          task=task, quiet=quiet, hotwords=hotwords, pieces=pieces)
        if audio_data is None or len(audio_data) == 0:
            return None
        audio = np.asarray(audio_data, dtype=np.float32).flatten()
        vad = getattr(local, 'vad_manager', None)
        if vad is not None and vad.is_available() and not vad.check_audio_for_speech(audio):
            print("   ✗ No speech detected, skipping transcription")
            return None

        forced = getattr(local, 'language', None) if language is None else (None if language == 'auto' else language)
        seconds = len(audio) / RATE
        started = time.monotonic()
        try:
            text, info = TRANSPORTS[self.provider_id](
                PROVIDERS[self.provider_id].key(), self.model, to_wav(audio), language=forced or '',
                prompt=self._prompt(local) if use_prompt else '', timeout=max(self.timeout, seconds))
        except Exception as e:
            return self._fall_back(local, audio, language, use_prompt, e)

        elapsed = time.monotonic() - started
        rec = ledger.record(self.feature, self.spec, seconds=info.get('billed_seconds') or seconds,
                            tokens_in=info['tokens_in'], tokens_out=info['tokens_out'],
                            audio_tokens=info['audio_tokens'])
        cost = rec.get('cost')
        print(f"   ✓ {self.spec} transcribed in {elapsed:.1f} s" + (f" · ${cost:.4f}" if cost is not None else ''))
        logger.info(f"Online {self.feature}: {self.spec} in {elapsed:.2f}s, {seconds:.1f} s audio, "
                    f"{info['audio_tokens']} audio + {info['tokens_in']} text → {info['tokens_out']} tokens, "
                    f"cost {cost if cost is not None else 'unknown'}")
        if pieces is not None:
            pieces.extend(info['segments'])
        self.last_info = {
            'language': info['language'] or forced or '',
            'language_probability': None,
            'audio_seconds': round(seconds, 2), 'seconds': round(elapsed, 2),
            'model': self.spec, 'backend': 'online', 'device': 'cloud',
            'segments': info['segments'], 'cost': cost,
        }
        return text or None

    # The provider failed: say why (once in a while), and use the local model.
    def _fall_back(self, local, audio, language, use_prompt, error: Exception) -> Optional[str]:
        reason = describe_error(self.provider_id, error)
        logger.warning(f"Online {self.feature} with {self.spec} failed, using local Whisper: {reason}")
        now = time.monotonic()
        if now - _last_problem.get(self.spec, -PROBLEM_EVERY_S) >= PROBLEM_EVERY_S:
            _last_problem[self.spec] = now
            try:
                from ..ui.events import report_problem
                report_problem(f"{self.spec} didn't answer, so local Whisper transcribed instead: {reason}")
            except Exception:
                pass
        text = local.transcribe_audio(audio, language=language, use_prompt=use_prompt)
        # The caller records the local call (record_local), as for any local one.
        info = dict(getattr(local, 'last_info', None) or {})
        info['online_error'] = reason
        self.last_info = info
        return text

    def is_loading(self) -> bool:
        return False

    def cancel(self):
        local = self.local_provider()
        if hasattr(local, 'cancel'):
            local.cancel()


# A local Whisper transcription, for the ledger: free, but a meeting still
# says which model wrote it.
def record_local(feature: str, info: dict, meeting_id: Optional[str] = None) -> None:
    if not info or info.get('backend') == 'online':
        return
    ledger.record(feature, f"local/{info.get('model') or 'whisper'}", seconds=info.get('audio_seconds') or 0,
                  meeting_id=meeting_id, cost=0.0)
