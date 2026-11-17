# src/koli/meeting/pieces.py
# Cuts a saved meeting and its AI summary into pieces that search embeds:
# overlapping transcript windows, one piece per summary item, one per screenshot caption.
from .store import plain_text

# ── Sizes ────────────────────────────────────────────────────────────────────
WINDOW_S = 75           # seconds of talk in one transcript piece
OVERLAP_S = 20          # the next piece starts this long before the last one ends
MIN_ITEM_CHARS = 15     # shorter summary items say too little to find by meaning

# Layout and bookkeeping fields of a summary: not things anybody said or decided.
SKIPPED_SUMMARY_FIELDS = frozenset({
    'labels', 'generator', 'template', 'version', 'written', 'language', 'date',
    'meeting_id', 'title', 'participants', 'roles', 'attendees', 'instructions',
})


# ── Public API ───────────────────────────────────────────────────────────────

# Every searchable piece of one meeting: transcript, summary, screenshot captions.
def pieces(meeting: dict, brief: dict | None) -> list[dict]:
    return _transcript_pieces(meeting) + _summary_pieces(brief) + _screenshot_pieces(meeting)


# The words that open every embedded piece, so a hit knows which meeting it is from.
def label(meeting: dict) -> str:
    day = str(meeting.get('started_at') or '')[:10]
    return f'Meeting "{meeting.get("title") or ""}" on {day}'


# What the model actually embeds for a piece: the label, a line break, the piece text.
def embed_text(meeting: dict, piece: dict) -> str:
    return f'{label(meeting)}\n{piece["text"]}'


# ── Transcript ───────────────────────────────────────────────────────────────

# Windows of WINDOW_S seconds, each starting OVERLAP_S before the last one ended.
def _transcript_pieces(meeting: dict) -> list[dict]:
    names = {s.get('id'): s.get('name') for s in meeting.get('speakers') or []}
    turns = [t for t in meeting.get('turns') or [] if str(t.get('text') or '').strip()]
    out = []
    first = 0
    while first < len(turns):
        window_start = float(turns[first]['start'])
        window_end = window_start + WINDOW_S
        last = first + 1                      # a window always holds at least one turn
        while last < len(turns) and float(turns[last]['start']) < window_end:
            last += 1
        lines = [_turn_line(t, names) for t in turns[first:last]]
        out.append({'kind': 'transcript', 'field': 'turns', 'at': window_start, 'text': '\n'.join(lines)})
        if last >= len(turns):
            break
        # Next window: first turn at or after the overlap point, but always moving forward.
        next_first = first + 1
        while next_first < last and float(turns[next_first]['start']) < window_end - OVERLAP_S:
            next_first += 1
        first = next_first
    return out


# "Name: what was said", falling back to the speaker id when the name is unknown.
def _turn_line(turn: dict, names: dict) -> str:
    speaker = turn.get('speaker')
    return f'{names.get(speaker) or speaker or "?"}: {str(turn["text"]).strip()}'


# ── Summary ──────────────────────────────────────────────────────────────────

# One piece per list item (or per scalar) of each top-level summary field.
def _summary_pieces(brief: dict | None) -> list[dict]:
    out = []
    for field, value in (brief or {}).items():
        if field in SKIPPED_SUMMARY_FIELDS:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = plain_text(item).strip()
            if len(text) >= MIN_ITEM_CHARS:
                out.append({'kind': 'summary', 'field': field, 'at': _item_seconds(item), 'text': text})
    return out


# The second an item points at: its own `at`, else its `start`, else nowhere in particular.
def _item_seconds(item) -> float | None:
    if not isinstance(item, dict):
        return None
    for key in ('at', 'start'):
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


# ── Screenshots ──────────────────────────────────────────────────────────────

# A screenshot is found by what its caption says it shows.
def _screenshot_pieces(meeting: dict) -> list[dict]:
    out = []
    for shot in meeting.get('screenshots') or []:
        caption = str(shot.get('caption') or '').strip()
        if caption:
            out.append({'kind': 'screenshot', 'field': 'caption', 'at': shot.get('at'), 'text': caption})
    return out
