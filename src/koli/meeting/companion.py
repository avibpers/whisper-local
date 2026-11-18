# meeting/companion.py
# Koli's head while a meeting runs: short notes kept up to date (the topic
# now, decisions, tasks, open questions), an answer you could give, a catch-up
# of the last minutes, the task being talked about, and the one view of the
# notes that people in the call may see. Pure: it takes the meeting and an AI
# function and returns data; services/companion.py runs it.
# Design: docs/design/meet-participant.md.

import json
import re
from typing import Callable, Optional

from .summarize import language_name

MODES = ('off', 'insights', 'active')
FEATURE = 'companion'          # the AI route: ai.companion.model, else the main model

MAX_SUMMARY = 3
MAX_DECISIONS = 8
MAX_TASKS = 10
MAX_QUESTIONS = 5
MAX_POINTS = 3
NEW_TEXT_CHARS = 6000          # the most transcript one call carries: the newest lines win
MIN_NEW_WORDS = 25             # fewer new words than this aren't worth a call
SAY_MAX = 400                  # characters of anything said to the whole call
LINE_MAX = 240                 # one note, decision, task or question

# What the board and the chat may carry, and nothing else.
PUBLIC_FIELDS = ('title', 'subtitle', 'topic', 'decisions', 'tasks', 'questions', 'say')

# How Whisper and Meet's captions write "Koli".
ALIASES = ('koli', 'kolie', 'coley', 'collie', 'kohli', 'קולי')
ASKING = re.compile(r"\?|\b(what|when|who|where|why|how|which|can you|could you|would you|do you|please)\b"
                    r"|(?<!\w)(מה|מתי|מי|איפה|למה|איך|האם|תוכלי|אפשר|בבקשה)(?!\w)", re.IGNORECASE)
RTL = re.compile(r'[֐-׿؀-ۿ]')


class CompanionError(Exception):
    """The AI did not give what was asked; the message is for the user."""


def empty_notes() -> dict:
    return {'topic': '', 'summary': [], 'decisions': [], 'tasks': [], 'questions': [], 'ask': None}


# ── The meeting as lines ─────────────────────────────────────────────────────

def _clock(seconds) -> str:
    s = max(0, int(seconds or 0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# The meeting's turns from index `first` on, said at or after `since`
# seconds: [{'at', 'name', 'you', 'text'}]. Names are looked up now, so a
# speaker renamed a minute ago already has the new name.
def lines(m: dict, first: int = 0, since: float = 0.0) -> list:
    names = {s['id']: s.get('name') or s['id'] for s in (m or {}).get('speakers', [])}
    rows = []
    for t in ((m or {}).get('turns') or [])[max(0, int(first)):]:
        text = ' '.join(str(t.get('text') or '').split())
        if not text or t.get('start', 0) < since:
            continue
        speaker = t.get('speaker')
        you = speaker == 'you'
        name = 'You' if you else names.get(speaker) or ('Speaker ?' if speaker == '?' else str(speaker or 'Someone'))
        rows.append({'at': t.get('start', 0), 'name': name, 'you': you, 'text': text})
    return rows


# `[m:ss] Name: text` per line. Over the limit, the oldest lines go.
def transcript(rows: list, limit: int = NEW_TEXT_CHARS) -> str:
    out, size = [], 0
    for row in reversed(rows or []):
        line = f"[{_clock(row['at'])}] {row['name']}: {row['text']}"
        if size + len(line) + 1 > limit and out:
            break
        out.append(line[:limit])
        size += len(line) + 1
    return '\n'.join(reversed(out))


def words(rows: list) -> int:
    return sum(len(r['text'].split()) for r in rows or [])


# ── Tidying what the model wrote ─────────────────────────────────────────────

def _text(value, limit: int = LINE_MAX) -> str:
    return ' '.join(value.split())[:limit] if isinstance(value, str) else ''


def _texts(value, most: int) -> list:
    out = [_text(v) for v in value] if isinstance(value, list) else []
    return [v for v in out if v][:most]


def _tasks(value) -> list:
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            item = {'task': item}
        if not isinstance(item, dict):
            continue
        task = _text(item.get('task'))
        if task:
            out.append({'task': task, 'owner': _text(item.get('owner'), 40) or None})
    return out[:MAX_TASKS]


def _ask(value) -> Optional[dict]:
    if not isinstance(value, dict) or not _text(value.get('question')):
        return None
    return {'who': _text(value.get('who'), 40), 'question': _text(value.get('question')),
            'for_you': bool(value.get('for_you')), 'answer': _text(value.get('answer'), 600),
            'points': _texts(value.get('points'), MAX_POINTS)}


# Only the fields notes have, each within its limit. A question to answer
# ("ask") is kept in Active mode only.
def clean_notes(raw, mode: str) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {'topic': _text(raw.get('topic')),
            'summary': _texts(raw.get('summary'), MAX_SUMMARY),
            'decisions': _texts(raw.get('decisions'), MAX_DECISIONS),
            'tasks': _tasks(raw.get('tasks')),
            'questions': _texts(raw.get('questions'), MAX_QUESTIONS),
            'ask': _ask(raw.get('ask')) if mode == 'active' else None}


# ── Asking the model ─────────────────────────────────────────────────────────

RULES = ("Use this meeting only: nothing you know from elsewhere, nothing invented. "
         "Write every text in {language}, the language the meeting is spoken in. "
         "\"You\" in the transcript is the person you work for. Answer with one JSON object only.")

NOTES_PROMPT = """You are Koli, an assistant sitting in a live meeting. You keep short running notes.
You get the notes so far (JSON) and the new lines of the transcript since then, as [time] Name: words.
Return the notes brought up to date, as JSON with exactly these fields:
- "topic": what is being discussed right now, one short line.
- "summary": up to 3 short lines on where the meeting stands.
- "decisions": up to 8 things that were decided, each one line. Keep earlier ones unless they were reversed.
- "tasks": up to 10 things someone took on or was asked to do, each {{"task": "...", "owner": "name or null"}}.
- "questions": up to 5 questions raised and still open.{ask}
Keep what is still true from the notes so far, change what the new lines change, drop what was settled.
""" + RULES

ASK_PART = """
- "ask": if in the new lines someone other than "You" asked a question that nobody has answered by the last line, {"who": "their name", "question": "the question", "for_you": true when it was put to "You" (by name or as the only other person), "answer": "two or three sentences \\"You\\" could say, from what the meeting said", "points": up to 3 short facts from the meeting behind that answer}; otherwise null."""

ANSWER_PROMPT = """You are Koli, an assistant sitting in a live meeting with the person you work for ("You").
You get the notes so far and the last minutes of the transcript. They ask: what should I answer?
Find what is being asked of them or argued right now, and give them something to say.
Return JSON: {{"question": "what is being asked or decided, one line", "answer": "two or three sentences they could say, in the first person", "points": up to 3 short facts behind it}}.
""" + RULES

CATCH_UP_PROMPT = """You are Koli, an assistant sitting in a live meeting. The person you work for looked away and asks to be caught up.
You get the last minutes of the transcript. Return JSON: {{"lines": up to 3 short lines saying what happened, the most important first}}.
""" + RULES

TASK_PROMPT = """You are Koli, an assistant sitting in a live meeting. The person you work for pressed "add a task".
You get the last minute and a half of the transcript. Find the one task being talked about just now.
Return JSON: {{"task": "the task, one line, starting with a verb", "owner": "who will do it, or null"}}.
""" + RULES

PUBLIC_PROMPT = """You are Koli, an AI assistant taking notes in a meeting. Someone in the meeting spoke to you.
Your answer is shown to everyone in the call, so it must hold nothing but what was said in this meeting.
You get the notes so far, the last minutes of the transcript, and what was said to you.
Return JSON: {{"answer": "your answer, at most three short sentences"}}. If the meeting doesn't tell, say so in one sentence.
""" + RULES


# A JSON object out of the model's words: plain, in a code fence, or inside prose.
def _parse(text: str):
    text = (text or '').strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


# One call: the system prompt, the parts as one user message, a JSON object back.
def _ask_model(prompt: str, parts: list, language: str, complete: Callable, parse: Optional[Callable]) -> dict:
    system = prompt.format(language=language_name(language), ask='')
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': '\n\n'.join(p for p in parts if p)}]
    result = complete(FEATURE, messages, json=True)
    if not getattr(result, 'ok', False):
        raise CompanionError(getattr(result, 'error', '') or 'The AI did not answer')
    try:
        answer = (parse or _parse)(result.text)
    except Exception:
        answer = None
    if not isinstance(answer, dict):
        raise CompanionError("The AI's answer could not be read")
    return answer


def _notes_part(notes: dict) -> str:
    shown = {k: v for k, v in (notes or empty_notes()).items() if k != 'ask'}
    return "The notes so far (JSON):\n" + json.dumps(shown, ensure_ascii=False)


# The notes brought up to date with the new `rows`. With no new lines the
# notes come back as they are and nothing is asked.
def update(notes: dict, rows: list, mode: str, language: str, complete: Callable, parse: Optional[Callable] = None) -> dict:
    if not rows:
        return notes
    prompt = NOTES_PROMPT.replace('{ask}', ASK_PART.replace('{', '{{').replace('}', '}}') if mode == 'active' else '')
    answer = _ask_model(prompt, [_notes_part(notes), "New lines:\n" + transcript(rows)], language, complete, parse)
    # {} or {"error": ...} is JSON too: without a topic it isn't the notes, and
    # taking it would empty them, and her board in the call with them. A field
    # the model left out keeps what it held.
    if not isinstance(answer.get('topic'), str):
        raise CompanionError("The AI's answer was not the notes")
    kept = {field: (notes or {}).get(field) for field in ('summary', 'decisions', 'tasks', 'questions')}
    return clean_notes({**kept, **answer}, mode)


# Something to say now: {'question', 'answer', 'points'}. `past` is text
# from the owner's earlier meetings; it is for the owner's eyes only.
def answer(notes: dict, rows: list, language: str, complete: Callable, parse: Optional[Callable] = None,
           past: str = '') -> dict:
    parts = [_notes_part(notes), "The last minutes:\n" + transcript(rows)]
    if str(past or '').strip():
        parts.append("From their earlier meetings, to use only if it bears on the question:\n" + str(past).strip())
    got = _ask_model(ANSWER_PROMPT, parts, language, complete, parse)
    said = _text(got.get('answer'), 600)
    if not said:
        raise CompanionError("The AI had no answer to suggest")
    return {'question': _text(got.get('question')), 'answer': said, 'points': _texts(got.get('points'), MAX_POINTS)}


# The last minutes in up to three lines: {'lines'}.
def catch_up(rows: list, language: str, complete: Callable, parse: Optional[Callable] = None) -> dict:
    if not rows:
        raise CompanionError("Nothing was said in the last minutes")
    got = _ask_model(CATCH_UP_PROMPT, ["The last minutes:\n" + transcript(rows)], language, complete, parse)
    found = _texts(got.get('lines'), MAX_SUMMARY)
    if not found:
        raise CompanionError("The AI had nothing to report")
    return {'lines': found}


# The task being talked about just now: {'task', 'owner'}.
def task_from_talk(rows: list, language: str, complete: Callable, parse: Optional[Callable] = None) -> dict:
    if not rows:
        raise CompanionError("Nothing was said in the last minutes")
    got = _ask_model(TASK_PROMPT, ["The last minute and a half:\n" + transcript(rows)], language, complete, parse)
    task = _text(got.get('task'))
    if not task:
        raise CompanionError("The AI found no task in what was just said")
    return {'task': task, 'owner': _text(got.get('owner'), 40) or None}


# What Koli answers when someone in the call speaks to her. It goes to
# everyone, so the model sees this meeting only, never the owner's others.
def public_answer(notes: dict, rows: list, question: str, language: str, complete: Callable,
                  parse: Optional[Callable] = None) -> str:
    got = _ask_model(PUBLIC_PROMPT, [_notes_part(notes), "The last minutes:\n" + transcript(rows),
                                     "What was said to you:\n" + _text(question, 600)], language, complete, parse)
    said = _text(got.get('answer'), SAY_MAX)
    if not said:
        raise CompanionError("The AI had no answer")
    return said


# ── What others may see ──────────────────────────────────────────────────────

# The view of the notes that the board in the call shows. Built field by
# field from PUBLIC_FIELDS: the summary lines, a question put to the owner
# and the answer suggested to him stay out.
def public_view(notes: dict, owner: str, name: str = 'Koli', say: str = '') -> dict:
    notes = notes if isinstance(notes, dict) else {}
    topic = _text(notes.get('topic'))
    tasks = [f"{t['task']} ({t['owner']})" if t.get('owner') else t['task'] for t in _tasks(notes.get('tasks'))]
    decisions = _texts(notes.get('decisions'), MAX_DECISIONS)
    owner = _text(owner, 40)
    rtl = bool(RTL.search(topic or (decisions + tasks + [''])[0]))
    if rtl:
        subtitle = f"סיכום AI · עם {owner}" if owner else "סיכום AI"
    else:
        subtitle = f"AI notes · with {owner}" if owner else "AI notes"
    view = {'title': _text(name, 40) or 'Koli', 'subtitle': subtitle, 'topic': topic, 'decisions': decisions,
            'tasks': tasks, 'questions': _texts(notes.get('questions'), MAX_QUESTIONS), 'say': _text(say, SAY_MAX)}
    return {field: view[field] for field in PUBLIC_FIELDS}


# Whether a line is said to Koli: her name as a word of its own (or a way
# speech-to-text writes it), in a line that asks for something.
def spoken_to(text: str, name: str = 'Koli') -> bool:
    text = str(text or '')
    name = (name or 'Koli').strip().lower()
    names = set(ALIASES) if name == 'koli' else {name}
    found = None
    for alias in names:
        found = re.search(rf'(?<!\w){re.escape(alias)}(?!\w)', text, re.IGNORECASE)
        if found:
            break
    if not found:
        return False
    # Her name first is someone turning to her; otherwise the line must ask.
    return found.start() <= 2 or bool(ASKING.search(text))


__all__ = ['ALIASES', 'CompanionError', 'FEATURE', 'MAX_DECISIONS', 'MAX_QUESTIONS', 'MAX_SUMMARY', 'MAX_TASKS',
           'MIN_NEW_WORDS', 'MODES', 'NEW_TEXT_CHARS', 'PUBLIC_FIELDS', 'SAY_MAX', 'answer', 'catch_up', 'clean_notes',
           'empty_notes', 'lines', 'public_answer', 'public_view', 'spoken_to', 'task_from_talk', 'transcript', 'update',
           'words']
