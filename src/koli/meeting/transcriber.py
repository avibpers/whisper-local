# meeting/transcriber.py
# Turns meeting chunks into transcript turns on one background worker. The
# Whisper engine is shared with dictation, so the worker waits whenever a
# dictation is being recorded or transcribed; the queue just grows and the
# panel shows how far behind it is. Mic chunks are "You"; loopback chunks get
# a speaker from the tracker.

import logging
import queue
import threading
import time
from typing import Callable, Optional

from .speakers import SpeakerTracker

logger = logging.getLogger(__name__)


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
        text = engine.transcribe_audio(chunk['audio'], language=self.language_provider() or 'auto')
        if not text:
            return
        lang = (getattr(engine, 'last_info', None) or {}).get('language') or ''
        if chunk['stream'] == 'mic':
            speaker = 'you'
        else:
            speaker, entry = self.tracker.label(chunk['audio'], speech_s=chunk.get('speech_s'))
            if entry is not None:
                self.on_speaker(entry)
        self.on_turn({'t0': chunk['t0'], 't1': chunk['t1'], 'stream': chunk['stream'],
                      'speaker': speaker, 'text': text, 'lang': lang, 'meeting_id': chunk.get('meeting_id')})
