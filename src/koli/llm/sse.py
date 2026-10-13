# llm/sse.py
# Server-sent events, the format Codex and the OpenAI-style providers stream
# in: blocks separated by a blank line, each with an optional "event:" line and
# "data:" lines. Yields (event, data) with the data lines joined. Reading stops
# at "[DONE]" or when the stream ends.

def iter_events(stream):
    event, data = None, []
    for raw in stream:
        line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
        if not line:
            if data:
                joined = '\n'.join(data)
                if joined.strip() == '[DONE]':
                    return
                yield event, joined
            event, data = None, []
            continue
        if line.startswith(':'):
            continue
        if line.startswith('event:'):
            event = line[6:].strip()
        elif line.startswith('data:'):
            data.append(line[5:].lstrip())
    if data:
        joined = '\n'.join(data)
        if joined.strip() != '[DONE]':
            yield event, joined
