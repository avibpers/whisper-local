# online_stt/gemini.py
# Gemini hears the WAV inline in generateContent and writes back one JSON
# object: the language it heard and the words (models without JSON mode,
# like gemini-3.5-transcribe, write the words alone). usageMetadata splits the
# prompt's tokens by modality, so audio and the instruction are priced apart;
# thinking tokens are billed as output.

import base64

from ..llm import http, parse_json
from ..llm.transports.gemini import BASE_URL
from ..llm.types import ProviderError
from .audio import language_name

INSTRUCTION = ("Transcribe this recording word for word, in the language spoken (do not translate). "
               "Write numbers, names and punctuation as a careful typist would. No timestamps, no speaker "
               "labels, no comments. If nobody speaks, the text is empty.")
JSON_ANSWER = ' Answer with one JSON object: {"language": "<ISO 639-1 code>", "text": "<the transcript>"}.'
TEXT_ANSWER = ' Answer with the transcript alone.'


# gemini-3.5-transcribe refuses JSON mode (HTTP 400, checked 2026-10-29).
def _json_mode(model: str) -> bool:
    return 'transcribe' not in model


def transcribe(key: str, model: str, wav: bytes, language: str = '', prompt: str = '',
               timeout: float = 60, speaks: tuple = ()) -> tuple:
    json_mode = _json_mode(model)
    instruction = INSTRUCTION + (JSON_ANSWER if json_mode else TEXT_ANSWER)
    if language:
        instruction += f" The recording is in the language with code '{language}'."
    elif speaks:
        # Your languages (whisper.languages): without them a Hebrew word came
        # back in Latin letters ("Metupal"); calling one of them the main
        # language made English come back in Hebrew letters. So: neutral.
        names = ' or '.join(language_name(code) for code in speaks)
        instruction += (f" The speaker speaks {names}, sometimes both in one sentence. Write each word in "
                        "the alphabet of the language it was spoken in; never transliterate.")
    if prompt:
        instruction += f" Words and names that may come up, spelled this way: {prompt[:800]}"
    body = {'contents': [{'role': 'user', 'parts': [
                {'inlineData': {'mimeType': 'audio/wav', 'data': base64.b64encode(wav).decode('ascii')}},
                {'text': instruction}]}],
            'generationConfig': {'temperature': 0, **({'responseMimeType': 'application/json'} if json_mode else {})}}
    data = http.post_json(f"{BASE_URL}/models/{model}:generateContent", body, {'x-goog-api-key': key}, timeout)
    candidates = data.get('candidates') or []
    if not candidates:
        reason = (data.get('promptFeedback') or {}).get('blockReason')
        raise ProviderError(f"Gemini returned no answer{f' ({reason})' if reason else ''}")
    parts = (candidates[0].get('content') or {}).get('parts') or []
    # The transcription model answers in parts[].audioTranscription, the
    # others in parts[].text.
    raw = ''.join(p.get('text') or (p.get('audioTranscription') or {}).get('text') or ''
                  for p in parts if not p.get('thought')).strip()
    answer = parse_json(raw) if json_mode else None
    text = str(answer.get('text') or '') if answer is not None else raw
    heard = next(((p.get('audioTranscription') or {}).get('languageCode') for p in parts
                  if (p.get('audioTranscription') or {}).get('languageCode')), '')
    lang = str((answer or {}).get('language') or heard or language or '').lower()[:5]
    usage = data.get('usageMetadata') or {}
    by_modality = {str(d.get('modality', '')).upper(): int(d.get('tokenCount') or 0)
                   for d in usage.get('promptTokensDetails') or []}
    audio_tokens = by_modality.get('AUDIO', 0)
    text_in = by_modality.get('TEXT', 0) if by_modality else int(usage.get('promptTokenCount') or 0)
    info = {'language': lang, 'segments': [],
            'audio_tokens': audio_tokens, 'tokens_in': text_in,
            'tokens_out': int(usage.get('candidatesTokenCount') or 0) + int(usage.get('thoughtsTokenCount') or 0),
            'billed_seconds': 0.0}
    return text.strip(), info
