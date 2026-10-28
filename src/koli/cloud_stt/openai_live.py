# cloud_stt/openai_live.py
# OpenAI's live transcription for the bubble's Cloud engine: microphone audio
# goes out over a WebSocket as you speak, words come back as you say them.
# Koli decides where a turn ends (its own phrase pauses) and commits it;
# OpenAI then sends that turn's final text. Protocol checked against
# developers.openai.com (realtime transcription) on 2026-10-28.

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

URL = 'wss://api.openai.com/v1/realtime?intent=transcription'
SEND_RATE = 24000          # the realtime API takes 24 kHz PCM16
MIN_APPEND_BYTES = 4800    # 100 ms at 24 kHz: fewer, larger messages
MIN_TURN_SAMPLES = 1600    # 100 ms at 16 kHz: shorter turns are not committed (OpenAI refuses them)


def _resample_to_24k(mono16k: np.ndarray) -> np.ndarray:
    try:
        import soxr
        return soxr.resample(mono16k.astype(np.float32, copy=False), 16000, SEND_RATE)
    except ImportError:
        n = int(round(len(mono16k) * SEND_RATE / 16000))
        return np.interp(np.linspace(0, len(mono16k) - 1, n), np.arange(len(mono16k)), mono16k).astype(np.float32)


def _pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767).astype('<i2').tobytes()


class OpenAILive:
    def __init__(self, key: str, model: str, language: str,
                 on_interim: Callable[[str], None], on_final: Callable[[str], None],
                 on_error: Callable[[str], None], connect=WebSocket.connect, retry_delay: float = 0.25):
        self._key, self._model, self._language = key, model, language
        self._on_interim, self._on_final, self._on_error = on_interim, on_final, on_error
        self._connect, self._retry_delay = connect, retry_delay
        self._outbox: 'queue.Queue' = queue.Queue()
        self._socket = None
        self._lock = threading.Lock()
        self._pending_bytes = b''
        # Each turn: the 16 kHz audio fed for it, and whether its text came back.
        self._turns = [{'audio': [], 'done': False}]
        self._committed = 0            # turns committed so far
        self._item_turn = {}           # item_id -> turn index, in commit order
        self._deltas = {}              # item_id -> text so far
        self._ready = {}               # turn index -> final text waiting for earlier turns
        self._next_final = 0           # the next turn whose final may be delivered
        self._stopping = False
        self.seconds_sent = 0.0
        self.transcribed_any = False
        self.failed = False
        self._fail_reported = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name='openai-live').start()

    def _session_update(self) -> dict:
        transcription = {'model': self._model}
        if self._language:
            transcription['languages'] = [self._language]
        transcription['delay'] = 'low'
        return {'type': 'session.update', 'session': {'type': 'transcription', 'audio': {'input': {
            'format': {'type': 'audio/pcm', 'rate': SEND_RATE},
            'transcription': transcription, 'turn_detection': None}}}}

    # Connect (one retry after a short wait if nothing was transcribed yet),
    # then send and read until the session ends.
    def _run(self) -> None:
        for attempt in range(2):
            if self._stopping:
                return
            try:
                sock = self._connect(URL, {'Authorization': f'Bearer {self._key}'}, timeout=10)
                break
            except Exception as e:
                if attempt == 0 and not self.transcribed_any:
                    time.sleep(self._retry_delay)
                    continue
                self._fail(f"Couldn't reach OpenAI: {e}")
                return
        with self._lock:
            self._socket = sock
        sock.send_text(json.dumps(self._session_update()))
        sender = threading.Thread(target=self._send_loop, args=(sock,), daemon=True, name='openai-live-send')
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
                self._fail(f"Lost the connection to OpenAI: {e}")
                return

    def _read_loop(self, sock) -> None:
        while True:
            item = sock.recv()
            if item is None:
                if not self._stopping:
                    self._fail('OpenAI closed the connection')
                return
            kind, payload = item
            if kind != 'text':
                continue
            try:
                self._handle(json.loads(payload))
            except Exception as e:
                logger.debug(f"OpenAI event not understood: {e}")

    # Said once; `failed` turns true after the flow has been told, so anyone
    # who sees it can rely on the error having been handled.
    def _fail(self, message: str) -> None:
        with self._lock:
            if self._fail_reported:
                return
            self._fail_reported = True
        logger.warning(message)
        try:
            self._on_error(message)
        finally:
            self.failed = True

    # ── Audio in ─────────────────────────────────────────────────────────────

    # 16 kHz float audio from the microphone. Safe to call from the audio
    # thread: it only queues.
    def feed(self, mono16k: np.ndarray) -> None:
        if self._stopping or mono16k is None or not len(mono16k):
            return
        with self._lock:
            self._turns[-1]['audio'].append(np.asarray(mono16k, dtype=np.float32).copy())
            self.seconds_sent += len(mono16k) / 16000.0
            self._pending_bytes += _pcm16(_resample_to_24k(np.asarray(mono16k, dtype=np.float32)))
            if len(self._pending_bytes) < MIN_APPEND_BYTES:
                return
            chunk, self._pending_bytes = self._pending_bytes, b''
        self._outbox.put({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(chunk).decode()})

    def _flush(self) -> None:
        with self._lock:
            chunk, self._pending_bytes = self._pending_bytes, b''
        if chunk:
            self._outbox.put({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(chunk).decode()})

    # A phrase pause: the turn so far is complete, OpenAI may finish it.
    def commit(self) -> None:
        with self._lock:
            if sum(len(a) for a in self._turns[-1]['audio']) < MIN_TURN_SAMPLES:
                # A click or a breath after the last pause: not worth a turn.
                self._turns[-1]['audio'] = []
                self._pending_bytes = b''
                return
        self._flush()
        with self._lock:
            self._committed += 1
            self._turns.append({'audio': [], 'done': False})
        self._outbox.put({'type': 'input_audio_buffer.commit'})

    # The recording ended: commit the last turn, wait for every turn's text
    # (up to `timeout`), then close.
    def finish(self, timeout: float = 5.0) -> None:
        self.commit()
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.failed:
            with self._lock:
                if self._next_final >= self._committed:
                    break
            time.sleep(0.02)
        self._close()

    def cancel(self) -> None:
        self._close()

    # Everything already queued is sent before the socket closes.
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
        kind = event.get('type', '')
        item = event.get('item_id')
        if kind == 'input_audio_buffer.committed' and item:
            with self._lock:
                self._item_turn.setdefault(item, len(self._item_turn))
        elif kind == 'conversation.item.input_audio_transcription.delta' and item:
            with self._lock:
                self._item_turn.setdefault(item, len(self._item_turn))
                self._deltas[item] = self._deltas.get(item, '') + (event.get('delta') or '')
                interim = ' '.join(t for i, t in self._deltas.items() if t).strip()
            self._on_interim(interim)
        elif kind == 'conversation.item.input_audio_transcription.completed' and item:
            finals = []
            with self._lock:
                turn = self._item_turn.setdefault(item, len(self._item_turn))
                self._deltas.pop(item, None)
                self._ready[turn] = (event.get('transcript') or '').strip()
                if turn < len(self._turns):
                    self._turns[turn]['done'] = True
                while self._next_final in self._ready:
                    finals.append(self._ready.pop(self._next_final))
                    self._next_final += 1
            for text in finals:
                if text:
                    self.transcribed_any = True
                    self._on_final(text)
        elif kind == 'error':
            error = event.get('error') or {}
            if error.get('code') == 'input_audio_buffer_commit_empty':
                logger.debug("OpenAI: an empty turn was not committed")
                return
            self._fail(f"OpenAI: {error.get('message') or error or 'error'}")

    # The audio whose text never came back (the current turn included), for
    # Whisper to transcribe after a dropped connection.
    def unsent_audio(self) -> Optional[np.ndarray]:
        with self._lock:
            parts = [a for t in self._turns if not t['done'] for a in t['audio']]
        return np.concatenate(parts) if parts else None
