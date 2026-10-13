# llm/transports/chat_completions.py
# The OpenAI chat format (POST {base}/chat/completions), spoken by LM Studio,
# OpenRouter, OpenAI and the Grok CLI proxy. Streams when the caller wants text
# as it arrives, and also accepts a plain JSON answer to a streamed request
# (some proxies ignore `stream`).

import json

from .. import http
from ..sse import iter_events
from ..types import ProviderError

JSON_INSTRUCTION = 'Answer with one JSON object only, no other text.'


# `json_mode` is 'native' (response_format), 'instruction' (a system line), or
# None. Not every server accepts response_format, so providers choose.
def complete(base_url: str, headers: dict, model: str, messages: list, json_mode=None,
             on_text=None, timeout: float = 30, stream: bool = False, extra: dict = None):
    if json_mode == 'instruction':
        messages = with_json_instruction(messages)
    payload = {'model': model, 'messages': messages, 'stream': bool(stream or on_text)}
    if json_mode == 'native':
        payload['response_format'] = {'type': 'json_object'}
    if extra:
        payload.update(extra)
    url = f"{base_url.rstrip('/')}/chat/completions"
    if not payload['stream']:
        return _parse_answer(http.post_json(url, payload, headers, timeout))
    with http.post_stream(url, payload, headers, timeout) as resp:
        if 'application/json' in (resp.headers.get('Content-Type') or ''):
            text, t_in, t_out = _parse_answer(json.loads(resp.read() or b'{}'))
            if on_text and text:
                on_text(text)
            return text, t_in, t_out
        return _read_stream(resp, on_text)


def _parse_answer(data) -> tuple:
    if isinstance(data, dict) and data.get('error'):
        err = data['error']
        raise ProviderError(err.get('message') if isinstance(err, dict) else str(err))
    choices = (data or {}).get('choices') or []
    message = (choices[0].get('message') if choices else None) or {}
    usage = (data or {}).get('usage') or {}
    return (str(message.get('content') or ''),
            int(usage.get('prompt_tokens') or usage.get('input_tokens') or 0),
            int(usage.get('completion_tokens') or usage.get('output_tokens') or 0))


def _read_stream(resp, on_text) -> tuple:
    parts, t_in, t_out = [], 0, 0
    for _event, data in iter_events(resp):
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        if obj.get('error'):
            err = obj['error']
            raise ProviderError(err.get('message') if isinstance(err, dict) else str(err))
        for choice in obj.get('choices') or []:
            delta = (choice.get('delta') or {}).get('content')
            if delta:
                parts.append(delta)
                if on_text:
                    on_text(delta)
        usage = obj.get('usage') or {}
        t_in = int(usage.get('prompt_tokens') or t_in)
        t_out = int(usage.get('completion_tokens') or t_out)
    return ''.join(parts), t_in, t_out


# Ask for JSON in words, for servers without a JSON mode.
def with_json_instruction(messages: list) -> list:
    if any(m.get('role') == 'system' for m in messages):
        return [{**m, 'content': f"{m['content']}\n\n{JSON_INSTRUCTION}"} if m.get('role') == 'system' else m
                for m in messages]
    return [{'role': 'system', 'content': JSON_INSTRUCTION}, *messages]


# GET {base}/models in the OpenAI shape ({data: [{id}]}); some servers answer
# {models: [...]} or a bare list.
def list_model_ids(base_url: str, headers: dict, timeout: float = 10) -> list:
    data = http.get_json(f"{base_url.rstrip('/')}/models", headers, timeout)
    rows = data if isinstance(data, list) else (data.get('data') or data.get('models') or [])
    ids = []
    for row in rows:
        mid = (row.get('id') or row.get('slug') or row.get('name')) if isinstance(row, dict) else row
        if mid:
            ids.append(str(mid))
    return ids
