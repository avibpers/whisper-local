# online_stt/audio.py
# A recording as the online models want it: a 16 kHz mono 16-bit WAV file in
# memory, plus the language names OpenAI answers with turned back into codes.

import io
import wave

import numpy as np

RATE = 16000


def to_wav(audio: np.ndarray) -> bytes:
    samples = np.clip(np.asarray(audio, dtype=np.float32).flatten(), -1.0, 1.0)
    pcm = (samples * 32767).astype('<i2').tobytes()
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


# "he" -> "Hebrew", for an instruction to the model.
def language_name(code: str) -> str:
    from ..settings.schema import LANGUAGES
    label = dict(LANGUAGES).get(str(code or ''), '')
    return label.split(' ')[0] if label else str(code or '')


# whisper-1's verbose answer names the language ("hebrew"); Koli keeps codes.
def language_code(name: str) -> str:
    name = str(name or '').strip().lower()
    if len(name) <= 3:
        return name
    from ..settings.schema import LANGUAGES
    for code, label in LANGUAGES:
        if label.split(' ')[0].lower() == name:
            return code
    return name
