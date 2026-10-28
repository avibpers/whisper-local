# online_stt/openai.py
# OpenAI's speech-to-text upload: POST /v1/audio/transcriptions with the WAV
# as multipart form data (standard library only, like llm/http.py). whisper-1
# answers verbose_json, whose segments carry the certainty numbers the local
# guards read; the gpt-4o family answers json with token usage.

import json
import uuid

from ..llm import http
from ..llm.types import ProviderError
from .audio import language_code

URL = 'https://api.openai.com/v1/audio/transcriptions'


def _multipart(fields: dict, wav: bytes) -> tuple:
    boundary = f"koli-{uuid.uuid4().hex}"
    out = []
    for name, value in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode('utf-8'))
    out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
               f'Content-Type: audio/wav\r\n\r\n'.encode('utf-8'))
    out.append(wav)
    out.append(f'\r\n--{boundary}--\r\n'.encode('utf-8'))
    return b''.join(out), f'multipart/form-data; boundary={boundary}'


# (text, info) where info has language, segments and usage numbers.
def transcribe(key: str, model: str, wav: bytes, language: str = '', prompt: str = '',
               timeout: float = 60) -> tuple:
    verbose = model == 'whisper-1'
    diarize = 'diarize' in model
    fields = {'model': model, 'response_format': 'verbose_json' if verbose else 'json', 'temperature': '0'}
    if language:
        fields['language'] = language
    if prompt and not diarize:          # the diarizing model takes no prompt
        fields['prompt'] = prompt[:800]
    if diarize:
        fields['chunking_strategy'] = 'auto'
    body, content_type = _multipart(fields, wav)
    _status, _headers, raw = http.raw('POST', URL, {'Authorization': f"Bearer {key}", 'Content-Type': content_type,
                                                    'Accept': 'application/json'}, body, timeout)
    try:
        data = json.loads(raw.decode('utf-8', errors='replace') or '{}')
    except ValueError:
        raise ProviderError(f"OpenAI gave an answer that isn't JSON: {raw[:120]!r}") from None
    if data.get('error'):
        err = data['error']
        raise ProviderError(err.get('message') if isinstance(err, dict) else str(err))
    usage = data.get('usage') or {}
    details = usage.get('input_token_details') or {}
    segments = []
    for s in data.get('segments') or []:
        if 'avg_logprob' in s:
            segments.append({'text': s.get('text', ''), 'start': float(s.get('start') or 0), 'end': float(s.get('end') or 0),
                             'no_speech_prob': round(float(s.get('no_speech_prob') or 0), 3),
                             'avg_logprob': round(float(s.get('avg_logprob') or 0), 3),
                             'compression_ratio': round(float(s.get('compression_ratio') or 0), 2)})
    info = {'language': language_code(data.get('language') or language),
            'segments': segments,
            'audio_tokens': int(details.get('audio_tokens') or 0),
            'tokens_in': int(details.get('text_tokens') or 0),
            'tokens_out': int(usage.get('output_tokens') or 0),
            'billed_seconds': float(usage.get('seconds') or 0)}
    return str(data.get('text') or '').strip(), info
