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
SAY_MAX = 900                  # characters of anything she says on her tile (it scrolls, so nothing is cut)
LINE_MAX = 240                 # one note, decision, task or question

# What the board and the chat may carry, and nothing else.
PUBLIC_FIELDS = ('title', 'subtitle', 'topic', 'decisions', 'tasks', 'questions', 'say', 'say_kind', 'say_to')
SAY_KINDS = ('answer', 'chime')   # an answer to someone, or something she adds on her own

# How Whisper and Meet's captions write "Koli".
ALIASES = ('koli', 'kolie', 'coley', 'collie', 'kohli', 'קולי')
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
# seconds: [{'at', 'name', 'you', 'text', 'heard'}]. Names are looked up
# now, so a speaker renamed a minute ago already has the new name.
# `heard`: everyone in the call heard it. Other people's words always were.
# Yours when the turn is marked so (Meet captioned it), else what `heard`
# answers for the turn: True, False, or None when it can't be known yet
# (was your microphone on in Meet? Koli hears it muted too). With no
# `heard` to ask, they weren't. `heard_only` keeps the rows that were: what
# anything shown or said to the call is made from. `index` is the turn's
# place; `captioned`: the turn is a finished caption of Meet's.
def lines(m: dict, first: int = 0, since: float = 0.0, heard: Optional[Callable] = None,
          heard_only: bool = False) -> list:
    names = {s['id']: s.get('name') or s['id'] for s in (m or {}).get('speakers', [])}
    rows = []
    first = max(0, int(first))
    for index, t in enumerate(((m or {}).get('turns') or [])[first:], start=first):
        text = ' '.join(str(t.get('text') or '').split())
        if not text or t.get('start', 0) < since:
            continue
        speaker = t.get('speaker')
        you = speaker == 'you'
        if not you or t.get('heard'):
            was_heard = True
        else:
            was_heard = heard(t) if heard else False
        if heard_only and was_heard is not True:
            continue
        name = 'You' if you else names.get(speaker) or ('Speaker ?' if speaker == '?' else str(speaker or 'Someone'))
        rows.append({'at': t.get('start', 0), 'name': name, 'you': you, 'text': text, 'heard': was_heard, 'index': index,
                     'captioned': bool(t.get('heard'))})
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
         "Write every text in {language}, the language the meeting is spoken in, but keep the JSON "
         "field names exactly as written here, in English. "
         "\"You\" in the transcript is the person you work for.{owner} Answer with one JSON object only.")
ATTEMPTS = 2                   # an answer that can't be used is asked for once more (a small model slips now and then)

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

TALK_PROMPT = """You are Koli, an AI assistant sitting in a video call as a participant of your own. You have no voice yet: what you answer appears as text on your video tile, for everyone in the call to read.{owner}
Words that may be for you were just said. You get them as speech-to-text wrote them: some words are garbled, there is no punctuation, and your name may be written Coley, Collie or קולי.

First decide which of three things to do, and do exactly one:

1. [wait] - reply with exactly this when the sentence is not finished yet: it stops in the middle, or it only announces that something is coming, or it is only your name. An answer now would cut in. Never fill the gap with "I'm listening", "go ahead" or "yes?": that is [wait].
   "I have a few questions for Koli" -> [wait]
   "Koli in which year" -> [wait]
   "what would you suggest I do for" -> [wait]

2. [skip] - reply with exactly this when the words are not for you. Among other people, speak only when someone speaks to you. Also when you are alone with one person, they may be talking to somebody in their room or on the phone: a request only a person in the room could do (come here, look at my screen, are you at your computer, bring me something) is not for you. When you can't tell whether it is for you, [skip].
   "come here for two minutes before I go to sleep" -> [skip]
   "hi Viktor how are you" -> [skip]
   "so I think we should ship on Thursday" -> [skip]

3. Otherwise answer: directly, in at most three short sentences, in the language they spoke. Use what was said in this meeting, and your general knowledge for general questions. You know nothing about these people beyond this meeting: never guess private details about anyone. If the words are for you but you can't make out what is asked, say so in a few words and ask them to say it again. Plain words only: no markdown, no lists, no quotation marks around the answer.
   "Koli in which year was the state of Israel founded" -> In 1948.

When you are told the speaker has stopped talking, [wait] is no longer possible: answer what you can make of it, or [skip]."""

# Added to the talk prompt when she may look things up.
WEB_PART = """

You can search the web. Do it for things that change or that you can't know: the weather, news, prices, opening hours, results, who holds a role now. Don't search for what the meeting itself answers or for plain general knowledge. Say the answer itself, briefly, with no links and no list of sources."""


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


# One question to the model: the system prompt, the parts as one user
# message, a JSON object back. `usable(answer)` says whether it holds what
# was asked for; one that doesn't (or isn't JSON) is asked for once more,
# then `problem` is raised. A model that fails outright is not asked again.
# `owner` is the name other people call "You" by.
def _ask_model(prompt: str, parts: list, language: str, complete: Callable, parse: Optional[Callable],
               usable: Callable[[dict], bool], problem: str, owner: str = '') -> dict:
    owner = _text(owner, 40)
    who = f' "You" is {owner}: when someone says that name, they mean "You".' if owner else ''
    system = prompt.format(language=language_name(language), ask='', owner=who)
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': '\n\n'.join(p for p in parts if p)}]
    for _ in range(ATTEMPTS):
        result = complete(FEATURE, messages, json=True)
        if not getattr(result, 'ok', False):
            raise CompanionError(getattr(result, 'error', '') or 'The AI did not answer')
        try:
            answer = (parse or _parse)(result.text)
        except Exception:
            answer = None
        if isinstance(answer, dict) and usable(answer):
            return answer
    raise CompanionError(problem)


def _notes_part(notes: dict) -> str:
    shown = {k: v for k, v in (notes or empty_notes()).items() if k != 'ask'}
    return "The notes so far (JSON):\n" + json.dumps(shown, ensure_ascii=False)


# The notes brought up to date with the new `rows`. With no new lines the
# notes come back as they are and nothing is asked.
def update(notes: dict, rows: list, mode: str, language: str, complete: Callable, parse: Optional[Callable] = None,
           owner: str = '') -> dict:
    if not rows:
        return notes
    prompt = NOTES_PROMPT.replace('{ask}', ASK_PART.replace('{', '{{').replace('}', '}}') if mode == 'active' else '')
    # {} or {"error": ...} is JSON too: without a topic it isn't the notes, and
    # taking it would empty them, and her board in the call with them. A field
    # the model left out keeps what it held.
    answer = _ask_model(prompt, [_notes_part(notes), "New lines:\n" + transcript(rows)], language, complete, parse,
                        usable=lambda a: isinstance(a.get('topic'), str), problem="The AI's answer was not the notes",
                        owner=owner)
    kept = {field: (notes or {}).get(field) for field in ('summary', 'decisions', 'tasks', 'questions')}
    return clean_notes({**kept, **answer}, mode)


# Something to say now: {'question', 'answer', 'points'}. `past` is text
# from the owner's earlier meetings; it is for the owner's eyes only.
def answer(notes: dict, rows: list, language: str, complete: Callable, parse: Optional[Callable] = None,
           past: str = '', owner: str = '') -> dict:
    parts = [_notes_part(notes), "The last minutes:\n" + transcript(rows)]
    if str(past or '').strip():
        parts.append("From their earlier meetings, to use only if it bears on the question:\n" + str(past).strip())
    got = _ask_model(ANSWER_PROMPT, parts, language, complete, parse, usable=lambda a: bool(_text(a.get('answer'))),
                     problem="The AI had no answer to suggest", owner=owner)
    return {'question': _text(got.get('question')), 'answer': _text(got.get('answer'), 600),
            'points': _texts(got.get('points'), MAX_POINTS)}


# The last minutes in up to three lines: {'lines'}.
def catch_up(rows: list, language: str, complete: Callable, parse: Optional[Callable] = None) -> dict:
    if not rows:
        raise CompanionError("Nothing was said in the last minutes")
    got = _ask_model(CATCH_UP_PROMPT, ["The last minutes:\n" + transcript(rows)], language, complete, parse,
                     usable=lambda a: bool(_texts(a.get('lines'), MAX_SUMMARY)), problem="The AI had nothing to report")
    return {'lines': _texts(got.get('lines'), MAX_SUMMARY)}


# The task being talked about just now: {'task', 'owner'}.
def task_from_talk(rows: list, language: str, complete: Callable, parse: Optional[Callable] = None) -> dict:
    if not rows:
        raise CompanionError("Nothing was said in the last minutes")
    got = _ask_model(TASK_PROMPT, ["The last minute and a half:\n" + transcript(rows)], language, complete, parse,
                     usable=lambda a: bool(_text(a.get('task'))), problem="The AI found no task in what was just said")
    return {'task': _text(got.get('task')), 'owner': _text(got.get('owner'), 40) or None}


# Of the notes, only what her board shows anyway: not the summary lines, not
# a question put to the owner or the answer suggested to him.
def _public_notes_part(notes: dict) -> str:
    shown = public_view(notes, '')
    return "The notes so far (JSON):\n" + json.dumps(
        {field: shown[field] for field in ('topic', 'decisions', 'tasks', 'questions')}, ensure_ascii=False)


# The words on her tile: one line of plain text, without the marks a model
# likes to add, cut to what a tile holds. A model that searched the web adds
# its sources whatever it is told: "([site](https://…))" goes, a link keeps
# its words, a bare address goes, and so does half a link at the end while
# the words are still coming in.
def tidy_say(text: str) -> str:
    text = str(text or '').replace('**', '').replace('`', '')
    text = re.sub(r'\s*\(\s*\[[^\]]*\]\([^)]*\)\s*\)', '', text)     # ([source](address))
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)             # [words](address)
    text = re.sub(r'\s*\(?\[[^\]]*(\]\([^)]*)?$', '', text)          # half a link at the end
    text = re.sub(r'\s*https?://\S+', '', text)                      # a bare address
    text = ' '.join(text.split())
    if len(text) >= 2 and text[0] == text[-1] and text[0] in '"\'':
        text = text[1:-1].strip()
    return text[:SAY_MAX]


# What Koli says back to someone in the call, if anything: ('answer', her
# words), ('wait', '') when the speaker only paused in the middle of a
# sentence, ('skip', '') when the words aren't for her. The model judges:
# a pause in a caption is not the end of a sentence, and words said near
# her are not always said to her. `stopped`: the speaker has gone quiet, so
# there is nothing to wait for. `alone`: the call is just her and the speaker.
# An answer goes on her tile for everyone, so the model gets only the
# public part of the notes, the rows the call heard (the caller passes
# lines(..., heard_only=True)) and the talk with her so far ([{'who',
# 'text'}], her own answers among them: they are in no transcript).
# `on_text(piece)` gets the words as the model writes them. Plain text.
# `web`: she may search the web for the answer (weather, news); a model
# whose provider can't search answers from what it knows.
def reply(notes: dict, rows: list, talk: list, who: str, text: str, language: str, complete: Callable,
          on_text: Optional[Callable] = None, owner: str = '', alone: bool = False, stopped: bool = True,
          web: bool = False) -> tuple:
    owner = _text(owner, 40)
    who = _text(who, 40) or 'someone'
    system = TALK_PROMPT.format(owner=f' You work for {owner}.' if owner else '') + (WEB_PART if web else '')
    parts = [_public_notes_part(notes)]
    if rows:
        parts.append("The last minutes of the meeting:\n" + transcript(rows))
    if talk:
        parts.append("Your talk with them so far:\n" + '\n'.join(f"{t['who']}: {t['text']}" for t in talk))
    parts.append("The situation: "
                 + (f"there are only you and {who} in the call. " if alone else "there are other people in the call. ")
                 + (f"{who} has stopped talking." if stopped else f"{who} has paused; more words may still come."))
    parts.append(f"Said to you now by {who}:\n{_text(text, 600)}")
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': '\n\n'.join(parts)}]
    result = complete(FEATURE, messages, json=False, on_text=on_text, **({'web': True} if web else {}))
    if not getattr(result, 'ok', False):
        raise CompanionError(getattr(result, 'error', '') or 'The AI did not answer')
    first = str(result.text or '').strip().lower()
    if first.startswith('[wait]'):
        return ('skip', '') if stopped else ('wait', '')     # nobody is left to wait for
    if first.startswith('[skip]'):
        return ('skip', '')
    said = tidy_say(result.text)
    if not said:
        raise CompanionError("The AI had no answer")
    return ('answer', said)


# ── What others may see ──────────────────────────────────────────────────────

# The view of the notes that the board in the call shows. Built field by
# field from PUBLIC_FIELDS: the summary lines, a question put to the owner
# and the answer suggested to him stay out.
# `say` is what she is saying on her tile now, `say_kind` whether it answers
# someone (`say_to`, a name from the call) or is a note of her own.
def public_view(notes: dict, owner: str, name: str = 'Koli', say: str = '', say_kind: str = '', say_to: str = '') -> dict:
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
            'tasks': tasks, 'questions': _texts(notes.get('questions'), MAX_QUESTIONS), 'say': _text(say, SAY_MAX),
            'say_kind': say_kind if say_kind in SAY_KINDS else '', 'say_to': _text(say_to, 40)}
    return {field: view[field] for field in PUBLIC_FIELDS}


# Whether a line names Koli: her name as a word of its own, or a way
# speech-to-text writes it. That is enough for a look; whether the line is
# really for her ("Koli, give me a list" or "I told Koli yesterday") is the
# model's call in reply().
def spoken_to(text: str, name: str = 'Koli') -> bool:
    name = (name or 'Koli').strip().lower()
    names = set(ALIASES) if name == 'koli' else {name}
    return any(re.search(rf'(?<!\w){re.escape(alias)}(?!\w)', str(text or ''), re.IGNORECASE) for alias in names)


__all__ = ['ALIASES', 'CompanionError', 'FEATURE', 'MAX_DECISIONS', 'MAX_QUESTIONS', 'MAX_SUMMARY', 'MAX_TASKS',
           'MIN_NEW_WORDS', 'MODES', 'NEW_TEXT_CHARS', 'PUBLIC_FIELDS', 'SAY_MAX', 'answer', 'catch_up', 'clean_notes',
           'empty_notes', 'lines', 'public_view', 'reply', 'spoken_to', 'task_from_talk', 'tidy_say', 'transcript', 'update',
           'words']
