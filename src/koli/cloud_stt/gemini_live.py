# cloud_stt/gemini_live.py
# Gemini's live transcription (gemini-3.5-transcribe-live) for the bubble's
# Cloud engine, with the same face as openai_live.OpenAILive: microphone audio
# goes out over a WebSocket as you speak, words come back as you say them.
# Koli marks the turns itself (activityStart / activityEnd at its phrase
# pauses); Gemini answers each turn with running interim text and then one
# final text. Checked against the real API on 2026-10-29: a turn started
# before the previous one's final arrived is ignored, so the next turn opens
# only after that final, and its audio waits until then.

import base64
import json
import logging
import queue
import threading
import time
from typing import Callable, Optional

import numpy as np

from .ws import WebSocket

logger = logging.getLogger(__name__)

# The key goes in a header, never the URL, so no error text can carry it.
URL = ('wss://generativelanguage.googleapis.com/ws/'
       'google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent')
CHUNK_BYTES = 3200          # 100 ms of 16 kHz PCM16 per message
MIN_TURN_SAMPLES = 1600     # 100 ms at 16 kHz: shorter turns are not sent


def _pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767).astype('<i2').tobytes()


class GeminiLive:
    def __init__(self, key: str, model: str, language,
                 on_interim: Callable[[str], None], on_final: Callable[[str], None],
                 on_error: Callable[[str], None], connect=WebSocket.connect, retry_delay: float = 0.25):
        self._key, self._model = key, model
        self._languages = [language] if isinstance(language, str) else list(language or [])
        self._languages = [code for code in self._languages if code]
        self._on_interim, self._on_final, self._on_error = on_interim, on_final, on_error
        self._connect, self._retry_delay = connect, retry_delay
        self._outbox: 'queue.Queue' = queue.Queue()
        self._socket = None
        self._lock = threading.Lock()
        # Each turn: its 16 kHz audio, whether it was ended (a phrase pause),
        # and whether its final text came back. Turns go to Gemini one at a time.
        self._turns = [{'audio': [], 'ended': False, 'done': False}]
        self._open = -1               # the turn Gemini is hearing now (-1: none yet)
        self._sent = 0                # how much of the open turn's audio went out
        self._ready = False           # the session is set up
        self._stopping = False
        self.seconds_sent = 0.0
        self.transcribed_any = False
        self.failed = False
        self._fail_reported = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name='gemini-live').start()

    def _setup(self) -> dict:
        transcription = {'languageCodes': self._languages} if self._languages else {}
        return {'setup': {'model': f'models/{self._model}',
                          'generationConfig': {'responseModalities': ['TEXT']},
                          'inputAudioTranscription': transcription,
                          'realtimeInputConfig': {'automaticActivityDetection': {'disabled': True}}}}

    def _run(self) -> None:
        for attempt in range(2):
            if self._stopping:
                return
            try:
                sock = self._connect(URL, {'x-goog-api-key': self._key}, timeout=10)
                break
            except Exception as e:
                if attempt == 0:
                    time.sleep(self._retry_delay)
                    continue
                self._fail(f"Couldn't reach Gemini: {e}")
                return
        with self._lock:
            self._socket = sock
        sock.send_text(json.dumps(self._setup()))
        sender = threading.Thread(target=self._send_loop, args=(sock,), daemon=True, name='gemini-live-send')
        sender.start()
        self._sender = sender
        self._read_loop(sock)

    def _send_loop(self, sock) -> None:
        while True:
            message = self._outbox.get()
            if message is None:
                return
            try:
                sock.send_text(json.dumps(message))
            except Exception as e:
                self._fail(f"Lost the connection to Gemini: {e}")
                return

    def _read_loop(self, sock) -> None:
        while True:
            item = sock.recv()
            if item is None:
                if not self._stopping:
                    self._fail('Gemini closed the connection')
                return
            _kind, payload = item
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode('utf-8', errors='replace')
            try:
                self._handle(json.loads(payload))
            except Exception as e:
                logger.debug(f"Gemini event not understood: {e}")

    def _fail(self, message: str) -> None:
        with self._lock:
            if self._fail_reported:
                return
            self._fail_reported = True
        if self._key:
            message = message.replace(self._key, '***')
        logger.warning(message)
        try:
            self._on_error(message)
        finally:
            self.failed = True

    # ── Audio in ─────────────────────────────────────────────────────────────

    # 16 kHz float audio from the microphone; safe from the audio thread.
    def feed(self, mono16k: np.ndarray) -> None:
        if self._stopping or mono16k is None or not len(mono16k):
            return
        with self._lock:
            self._turns[-1]['audio'].append(np.asarray(mono16k, dtype=np.float32).copy())
            self.seconds_sent += len(mono16k) / 16000.0
        self._pump()

    # A phrase pause: the turn so far is complete.
    def commit(self) -> None:
        with self._lock:
            turn = self._turns[-1]
            if sum(len(a) for a in turn['audio']) < MIN_TURN_SAMPLES:
                turn['audio'] = []          # a click or a breath: not worth a turn
                return
            turn['ended'] = True
            self._turns.append({'audio': [], 'ended': False, 'done': False})
        self._pump()

    # Sends what can go now: opens the next turn once the previous one's final
    # is back, streams the open turn's audio, and ends it when it was committed.
    def _pump(self) -> None:
        out = []
        with self._lock:
            if not self._ready or self._stopping:
                return
            if self._open < 0 or (self._turns[self._open]['done'] and self._open + 1 < len(self._turns)):
                nxt = self._open + 1
                if not self._turns[nxt]['audio'] and not self._turns[nxt]['ended']:
                    return               # nothing said yet in the next turn
                self._open, self._sent = nxt, 0
                out.append({'realtimeInput': {'activityStart': {}}})
            turn = self._turns[self._open]
            if turn['done'] or turn.get('closed'):
                return
            pending = turn['audio'][self._sent:]
            self._sent = len(turn['audio'])
            if pending:
                pcm = _pcm16(np.concatenate(pending))
                for i in range(0, len(pcm), CHUNK_BYTES):
                    out.append({'realtimeInput': {'audio': {'data': base64.b64encode(pcm[i:i + CHUNK_BYTES]).decode(),
                                                            'mimeType': 'audio/pcm;rate=16000'}}})
            if turn['ended']:
                turn['closed'] = True
                out.append({'realtimeInput': {'activityEnd': {}}})
        for message in out:
            self._outbox.put(message)

    # The recording ended: end the last turn, wait for every turn's text (up
    # to `timeout`), then close.
    def finish(self, timeout: float = 5.0) -> None:
        self.commit()
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.failed:
            with self._lock:
                if all(t['done'] for t in self._turns if t['ended']):
                    break
            time.sleep(0.02)
        self._close()

    def cancel(self) -> None:
        self._close()

    def _close(self) -> None:
        self._stopping = True
        self._outbox.put(None)
        sender = getattr(self, '_sender', None)
        if sender is not None and sender is not threading.current_thread():
            sender.join(timeout=2.0)
        with self._lock:
            sock = self._socket
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    # ── Text out ─────────────────────────────────────────────────────────────

    def _handle(self, event: dict) -> None:
        if 'setupComplete' in event:
            with self._lock:
                self._ready = True
            self._pump()
            return
        content = event.get('serverContent') or {}
        interim = (content.get('interimInputTranscription') or {}).get('text')
        if interim:
            self._on_interim(interim.strip())
        final = content.get('inputTranscription')
        if final is not None:
            text = (final.get('text') or '').strip()
            with self._lock:
                if 0 <= self._open < len(self._turns):
                    self._turns[self._open]['done'] = True
            if text:
                self.transcribed_any = True
                self._on_final(text)
            self._pump()                 # the next turn may open now
        elif content.get('generationComplete') or content.get('turnComplete'):
            # A turn with nothing said ends without a transcription.
            with self._lock:
                closed = 0 <= self._open < len(self._turns) and self._turns[self._open].get('closed')
                if closed:
                    self._turns[self._open]['done'] = True
            if closed:
                self._pump()
        error = event.get('error')
        if error:
            self._fail(f"Gemini: {error.get('message') if isinstance(error, dict) else error}")

    # The audio whose text never came back, for Whisper after a dropped connection.
    def unsent_audio(self) -> Optional[np.ndarray]:
        with self._lock:
            parts = [a for t in self._turns if not t['done'] for a in t['audio']]
        return np.concatenate(parts) if parts else None
