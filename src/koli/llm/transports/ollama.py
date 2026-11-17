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


# Vectors for a list of texts, one per text and in order. `keep_alive` leaves
# the model loaded between questions, so a search after the first is instant.
def embed(base_url: str, model: str, texts: list, timeout: float = 120) -> list:
    data = http.post_json(f"{base_url.rstrip('/')}/api/embed",
                          {'model': model, 'input': texts, 'keep_alive': '30m'}, timeout=timeout)
    if data.get('error'):
        raise ProviderError(str(data['error']))
    vectors = data.get('embeddings') or []
    if len(vectors) != len(texts):
        raise ProviderError(f"{model} returned {len(vectors)} vectors for {len(texts)} texts")
    return vectors


# Downloads a model, reporting how far it is (0..1) and Ollama's status line
# ("pulling <layer>", "verifying", "success") for a progress bar.
def pull(base_url: str, model: str, on_progress, timeout: float = 3600) -> None:
    url = f"{base_url.rstrip('/')}/api/pull"
    with http.post_stream(url, {'model': model, 'stream': True}, timeout=timeout) as resp:
        for line in resp:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get('error'):
                raise ProviderError(str(obj['error']))
            status = str(obj.get('status') or '')
            total, completed = obj.get('total'), obj.get('completed')
            # Only layer downloads carry sizes; the other status lines have none.
            if total:
                on_progress(min(1.0, float(completed or 0) / float(total)), status)
            elif status == 'success':
                on_progress(1.0, status)
            else:
                on_progress(0.0, status)
