# meeting/capture.py
# The two audio streams of a meeting and how they become chunks. The loopback
# stream reads what the speakers play (soundcard, WASAPI loopback); the mic
# tap hooks the recorder's always-open microphone stream. Each goes through a
# Chunker that uses ten-vad to cut at pauses, so Whisper gets short pieces of
# one speaker at a time, with wall-clock times for the transcript.

import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

from ..voice_activity_detection import VAD_CHUNK_SIZE, convert_audio_for_ten_vad

logger = logging.getLogger(__name__)

RATE = 16000
FRAME = VAD_CHUNK_SIZE              # 256 samples, 16 ms
FRAME_S = FRAME / RATE
SPEECH_THRESHOLD = 0.5
TRAIL_S = 0.3                        # silence kept after the last speech


# ── Speech probability ───────────────────────────────────────────────────────

# A function frame -> probability of speech, from ten-vad when it is
# installed, else a loudness guess so a meeting still cuts at pauses.
def speech_prob_function() -> Callable[[np.ndarray], float]:
    try:
        from ten_vad import TenVad
        vad = TenVad()
        lock = threading.Lock()

        def prob(frame: np.ndarray) -> float:
            with lock:
                p, _ = vad.process(convert_audio_for_ten_vad(frame))
            return float(p)
        return prob
    except Exception as e:
        logger.warning(f"ten-vad unavailable for meetings ({e}); using loudness")

        def loud(frame: np.ndarray) -> float:
            return 0.9 if float(np.sqrt(np.mean(frame.astype(np.float32) ** 2))) > 0.01 else 0.0
        return loud


# ── Chunker ──────────────────────────────────────────────────────────────────

class Chunker:
    def __init__(self, stream: str, speech_prob: Callable[[np.ndarray], float], on_chunk: Callable[[dict], None],
                 on_speech: Optional[Callable[[float], None]] = None, pause_s: float = 0.6, max_s: float = 15.0,
                 min_speech_s: float = 0.3, rate: int = RATE):
        self.stream = stream
        self.speech_prob = speech_prob
        self.on_chunk = on_chunk
        self.on_speech = on_speech
        self.pause_frames = max(1, int(round(pause_s / FRAME_S)))
        self.max_frames = max(self.pause_frames + 1, int(round(max_s / FRAME_S)))
        self.min_speech_frames = max(1, int(round(min_speech_s / FRAME_S)))
        self.rate = rate
        self._lock = threading.Lock()
        self._frames = []            # frames of the open chunk
        self._speech = []            # one bool per frame
        self._rest = np.zeros(0, dtype=np.float32)   # samples not yet a full frame
        self._t_end = 0.0            # wall clock at the end of the last frame

    # `samples` is mono float32 at `rate`; `t_end` is the wall clock of its
    # last sample. The callbacks run after the lock is released, so one that
    # takes another lock (or re-enters the chunker) can never deadlock the
    # audio thread.
    def feed(self, samples: np.ndarray, t_end: float):
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        emitted, spoken = [], 0
        with self._lock:
            data = np.concatenate([self._rest, samples]) if len(self._rest) else samples
            whole = (len(data) // FRAME) * FRAME
            frames, self._rest = data[:whole], data[whole:]
            n = whole // FRAME
            for i in range(n):
                frame = frames[i * FRAME:(i + 1) * FRAME]
                speech = self.speech_prob(frame) >= SPEECH_THRESHOLD
                # This frame ended (n-1-i) frames plus the leftover samples before t_end.
                self._t_end = t_end - (n - 1 - i) * FRAME_S - len(self._rest) / self.rate
                chunk = self._push(frame, speech)
                if chunk is not None:
                    emitted.append(chunk)
                spoken += speech
        for chunk in emitted:
            self.on_chunk(chunk)
        if spoken and self.on_speech:
            self.on_speech(spoken * FRAME_S)

    # Adds a frame to the open chunk; returns the chunk when it closed.
    def _push(self, frame: np.ndarray, speech: bool):
        if not self._frames and not speech:
            return None              # silence before any speech is not kept
        self._frames.append(frame)
        self._speech.append(speech)
        trailing = 0
        for s in reversed(self._speech):
            if s:
                break
            trailing += 1
        if trailing >= self.pause_frames or len(self._frames) >= self.max_frames:
            return self._take_locked()
        return None

    def flush(self):
        with self._lock:
            chunk = self._take_locked()
        if chunk is not None:
            self.on_chunk(chunk)

    # The open chunk as a dict, or None when it holds too little speech.
    def _take_locked(self):
        if not self._frames:
            return None
        frames, speech = self._frames, self._speech
        self._frames, self._speech = [], []
        voiced = sum(speech)
        if voiced < self.min_speech_frames:
            return None
        # Keep a little silence after the last speech, not the whole pause.
        last = max(i for i, s in enumerate(speech) if s)
        keep = min(len(frames), last + 1 + int(round(TRAIL_S / FRAME_S)))
        dropped = len(frames) - keep
        t1 = self._t_end - dropped * FRAME_S
        t0 = t1 - keep * FRAME_S
        return {'audio': np.concatenate(frames[:keep]), 'stream': self.stream,
                't0': t0, 't1': t1, 'speech_s': voiced * FRAME_S}


# ── Loopback ─────────────────────────────────────────────────────────────────

def _open_default_loopback(rate: int, frames: int):
    import soundcard as sc
    speaker = sc.default_speaker()
    mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
    return mic.recorder(samplerate=rate, channels=1, blocksize=frames)


# Reads what the speakers play, 100 ms at a time, on its own thread. A device
# that disappears is reopened a few times; after that on_error gets a message
# and the thread ends (the meeting carries on with the microphone).
class LoopbackStream:
    def __init__(self, on_block: Callable[[np.ndarray, float], None], on_error: Callable[[str], None],
                 block_s: float = 0.1, rate: int = RATE, retries: int = 3, open_fn=None, retry_wait: float = 1.0):
        self.on_block = on_block
        self.on_error = on_error
        self.rate = rate
        self.frames = int(block_s * rate)
        self.retries = retries
        self.retry_wait = retry_wait
        self._open = open_fn or (lambda: _open_default_loopback(self.rate, self.frames))
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name='meeting-loopback')
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)

    def _run(self):
        failures = 0
        while not self._stop.is_set():
            try:
                with self._open() as rec:
                    failures = 0
                    while not self._stop.is_set():
                        data = rec.record(numframes=self.frames)
                        block = np.asarray(data, dtype=np.float32)
                        if block.ndim > 1:
                            block = block[:, 0]
                        self.on_block(block, time.time())
                return
            except Exception as e:
                failures += 1
                if failures > self.retries:
                    logger.error(f"System audio capture gave up: {e}")
                    self.on_error(f"System audio capture stopped: {e}")
                    return
                logger.warning(f"System audio capture failed ({e}); retry {failures}/{self.retries}")
                if self._stop.wait(self.retry_wait):
                    return


# ── Microphone tap ───────────────────────────────────────────────────────────

# The microphone as the recorder already captures it. `allow()` says whether
# a block may go into the meeting: not while you dictate, not while excluded.
class MicTap:
    def __init__(self, recorder_provider: Callable, on_block: Callable[[np.ndarray, float], None],
                 allow: Callable[[], bool]):
        self._recorder = recorder_provider
        self.on_block = on_block
        self.allow = allow
        self._attached = None

    def _tap(self, audio: np.ndarray, when: float):
        if self.allow():
            self.on_block(audio, when)

    def start(self):
        rec = self._recorder()
        if rec is None or self._attached is rec:
            return
        self.stop()
        rec.add_tap(self._tap)
        self._attached = rec

    def stop(self):
        if self._attached is not None:
            try:
                self._attached.remove_tap(self._tap)
            except Exception:
                pass
            self._attached = None
