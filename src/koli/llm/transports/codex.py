# llm/transports/codex.py
# The ChatGPT subscription through the Codex backend: OpenAI's Responses API
# at chatgpt.com/backend-api/codex/responses. It only answers streamed
# (`stream: true`, `store: false`), so the answer is always read as server-sent
# events. System messages become `instructions`; the rest become `input`.
# Ported from BaileysWhatsAPP providers/codex/{index,sse}.ts.

import json
import uuid

from .. import http
from ..sse import iter_events
from ..types import ProviderError
from .chat_completions import with_json_instruction

DEFAULT_INSTRUCTIONS = 'You are a helpful assistant.'


# Chat messages → (instructions, input) for the Responses API.
def to_responses_input(messages: list) -> tuple:
    instructions, items = [], []
    for m in messages:
        content = str(m.get('content') or '')
        if m.get('role') == 'system':
            instructions.append(content)
        else:
            items.append({'role': m.get('role') or 'user', 'content': content})
    return '\n\n'.join(instructions) or DEFAULT_INSTRUCTIONS, items


def complete(base_url: str, headers: dict, model: str, messages: list, json_mode=None,
             on_text=None, timeout: float = 60):
    if json_mode:
        # No JSON mode on this surface.
        messages = with_json_instruction(messages)
    instructions, items = to_responses_input(messages)
    payload = {'model': model, 'instructions': instructions, 'input': items, 'stream': True, 'store': False}
    hdrs = {**headers, 'openai-beta': 'responses=experimental', 'session_id': str(uuid.uuid4())}
    with http.post_stream(f"{base_url.rstrip('/')}/responses", payload, hdrs, timeout) as resp:
        return parse_stream(iter_events(resp), on_text)


# Reads the event stream to the end. Text arrives as output_text deltas; the
# final `response.completed` carries usage and the whole output, used when no
# delta came through. A `response.failed` or `error` event raises.
def parse_stream(events, on_text=None) -> tuple:
    parts, usage, failure = [], {}, None
    for event, data in events:
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        kind = obj.get('type') or event or ''
        if kind == 'response.output_text.delta' and isinstance(obj.get('delta'), str):
            parts.append(obj['delta'])
            if on_text:
                on_text(obj['delta'])
        elif kind == 'response.completed':
            response = obj.get('response') or {}
            usage = response.get('usage') or {}
            if not parts:
                for item in response.get('output') or []:
                    if item.get('type') == 'message':
                        for piece in item.get('content') or []:
                            if piece.get('text'):
                                parts.append(piece['text'])
                if parts and on_text:
                    on_text(''.join(parts))
        elif kind in ('response.failed', 'error'):
            failure = (obj.get('response') or {}).get('error') or obj.get('error') or obj
    if failure:
        message = failure.get('message') if isinstance(failure, dict) else str(failure)
        raise ProviderError(f"Codex: {message or json.dumps(failure)[:200]}")
    return ''.join(parts), int(usage.get('input_tokens') or 0), int(usage.get('output_tokens') or 0)


# GET {base}/models?client_version=… → [{id, label}]. Rows hidden from pickers
# (`visibility: hide`) are left out.
def list_models(base_url: str, headers: dict, client_version: str, timeout: float = 15) -> list:
    data = http.get_json(f"{base_url.rstrip('/')}/models?client_version={client_version}", headers, timeout)
    out = []
    for row in data.get('models') or []:
        slug = row.get('slug') or row.get('id')
        if slug and row.get('visibility', 'list') in ('', 'list', None):
            out.append({'id': slug, 'label': row.get('display_name') or slug})
    return out
