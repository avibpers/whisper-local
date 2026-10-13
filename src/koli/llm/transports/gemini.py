# llm/transports/gemini.py
# Google's Gemini API with an API key: POST models/{model}:generateContent.
# System messages become `systemInstruction`; assistant turns are role
# "model". The key goes in the x-goog-api-key header, never the URL, so it
# can't end up in a log line.

from .. import http
from ..types import ProviderError

BASE_URL = 'https://generativelanguage.googleapis.com/v1beta'


def to_gemini(messages: list) -> dict:
    system, contents = [], []
    for m in messages:
        text = str(m.get('content') or '')
        if m.get('role') == 'system':
            system.append(text)
        else:
            role = 'model' if m.get('role') == 'assistant' else 'user'
            contents.append({'role': role, 'parts': [{'text': text}]})
    body = {'contents': contents}
    if system:
        body['systemInstruction'] = {'parts': [{'text': '\n\n'.join(system)}]}
    return body


def complete(api_key: str, model: str, messages: list, json_mode=None, on_text=None, timeout: float = 30):
    body = to_gemini(messages)
    if json_mode:
        body['generationConfig'] = {'responseMimeType': 'application/json'}
    data = http.post_json(f"{BASE_URL}/models/{model}:generateContent", body,
                          {'x-goog-api-key': api_key}, timeout)
    candidates = data.get('candidates') or []
    if not candidates:
        reason = (data.get('promptFeedback') or {}).get('blockReason')
        raise ProviderError(f"Gemini returned no answer{f' ({reason})' if reason else ''}")
    parts = (candidates[0].get('content') or {}).get('parts') or []
    # Thinking models mark their reasoning parts with `thought: true`.
    text = ''.join(p.get('text', '') for p in parts if not p.get('thought'))
    if on_text and text:
        on_text(text)
    usage = data.get('usageMetadata') or {}
    return text, int(usage.get('promptTokenCount') or 0), int(usage.get('candidatesTokenCount') or 0)


# Models that can answer text (generateContent), newest names first.
def list_models(api_key: str, timeout: float = 15) -> list:
    data = http.get_json(f"{BASE_URL}/models?pageSize=200", {'x-goog-api-key': api_key}, timeout)
    out = []
    for row in data.get('models') or []:
        if 'generateContent' not in (row.get('supportedGenerationMethods') or []):
            continue
        mid = str(row.get('name', '')).removeprefix('models/')
        if mid and 'embedding' not in mid:
            out.append({'id': mid, 'label': row.get('displayName') or mid})
    return sorted(out, key=lambda m: m['id'], reverse=True)
