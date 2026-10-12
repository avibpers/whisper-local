# meeting/transcriber.py
# Turns meeting chunks into transcript turns on one background worker. The
# Whisper engine is shared with dictation, so the worker waits whenever a
# dictation is being recorded or transcribed; the queue just grows and the
# panel shows how far behind it is. Sentences Whisper made up are dropped.
# Mic chunks are "You"; a loopback chunk is split by voice (diarize.py) and
# each voice gets a speaker from the tracker, one turn per voice.

import logging
import queue
import threading
import time
from typing import Callable, List, Optional

import numpy as np

from ..dictation_cleanup import is_prompt_echo, unsure_piece
from .diarize import parts_by_voice, voice_audio, voice_seconds
from .speakers import SpeakerTracker

logger = logging.getLogger(__name__)

RATE = 16000
# A clip this short that Whisper wasn't clearly sure of is dropped whole.
# From a real Hebrew Meet: made-up clips under 2 s ("Bet.", "PR in Chile.",
# "We'll see you next time.") scored -0.45 to -1.12 log-prob per word, while
# real short speech ("כל פעם לפני שעולה גרסה.", 1.95 s) scored -0.09.
SHORT_CLIP_S = 2.0
SHORT_CLIP_MIN_LOGPROB = -0.35


# The pieces (Whisper segments) of one clip worth keeping. Each is checked
# the way auto-dictation checks a clip nobody chose to record (silence,
# word-by-word certainty, repetition; mic garbage scored -1.5 to -4.5), and
# a short clip must be clearly sure. Without piece numbers (another
# backend), the whole text is one piece.
def believable_pieces(text: str, info: dict, seconds: float) -> List[dict]:
    pieces = [p for p in (info or {}).get('segments') or [] if (p.get('text') or '').strip()]
    if not pieces:
        return [{'text': text, 'start': 0.0, 'end': seconds}]
    if seconds < SHORT_CLIP_S and any(p.get('avg_logprob', 0) < SHORT_CLIP_MIN_LOGPROB for p in pieces):
        return []
    return [p for p in pieces if not unsure_piece(p)]


class MeetingTranscriber:
    def __init__(self, engine_provider: Callable, is_busy: Callable[[], bool], tracker: SpeakerTracker,
                 language_provider: Callable[[], str], on_turn: Callable[[dict], None],
                 on_speaker: Callable[[dict], None], on_pending: Callable[[Optional[str]], None],
                 on_behind: Callable[[float], None], poll_s: float = 0.1):
        self.engine_provider = engine_provider
        self.is_busy = is_busy
        self.tracker = tracker
        self.language_provider = language_provider
        self.on_turn = on_turn
        self.on_speaker = on_speaker
        self.on_pending = on_pending
        self.on_behind = on_behind
        self.poll_s = poll_s
        # Finds the voices in a chunk: audio -> [(start_s, end_s, voice)];
        # set once its model is loaded, until then a chunk is one voice.
        self.split: Optional[Callable[[np.ndarray], list]] = None
        self._queue = queue.Queue()
        self._queued_s = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.working = False

    def queued_seconds(self) -> float:
        with self._lock:
            return self._queued_s

    def submit(self, chunk: dict):
        with self._lock:
            self._queued_s += float(chunk['t1'] - chunk['t0'])
            behind = self._queued_s
        self._queue.put(chunk)
        self.on_behind(round(behind, 1))

    # Drop what is queued (a meeting ended or was cancelled).
    def clear(self):
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            self._queued_s = 0.0
        self.on_behind(0.0)

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name='meeting-transcriber')
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._queue.put(None)
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(5.0)

    def _run(self):
        while not self._stop.is_set():
            chunk = self._queue.get()
            if chunk is None:
                break
            while self.is_busy() and not self._stop.is_set():
                time.sleep(self.poll_s)
            if self._stop.is_set():
                break
            self.working = True
            try:
                self._transcribe(chunk)
            except Exception as e:
                logger.error(f"Meeting chunk failed: {e}")
            finally:
                with self._lock:
                    self._queued_s = max(0.0, self._queued_s - float(chunk['t1'] - chunk['t0']))
                    behind = self._queued_s
                self.on_behind(round(behind, 1))
                self.on_pending(None)
                self.working = False

    def _transcribe(self, chunk: dict):
        engine = self.engine_provider()
        if engine is None:
            return
        self.on_pending(chunk['stream'])
        # No dictation prompt: it's a style hint for the app in front, and on a
        # short or quiet clip Whisper wrote it back as the turn ("Friendly tone.").
        text = engine.transcribe_audio(chunk['audio'], language=self.language_provider() or 'auto', use_prompt=False)
        if not text or is_prompt_echo(text, getattr(engine, 'initial_prompt', None)):
            return
        info = getattr(engine, 'last_info', None) or {}
        lang = info.get('language') or ''
        seconds = len(chunk['audio']) / RATE
        pieces = believable_pieces(text, info, seconds)
        if not pieces:
            logger.info(f"Dropped a {seconds:.1f} s {chunk['stream']} clip Whisper wasn't sure of")
            return
        base = {'stream': chunk['stream'], 'lang': lang, 'meeting_id': chunk.get('meeting_id')}
        if chunk['stream'] == 'mic':
            self.on_turn({**base, 't0': chunk['t0'], 't1': chunk['t1'], 'speaker': 'you',
                          'text': ''.join(p['text'] for p in pieces).strip()})
            return
        voices = self._voices_in(chunk['audio'])
        if len({v for _, _, v in voices}) < 2:
            speaker = self._label(chunk['audio'], chunk.get('speech_s'), ())
            self.on_turn({**base, 't0': chunk['t0'], 't1': chunk['t1'], 'speaker': speaker,
                          'text': ''.join(p['text'] for p in pieces).strip()})
            return
        # Several voices: one turn per run of one voice, each voice labelled
        # once from all its audio here, and never as another voice of this chunk.
        speakers, previous = {}, None
        for part in parts_by_voice(pieces, voices):
            voice = part['voice']
            if voice not in speakers:
                speaker = self._label(voice_audio(chunk['audio'], voices, voice), voice_seconds(voices, voice),
                                      [s for s in speakers.values() if s != SpeakerTracker.UNKNOWN])
                # Too little of this voice for a voiceprint: it stays with the voice before it.
                if speaker == SpeakerTracker.UNKNOWN and self.tracker.embed is not None and previous:
                    speaker = previous
                speakers[voice] = speaker
            previous = speakers[voice]
            self.on_turn({**base, 't0': chunk['t0'] + part['start'], 't1': chunk['t0'] + part['end'],
                          'speaker': speakers[voice], 'text': part['text']})

    # The voices the splitter hears in a chunk; none when it isn't loaded or fails.
    def _voices_in(self, audio) -> list:
        if self.split is None:
            return []
        try:
            return self.split(audio)
        except Exception as e:
            logger.debug(f"Splitting a chunk by voice failed: {e}")
            return []

    def _label(self, audio, speech_s, exclude) -> str:
        speaker, entry = self.tracker.label(audio, speech_s=speech_s, exclude=exclude)
        if entry is not None:
            self.on_speaker(entry)
        return speaker
