# meeting/ask.py
# "Ask all meetings": the AI plans what to look at (phrases to search, meetings to open whole), the
# code gathers that material under a size limit, and the AI answers with references that are checked
# here, so a made-up meeting or second never reaches the page (docs/design/meeting-search.md).

import json
import logging
import re
from typing import Callable, Optional

from ..usage.ledger import for_meeting
from .index import cap
from .summarize import SummaryError

logger = logging.getLogger(__name__)

# ── Limits ───────────────────────────────────────────────────────────────────

MAX_MATERIAL = 60000      # characters of material the answering model gets
MAX_SEARCH = 3            # search phrases and meetings to open, from the plan
MAX_OPEN = 3
OPEN_PIECES = 12          # a meeting too long to open whole gives its best pieces
PER_MEETING = 4           # pieces of one meeting among the search results
TOTAL_PIECES = 30
HISTORY = 10              # earlier questions (with their answers) the model sees again
LIST_MAX_CHARS = 20000    # the planner's list of meetings, newest first, the oldest lines dropped
TITLE_MAX = 80            # a title or the people in one list line are cut to this many characters
PEOPLE_MAX = 120
UNAVAILABLE_NOTE = '(Search by meaning is unavailable right now; only whole meetings were read.)'

PLAN_SYSTEM = (
    "You help the user with their past meetings. You get the list of all meetings and the question. "
    "Choose how to answer: \"search\": up to 3 short phrases to find passages by meaning (add a Hebrew and an "
    "English phrasing when useful), for topics; \"open\": up to 3 meeting ids to read whole, when the question "
    "points at meetings by date, person or title. Use one or both. "
    "Answer with JSON only: {\"task\": \"<what the user wants, one line>\", \"search\": [...], \"open\": [...]}")

ANSWER_SYSTEM = (
    "You answer questions about the user's past meetings from the material only: pieces of many meetings "
    "and/or whole transcripts, each headed [title | date | id | where m:ss]. Say so when the material doesn't "
    "tell. Mark each fact with [n] and list n in refs as {\"id\": \"<meeting id>\", \"at\": <second>}. "
    "Answer in the language of the question, short and concrete. JSON only: {\"reply\": \"...\", \"refs\": [...]}")


# ── The plan ─────────────────────────────────────────────────────────────────

# m:ss, as the transcripts and the page show times.
def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f'{seconds // 60}:{seconds % 60:02d}'


# One line per meeting, newest first: id | start | title | app | people | turns | summary or not. Title and
# people are cut short and the list stops at `max_chars`, so the oldest meetings fall off a long list.
def meeting_list(heads: list, max_chars: int = LIST_MAX_CHARS) -> str:
    lines, used = [], 0
    for head in heads:
        start = str(head.get('started_at') or '')[:16].replace('T', ' ')
        people = ', '.join(str(name) for name in head.get('speakers') or [])
        line = ' | '.join([str(head.get('id') or ''), start, str(head.get('title') or '')[:TITLE_MAX],
                           str(head.get('source') or ''), people[:PEOPLE_MAX], str(head.get('turns') or 0),
                           'summary' if head.get('brief') else 'no summary'])
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return '\n'.join(lines)


# Earlier messages as the model sees them: the last HISTORY questions with their answers, text only.
def _history_messages(history: list) -> list:
    return [{'role': h['role'], 'content': str(h['text'])} for h in history[-2 * HISTORY:]
            if h.get('role') in ('user', 'assistant') and h.get('text')]


# What the planner reads: earlier conversation, then the list of meetings with the question.
def plan_messages(listing: str, history: list, question: str) -> list:
    context = f'All meetings (id | start | title | app | people | turns | summary):\n{listing or "(none)"}\n\nQuestion: {question}'
    return [{'role': 'system', 'content': PLAN_SYSTEM}, *_history_messages(history),
            {'role': 'user', 'content': context}]


# The plan as the code uses it: a task line, at most 3 non-empty phrases, at most 3 meeting ids that exist.
def clean_plan(plan: dict, known_ids: set) -> dict:
    plan = plan if isinstance(plan, dict) else {}
    phrases = [str(p).strip() for p in plan.get('search') or [] if isinstance(p, str) and p.strip()]
    opened = []
    for meeting_id in plan.get('open') or []:
        if isinstance(meeting_id, str) and meeting_id in known_ids and meeting_id not in opened:
            opened.append(meeting_id)
    return {'task': str(plan.get('task') or '').strip(), 'search': phrases[:MAX_SEARCH], 'open': opened[:MAX_OPEN]}


# ── The material ─────────────────────────────────────────────────────────────

# How long a meeting ran, as far as its turns and screenshots say: references past it are made up.
def _duration(meeting: dict) -> float:
    ends = [t.get('end', t.get('start')) for t in meeting.get('turns', [])]
    ends += [s.get('at') for s in meeting.get('screenshots', [])]
    return max((float(end) for end in ends if _is_number(end)), default=0.0)


# True for a real number of seconds (a bool is not one).
def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _heading(title, started_at, meeting_id, where: str) -> str:
    return f'[{title or "Meeting"} | {str(started_at or "")[:10]} | {meeting_id} | {where}]'


# A whole meeting: its heading, then one line per turn, "m:ss Name: text", in time order. A turn
# without a numeric start can't be placed or referenced, so it is left out (as pieces.py does).
def _full_text(meeting: dict) -> str:
    names = {s['id']: s.get('name') or s['id'] for s in meeting.get('speakers', [])}
    turns = sorted((t for t in meeting.get('turns', []) if _is_number(t.get('start'))),
                   key=lambda t: float(t['start']))
    lines = [f"{_clock(t['start'])} {names.get(t.get('speaker'), t.get('speaker'))}: {t.get('text', '')}"
             for t in turns]
    head = _heading(meeting.get('title'), meeting.get('started_at'), meeting['id'], 'full transcript')
    return '\n'.join([head, *lines])


# One found piece with its heading: [title | date | id | kind m:ss] and the text.
def _piece_text(piece: dict) -> str:
    where = str(piece.get('kind') or 'transcript')
    if piece.get('at') is not None:
        where += f' {_clock(piece["at"])}'
    return f'{_heading(piece.get("title"), piece.get("started_at"), piece["id"], where)}\n{piece["text"]}'


# The pieces every search phrase found, each piece once at its best score, best first.
def _search_pieces(phrases: list, question: str, hits_for: Callable) -> list:
    best: dict = {}
    for phrase in phrases:
        for piece in hits_for(phrase, None, PER_MEETING, TOTAL_PIECES):
            key = (piece['id'], piece.get('field'), piece.get('at'), piece.get('text'))
            if key not in best or piece['score'] > best[key]['score']:
                best[key] = piece
    return sorted(best.values(), key=lambda p: -p['score'])


# What the answering model reads and which meetings it came from: opened meetings first (whole when they
# fit, else their best pieces), then the pieces the search phrases found, never more than MAX_MATERIAL.
# `search_unavailable()` says the meaning search couldn't run; the material then tells the model so.
# Returns (text, {meeting_id: duration_s}) for every meeting that put something in.
def material(plan: dict, question: str, load: Callable[[str], dict], hits_for: Callable,
             search_unavailable: Optional[Callable[[], bool]] = None) -> tuple:
    blocks: list = []
    given: dict = {}
    used = [0]

    # Adds a block when it fits, and notes where it came from; False when it does not fit.
    def add(block: str, meeting_id: str, duration: Optional[float]) -> bool:
        if used[0] + len(block) + 2 > MAX_MATERIAL:
            return False
        blocks.append(block)
        used[0] += len(block) + 2
        if meeting_id not in given:
            given[meeting_id] = duration if duration is not None else _duration_of(meeting_id)
        return True

    durations: dict = {}

    def _duration_of(meeting_id: str) -> float:
        if meeting_id not in durations:
            try:
                durations[meeting_id] = _duration(load(meeting_id))
            except Exception as e:                      # a meeting that can't be read gives no references
                logger.warning(f'Ask could not read {meeting_id}: {e}')
                durations[meeting_id] = 0.0
        return durations[meeting_id]

    opened = []
    for meeting_id in plan.get('open', []):
        try:
            meeting = load(meeting_id)
        except Exception as e:
            logger.warning(f'Ask could not open {meeting_id}: {e}')
            continue
        durations[meeting_id] = _duration(meeting)
        opened.append(meeting_id)
        if add(_full_text(meeting), meeting_id, durations[meeting_id]):
            continue
        for piece in hits_for(question, meeting_id, OPEN_PIECES, OPEN_PIECES):      # too long: its best pieces
            if not add(_piece_text(piece), meeting_id, durations[meeting_id]):
                break
    pieces = _search_pieces(plan.get('search', []), question, hits_for)
    for piece in cap([p for p in pieces if p['id'] not in opened], PER_MEETING, TOTAL_PIECES):
        if not add(_piece_text(piece), piece['id'], None):
            break
    if plan.get('search') and search_unavailable is not None and search_unavailable() \
            and used[0] + len(UNAVAILABLE_NOTE) + 2 <= MAX_MATERIAL:
        blocks.append(UNAVAILABLE_NOTE)
    return '\n\n'.join(blocks), given


# ── The answer ───────────────────────────────────────────────────────────────

# What the answering model reads: earlier conversation, then the task, the material and the question.
def answer_messages(task: str, material_text: str, history: list, question: str) -> list:
    body = (f'The task: {task or question}\n\nThe material:\n'
            f'{material_text or "(Nothing was found in the meetings for this question.)"}\n\nQuestion: {question}')
    return [{'role': 'system', 'content': ANSWER_SYSTEM}, *_history_messages(history),
            {'role': 'user', 'content': body}]


# The answer with only references that can be true: a meeting that gave material and a second inside it.
# Dropped references take their [n] marker (with the space before it) out of the reply; the rest keep
# their numbers. Each kept reference is {'n', 'id', 'title', 'at'}.
def clean_answer(answer: dict, given: dict, titles: Optional[dict] = None) -> dict:
    answer = answer if isinstance(answer, dict) else {}
    reply = str(answer.get('reply') or '').strip() or '…'
    raw = answer.get('refs') if isinstance(answer.get('refs'), list) else []
    refs = []
    for n, ref in enumerate(raw, 1):
        if not isinstance(ref, dict) or not isinstance(ref.get('id'), str) or ref['id'] not in given \
                or isinstance(ref.get('at'), bool):
            continue
        try:
            at = float(ref['at'])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= at <= given[ref['id']]:            # also false for NaN
            continue
        refs.append({'n': n, 'id': ref['id'], 'title': (titles or {}).get(ref['id'], ''), 'at': int(at)})
    kept = {r['n'] for r in refs}
    reply = re.sub(r' ?\[(\d{1,3})\]', lambda m: m.group(0) if int(m.group(1)) in kept else '', reply)
    return {'reply': reply, 'refs': refs}


# ── One question ─────────────────────────────────────────────────────────────

# One call to the model: the JSON it answered. Raises SummaryError when the AI fails.
def _call(complete: Callable, parse: Callable, msgs: list):
    result = complete('meetings', msgs, json=True)
    if not result.ok:
        raise SummaryError(result.error or 'The AI did not answer')
    try:
        parsed = parse(result.text)
    except Exception:
        parsed = None                                     # llm.parse_json gives None, not an error, for prose
    return parsed if isinstance(parsed, dict) else {'reply': result.text}     # prose is still an answer


# Plans, gathers, answers: {'reply', 'refs'}. The cost is recorded for no meeting (feature "meeting_ask").
# Raises SummaryError when the question is empty or the AI fails.
def ask(question: str, history: list, heads: list, load: Callable[[str], dict], hits_for: Callable,
        complete: Callable, parse: Optional[Callable] = None,
        search_unavailable: Optional[Callable[[], bool]] = None) -> dict:
    question = str(question or '').strip()
    if not question:
        raise SummaryError('Write a question first')
    parse = parse or json.loads
    with for_meeting(None, 'meeting_ask'):
        plan = clean_plan(_call(complete, parse, plan_messages(meeting_list(heads), history, question)),
                          {h['id'] for h in heads})
        if not plan['search'] and not plan['open']:           # no usable plan: search for the question itself
            plan['search'] = [question]
        text, given = material(plan, question, load, hits_for, search_unavailable)
        answer = _call(complete, parse, answer_messages(plan['task'], text, history, question))
    return clean_answer(answer, given, {h['id']: h.get('title') or '' for h in heads})


__all__ = ['HISTORY', 'MAX_MATERIAL', 'ask', 'answer_messages', 'clean_answer', 'clean_plan', 'material',
           'meeting_list', 'plan_messages']
