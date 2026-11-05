# llm/transports/ollama.py
# Ollama's own API: POST /api/chat for answers, GET /api/tags for the models
# you have pulled. Its native JSON mode (`format: json`) is reliable, so it is
# used instead of an instruction.

import json

from .. import http
from ..types import ProviderError


# `options`: extra fields for the request, for a feature where speed matters
# (smart_correction.py: no thinking, a small context, stay loaded).
def complete(base_url: str, model: str, messages: list, json_mode=None, on_text=None, timeout: float = 30,
             options: dict = None):
    # Pictures go in Ollama's own `images` field: plain base64, no type.
    messages = [{**m, 'images': [image['data'] for image in m['images'] if image.get('data')]}
                if m.get('images') else m for m in messages]
    payload = {'model': model, 'messages': messages, 'stream': bool(on_text), **(options or {})}
    if json_mode:
        payload['format'] = 'json'
    url = f"{base_url.rstrip('/')}/api/chat"
    if not on_text:
        data = http.post_json(url, payload, timeout=timeout)
        if data.get('error'):
            raise ProviderError(str(data['error']))
        return (str((data.get('message') or {}).get('content') or ''),
                int(data.get('prompt_eval_count') or 0), int(data.get('eval_count') or 0))
    # Streaming: one JSON object per line.
    parts, t_in, t_out = [], 0, 0
    with http.post_stream(url, payload, timeout=timeout) as resp:
        for line in resp:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get('error'):
                raise ProviderError(str(obj['error']))
            delta = (obj.get('message') or {}).get('content')
            if delta:
                parts.append(delta)
                on_text(delta)
            if obj.get('done'):
                t_in, t_out = int(obj.get('prompt_eval_count') or 0), int(obj.get('eval_count') or 0)
    return ''.join(parts), t_in, t_out


# Loads the model without asking it anything. Answers in milliseconds when it is
# already loaded; `options` must match the real calls, or Ollama loads it again.
def warm(base_url: str, model: str, options: dict = None, timeout: float = 120):
    http.post_json(f"{base_url.rstrip('/')}/api/chat", {'model': model, 'messages': [], 'stream': False, **(options or {})},
                   timeout=timeout)


def list_models(base_url: str, timeout: float = 5) -> list:
    data = http.get_json(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
    return [{'id': m['name'], 'label': m['name']} for m in data.get('models') or [] if m.get('name')]
