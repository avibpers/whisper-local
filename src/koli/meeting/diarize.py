# meeting/diarize.py
# Where the voice changes inside one chunk of the call. A 15-second chunk can
# hold three people; labelled as one, their voices blurred into one speaker
# (a 4-person Meet came out as "You" and "Speaker 1"). pyannote's
# segmentation model, through sherpa-onnx, marks who speaks when inside the
# chunk (local voices 0, 1, 2); the tracker then decides who each one is.
# The model is optional: without it a chunk stays one voice, as before.
# sherpa-onnx's diarizer holds Python's lock for its whole run (5-8 s for a
# 15 s chunk, measured 2026-11-04). In the app that froze every thread, the
# keyboard and mouse hooks too, and with them the whole PC's input. So it runs
# in a low-priority process of its own: `python -m koli.meeting.diarize`.

import json
import logging
import os
import pathlib
import struct
import subprocess
import sys
import threading
import time
from typing import Callable, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

RATE = 16000
CLUSTER_THRESHOLD = 0.5   # sherpa-onnx's own default for telling local voices apart
MIN_ON_S = 0.3            # shorter bursts of a voice are folded into their neighbours
MIN_OFF_S = 0.5           # shorter pauses inside one voice don't split it
MIN_PART_S = 1.0          # a part shorter than this is never a turn of its own
SPLIT_TIMEOUT_S = 120.0   # a chunk taking longer than this means the worker hung (it also loads the model)
MAX_WORKER_FAILURES = 3   # after this many failures in a row, chunks stay one voice
WORKER_IDLE_EXIT_S = 600.0  # an idle worker leaves and gives its memory back; the next chunk starts another


# ── Splitting a chunk ────────────────────────────────────────────────────────

# A function from a chunk's audio (mono float32, 16 kHz) to the voices in it:
# [(start_s, end_s, local_voice)], sorted by start. The worker process starts
# at the first chunk, so a meeting that never splits (Meet's captions) has none.
def make_splitter(segmentation_model: str, embedding_model: str,
                  num_threads: int = 1) -> Callable[[np.ndarray], List[tuple]]:
    return VoiceSplitter([sys.executable, '-m', 'koli.meeting.diarize',
                          segmentation_model, embedding_model, str(num_threads)])


class VoiceSplitter:
    """Asks the worker process about one chunk at a time. Only the waiting
    happens in the app, and waiting leaves Python's lock free for everyone."""

    def __init__(self, command: List[str]):
        self.command = command
        self._lock = threading.Lock()
        self._process = None
        self._failures = 0

    def __call__(self, audio: np.ndarray) -> List[tuple]:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        with self._lock:
            if self._failures >= MAX_WORKER_FAILURES:
                raise RuntimeError("The voice splitter's process keeps failing")
            try:
                try:
                    voices = self._ask(samples)
                except (OSError, EOFError):
                    self._stop()              # most often it just left after being idle
                    voices = self._ask(samples)
            except Exception:
                self._failures += 1
                self._stop()
                raise
            self._failures = 0
            return voices

    def _ask(self, samples: np.ndarray) -> List[tuple]:
        process = self._process
        if process is None or process.poll() is not None:
            process = self._process = self._start()
        # A hung worker is killed, which ends the wait for its answer below.
        watchdog = threading.Timer(SPLIT_TIMEOUT_S, process.kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            process.stdin.write(struct.pack('<I', samples.size) + samples.tobytes())
            process.stdin.flush()
            line = process.stdout.readline()
        finally:
            watchdog.cancel()
        if not line:
            raise EOFError("The voice splitter's process ended")
        answer = json.loads(line)
        if 'error' in answer:
            raise RuntimeError(answer['error'])
        return [(float(s), float(e), int(v)) for s, e, v in answer['voices']]

    # Below normal priority, no console window, and able to find this package
    # however the app itself was started.
    def _start(self) -> subprocess.Popen:
        env = dict(os.environ)
        package_root = str(pathlib.Path(__file__).resolve().parents[2])
        env['PYTHONPATH'] = os.pathsep.join(p for p in (package_root, env.get('PYTHONPATH')) if p)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0)
        return subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, creationflags=flags)

    def _stop(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.kill()
            process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
        except Exception as e:
            logger.debug(f"Stopping the voice splitter's process: {e}")


# ── The worker process ───────────────────────────────────────────────────────

# The sherpa-onnx diarizer itself: audio -> [(start_s, end_s, local_voice)].
def load_diarizer(segmentation_model: str, embedding_model: str, num_threads: int = 1) -> Callable:
    import sherpa_onnx
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=segmentation_model),
            num_threads=num_threads),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=embedding_model, num_threads=num_threads),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=CLUSTER_THRESHOLD),
        min_duration_on=MIN_ON_S, min_duration_off=MIN_OFF_S)
    if not config.validate():
        raise RuntimeError("The speaker segmentation model could not be set up")
    diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)

    def diarize(samples: np.ndarray) -> List[tuple]:
        result = diarizer.process(samples).sort_by_start_time()
        return [(float(r.start), float(r.end), int(r.speaker)) for r in result]
    return diarize


# Answers chunks until the app closes the pipe (it quit or crashed), or nothing
# came for `idle_exit_s`. In: sample count (uint32) + float32 samples. Out: one
# JSON line, {"voices": [[start, end, voice], ...]} or {"error": "..."}.
# `load` runs at the first chunk, so a failed model load is that chunk's answer.
def serve(load: Callable[[], Callable], stdin, stdout, idle_exit_s: float = WORKER_IDLE_EXIT_S):
    last_used = [time.monotonic()]

    def leave_when_idle():
        while True:
            time.sleep(min(30.0, idle_exit_s))
            if time.monotonic() - last_used[0] > idle_exit_s:
                os._exit(0)
    threading.Thread(target=leave_when_idle, daemon=True).start()

    diarize = None
    while True:
        header = stdin.read(4)
        if len(header) < 4:
            return
        count = struct.unpack('<I', header)[0]
        data = stdin.read(count * 4)
        if len(data) < count * 4:
            return
        try:
            if diarize is None:
                diarize = load()
            answer = {'voices': diarize(np.frombuffer(data, dtype=np.float32))}
        except Exception as e:
            answer = {'error': str(e)}
        last_used[0] = time.monotonic()
        stdout.write(json.dumps(answer).encode('utf-8') + b'\n')
        stdout.flush()


# ── Giving Whisper's sentences to the voices ─────────────────────────────────

# Which local voice says a piece (a Whisper segment with start/end in the
# chunk's seconds): the one that overlaps it most; with no overlap at all
# (a piece in a gap), the nearest one.
def voice_of(piece: dict, voices: List[tuple]) -> Optional[int]:
    if not voices:
        return None
    start, end = float(piece.get('start', 0.0)), float(piece.get('end', 0.0))
    overlaps = {}
    for v_start, v_end, voice in voices:
        shared = min(end, v_end) - max(start, v_start)
        if shared > 0:
            overlaps[voice] = overlaps.get(voice, 0.0) + shared
    if overlaps:
        return max(overlaps, key=overlaps.get)
    middle = (start + end) / 2
    return min(voices, key=lambda v: min(abs(middle - v[0]), abs(middle - v[1])))[2]


# Whisper's pieces grouped into parts, one per run of the same voice:
# [{'voice', 'start', 'end', 'text'}] in speaking order. A part under
# MIN_PART_S joins the part before it (or after, when it comes first): a
# half-second "yes" isn't worth a turn, and its voice is the least certain.
def parts_by_voice(pieces: List[dict], voices: List[tuple]) -> List[dict]:
    parts = []
    for piece in pieces:
        text = piece.get('text') or ''
        if not text.strip():
            continue
        voice = voice_of(piece, voices)
        if parts and parts[-1]['voice'] == voice:
            parts[-1]['end'] = float(piece['end'])
            parts[-1]['text'] += text
        else:
            parts.append({'voice': voice, 'start': float(piece['start']), 'end': float(piece['end']), 'text': text})
    merged = []
    for part in parts:
        if merged and (part['end'] - part['start'] < MIN_PART_S or merged[-1]['voice'] == part['voice']):
            merged[-1]['end'] = part['end']
            merged[-1]['text'] += part['text']
        else:
            merged.append(part)
    if len(merged) > 1 and merged[0]['end'] - merged[0]['start'] < MIN_PART_S:
        first = merged.pop(0)
        merged[0]['start'] = first['start']
        merged[0]['text'] = first['text'] + merged[0]['text']
    for part in merged:
        part['text'] = part['text'].strip()
    return merged


# All of one voice's audio in the chunk, joined: its voiceprint comes from
# everything it said here, not from one short part.
def voice_audio(audio: np.ndarray, voices: List[tuple], voice: int) -> np.ndarray:
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    pieces = [samples[int(s * RATE):int(e * RATE)] for s, e, v in voices if v == voice]
    pieces = [p for p in pieces if len(p)]
    return np.concatenate(pieces) if pieces else samples[:0]


# Seconds of speech per local voice, for the tracker's "short voice" rule.
def voice_seconds(voices: List[tuple], voice: int) -> float:
    return sum(e - s for s, e, v in voices if v == voice)


if __name__ == '__main__':
    if hasattr(os, 'nice'):
        os.nice(5)      # Windows gets its low priority from the app's Popen flags
    segmentation, embedding, threads = sys.argv[1:4]
    serve(lambda: load_diarizer(segmentation, embedding, int(threads)), sys.stdin.buffer, sys.stdout.buffer)
