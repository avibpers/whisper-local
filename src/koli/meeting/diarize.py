# meeting/diarize.py
# Where the voice changes inside one chunk of the call. A 15-second chunk can
# hold three people; labelled as one, their voices blurred into one speaker
# (a 4-person Meet came out as "You" and "Speaker 1"). pyannote's
# segmentation model, through sherpa-onnx, marks who speaks when inside the
# chunk (local voices 0, 1, 2); the tracker then decides who each one is.
# The model is optional: without it a chunk stays one voice, as before.

import logging
import threading
from typing import Callable, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

RATE = 16000
CLUSTER_THRESHOLD = 0.5   # sherpa-onnx's own default for telling local voices apart
MIN_ON_S = 0.3            # shorter bursts of a voice are folded into their neighbours
MIN_OFF_S = 0.5           # shorter pauses inside one voice don't split it
MIN_PART_S = 1.0          # a part shorter than this is never a turn of its own


# ── Splitting a chunk ────────────────────────────────────────────────────────

# A function from a chunk's audio (mono float32, 16 kHz) to the voices in it:
# [(start_s, end_s, local_voice)], sorted by start. One sherpa-onnx
# diarizer, serialised like the voiceprint extractor.
def make_splitter(segmentation_model: str, embedding_model: str,
                  num_threads: int = 1) -> Callable[[np.ndarray], List[tuple]]:
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
    lock = threading.Lock()

    def split(audio: np.ndarray) -> List[tuple]:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        with lock:
            result = diarizer.process(samples).sort_by_start_time()
        return [(float(r.start), float(r.end), int(r.speaker)) for r in result]
    return split


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
