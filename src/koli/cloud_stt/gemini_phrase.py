# cloud_stt/gemini_phrase.py
# Gemini as the bubble's Cloud engine: Gemini has no streaming dictation on
# an API key, so each phrase (the audio between two pauses) is sent as a WAV
# with an instruction to write down exactly what was said. The answer is the
# phrase; the token counts feed the usage card.

import base64
import io
import wave

import numpy as np

from ..llm import http
from ..llm.transports.gemini import BASE_URL

# Below this loudness a phrase is treated as silence (a quiet room is ~0.001).
SILENCE_RMS = 0.002

SYSTEM = ("You transcribe dictated speech. Reply with only the words spoken, verbatim, in the language "
          "spoken, with normal punctuation. No quotes, no notes. If nothing is said, reply with nothing.")


def _language_name(code: str) -> str:
    try:
        from ..settings.schema import LANGUAGES
        names = {c: n.split(' · ')[0] for c, n in LANGUAGES}
    except Exception:
        names = {}
    return names.get(code, code) if code else ''


# 16 kHz mono float audio as a 16-bit PCM WAV file.
def wav_bytes(mono16k: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(np.asarray(mono16k, dtype=np.float32), -1, 1) * 32767).astype('<i2').tobytes())
    return buffer.getvalue()


# One phrase to text: (text, prompt tokens, output tokens). `context` is what
# was said just before, so names and spellings stay the same across phrases.
def transcribe(audio: np.ndarray, language: str, context: str, key: str, model: str,
               post=http.post_json, timeout: float = 20) -> tuple:
    # Gemini invents a sentence for pure silence, so near-silence is never sent.
    samples = np.asarray(audio, dtype=np.float32)
    if not samples.size or float(np.sqrt(np.mean(samples ** 2))) < SILENCE_RMS:
        return '', 0, 0
    codes = [language] if isinstance(language, str) else list(language or [])
    names = ' or '.join(_language_name(code) for code in codes if code)
    hint = (f"Language: {names or 'detect it'}. "
            f"What was said just before, for names and spelling: {context.strip() or 'nothing'}.")
    body = {
        'systemInstruction': {'parts': [{'text': SYSTEM}]},
        'contents': [{'role': 'user', 'parts': [
            {'text': hint},
            {'inline_data': {'mime_type': 'audio/wav', 'data': base64.b64encode(wav_bytes(audio)).decode()}},
        ]}],
    }
    data = post(f"{BASE_URL}/models/{model}:generateContent", body, {'x-goog-api-key': key}, timeout)
    usage = data.get('usageMetadata') or {}
    tokens_in, tokens_out = int(usage.get('promptTokenCount') or 0), int(usage.get('candidatesTokenCount') or 0)
    candidates = data.get('candidates') or []
    if not candidates:
        return '', tokens_in, tokens_out
    parts = (candidates[0].get('content') or {}).get('parts') or []
    text = ''.join(p.get('text', '') for p in parts if not p.get('thought')).strip()
    return text, tokens_in, tokens_out
