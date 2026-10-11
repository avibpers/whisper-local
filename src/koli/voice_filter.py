# voice_filter.py
# "Only my voice": leaves out speech that isn't yours, so a TV across the room
# or someone talking in your pauses doesn't get typed. Step 1 is loudness:
# Koli learns how loud you are from your hotkey dictations (always you, close
# to the mic) and drops pieces of a recording much quieter than that. Pure
# numpy; the state manager calls it per Whisper segment.

import json
import logging
import statistics
import threading
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_SECONDS = 0.03
SILENT_RMS = 1e-4          # below this a frame is silence, not quiet speech
MIN_FRAMES = 6             # ~0.2 s of sound before a level means anything
LEARN_KEEP = 20            # recent dictations a microphone's level is taken from
LEARN_MIN = 3              # dictations needed before the level is trusted


# ── Measuring ────────────────────────────────────────────────────────────────

# How loud the speech in `audio` is, in dBFS: the loud end (90th percentile)
# of its 30 ms frames, so pauses don't pull it down. `gain` is a boost applied
# after recording (Whisper mode), taken back out so levels stay comparable.
# None when there is too little sound to judge.
def speech_level_db(audio: np.ndarray, sample_rate: int = SAMPLE_RATE, gain: float = 1.0) -> Optional[float]:
    if audio is None or len(audio) == 0:
        return None
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    frame = int(FRAME_SECONDS * sample_rate)
    count = len(mono) // frame
    if frame < 1 or count < MIN_FRAMES:
        return None
    frames = mono[:count * frame].astype(np.float64).reshape(count, frame)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    voiced = rms[rms > SILENT_RMS]
    if len(voiced) < 3:
        return None
    level = float(np.percentile(20 * np.log10(voiced), 90))
    return level - 20 * float(np.log10(gain)) if gain and gain > 0 else level


# ── Learning your level ──────────────────────────────────────────────────────

# Your speaking level per microphone, from your recent hotkey dictations, kept
# in a small JSON file next to the settings. A new microphone starts unknown.
class VoiceLevels:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._levels = self._read()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return {str(k): [float(x) for x in v][-LEARN_KEEP:] for k, v in data.items() if isinstance(v, list)}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"Could not read {self.path}: {e}")
            return {}

    def learn(self, device: str, level_db: float) -> None:
        with self._lock:
            recent = self._levels.setdefault(device, [])
            recent.append(round(float(level_db), 1))
            del recent[:-LEARN_KEEP]
            try:
                self.path.write_text(json.dumps(self._levels, indent=1), encoding='utf-8')
            except Exception as e:
                logger.warning(f"Could not save {self.path}: {e}")

    # The middle of your recent levels, or None until there are enough.
    def my_level(self, device: str) -> Optional[float]:
        with self._lock:
            recent = self._levels.get(device) or []
            return float(statistics.median(recent)) if len(recent) >= LEARN_MIN else None


# ── Leaving out what isn't you ───────────────────────────────────────────────

# The text to keep from a recording, and why anything was left out. `pieces`
# are Whisper's segments with their text and start/end seconds in `audio`; a
# piece more than `margin_db` quieter than your level is someone else. With no
# piece times (whisper.cpp) the recording is judged as a whole.
def keep_my_voice(text: str, audio: np.ndarray, pieces, my_level_db: float, margin_db: float,
                  gain: float = 1.0, sample_rate: int = SAMPLE_RATE) -> tuple:
    if not pieces:
        level = speech_level_db(audio, sample_rate, gain)
        if level is not None and level < my_level_db - margin_db:
            return '', f'{my_level_db - level:.0f} dB quieter than you'
        return text, ''
    kept, why = my_voice_pieces(audio, pieces, my_level_db, margin_db, gain, sample_rate)
    return (text, '') if not why else (''.join(p.get('text', '') for p in kept).strip(), why)


# The pieces loud enough to be you, and why any were left out. Later checks
# (Whisper's confidence) then work on what is left.
def my_voice_pieces(audio: np.ndarray, pieces: list, my_level_db: float, margin_db: float,
                    gain: float = 1.0, sample_rate: int = SAMPLE_RATE) -> tuple:
    threshold = my_level_db - margin_db
    kept, quietest = [], None
    for piece in pieces:
        level = None
        if piece.get('start') is not None and piece.get('end') is not None:
            clip = audio[int(piece['start'] * sample_rate):int(piece['end'] * sample_rate)]
            level = speech_level_db(clip, sample_rate, gain)
        if level is not None and level < threshold:
            quietest = level if quietest is None else min(quietest, level)
        else:
            kept.append(piece)
    if len(kept) == len(pieces):
        return pieces, ''
    return kept, f'{len(pieces) - len(kept)} part(s) up to {my_level_db - quietest:.0f} dB quieter than you left out'
