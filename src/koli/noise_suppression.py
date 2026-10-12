# noise_suppression.py
# Optional spectral-gating noise reduction applied to recorded audio before it
# reaches Whisper. Wraps the `noisereduce` library, which is itself a wrapper
# around librosa's spectral subtraction. Off by default — enable via
# `audio.noise_suppression.enabled: true` and `pip install 'koli[noise]'`.
# Falls back to passthrough silently when the library isn't installed.

import logging
import threading

import numpy as np

logger = logging.getLogger(__name__)

# Set once the library has been loaded and run, so it is warmed up only once.
_warmed = threading.Event()


# The first run loads the library and its dependencies, which took 4.8 s on
# 2026-10-12: an auto-dictation phrase prepared that slowly can outlive its
# session. Running it once on half a second of silence, in the background, as
# soon as noise suppression is on keeps the first real recording fast.
def warm_up_in_background(sample_rate: int = 16000) -> None:
    if _warmed.is_set():
        return
    _warmed.set()

    def run():
        apply_noise_reduction(np.zeros(sample_rate // 2, dtype=np.float32), sample_rate)
        logger.debug("Noise suppression warmed up")
    threading.Thread(target=run, name='noise-warm-up', daemon=True).start()


# Returns the noise-reduced array on success, the original array on any failure
# (missing library, processing error, etc.). Never raises — noise reduction is
# strictly opt-in polish, never required for correct transcription.
def apply_noise_reduction(audio: np.ndarray, sample_rate: int, strength: float = 0.75) -> np.ndarray:
    try:
        import noisereduce as nr
        flat = audio.flatten()
        reduced = nr.reduce_noise(y=flat, sr=sample_rate, prop_decrease=float(strength))
        return reduced.reshape(audio.shape).astype(np.float32)
    except ImportError:
        logger.debug("noisereduce not installed; skipping. Install: pip install 'koli[noise]'")
        return audio
    except Exception as e:
        logger.warning(f"Noise reduction failed: {e}")
        return audio
