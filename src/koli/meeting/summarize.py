# meeting/summarize.py
# Writes a meeting's brief with the AI model, from a summary template: the
# template's prompt as the system message, the meeting as text, JSON back,
# then checked and tidied before it is saved. Also drafts new templates from
# a description or from past summaries. Design: docs/design/meeting-summary.md.

import datetime
import json
import logging
import re
from collections import Counter
from typing import Callable, Optional

from .summary_templates import BLOCKS, TemplateError, TemplateLibrary, parse_text, problems

logger = logging.getLogger(__name__)

LANGUAGES = {
    'he': 'Hebrew', 'en': 'English', 'ar': 'Arabic', 'ru': 'Russian', 'fr': 'French', 'es': 'Spanish',
    'de': 'German', 'it': 'Italian', 'pt': 'Portuguese', 'nl': 'Dutch', 'uk': 'Ukrainian', 'pl': 'Polish',
    'tr': 'Turkish', 'zh': 'Chinese', 'ja': 'Japanese', 'ko': 'Korean', 'hi': 'Hindi', 'am': 'Amharic',
}
LIST_FIELDS = ('tldr', 'decisions', 'action_items', 'open_questions', 'parked', 'next_meeting', 'agenda',
               'topics', 'sections', 'notes', 'attendees')
STATUSES = ('do_now', 'plan_only', 'waiting', 'done')
MIN_TURNS = 4


class SummaryError(RuntimeError):
    pass


def language_name(code: str) -> str:
    return LANGUAGES.get((code or '').lower()[:2], code or 'English')


# The language most of the meeting was spoken in, by words: each turn's own
# language when Whisper gave one, else Hebrew letters or not.
def meeting_language(m: dict) -> str:
    votes = Counter()
    for t in m.get('turns', []):
        text = t.get('text') or ''
        lang = (t.get('lang') or '').lower()[:2]
        if not lang:
            lang = 'he' if len(re.findall(r'[֐-׿]', text)) > len(text) / 4 else 'en'
        votes[lang] += len(text.split())
    return votes.most_common(1)[0][0] if votes else 'en'


# First that applies: asked now, the template's language, the setting, the meeting's.
def choose_language(m: dict, template: dict, asked: str = '', setting: str = '') -> str:
    for code in (asked, template.get('language'), setting):
        code = str(code or '').strip().lower()
        if code and code != 'meeting':
            return code
    return meeting_language(m)


def _clock(s) -> str:
    s = max(0, int(s or 0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _length(m: dict) -> float:
    try:
        start = datetime.datetime.fromisoformat(m['started_at'])
        end = datetime.datetime.fromisoformat(m['ended_at'])
        return max(0.0, (end - start).total_seconds())
    except (KeyError, TypeError, ValueError):
        return max((t.get('end', 0) for t in m.get('turns', [])), default=0)


# The meeting as the model reads it: who is who, then every turn with its
# second from the start, and the screenshots at their moments.
def meeting_text(m: dict) -> str:
    names = {s['id']: s.get('name') or s['id'] for s in m.get('speakers', [])}
    lines = [f"Meeting: {m.get('title') or 'Meeting'}",
             f"Started: {m.get('started_at') or 'unknown'}",
             f"Length: {_clock(_length(m))}",
             "Speakers (id: name as Koli knows it; \"You\" is the person who recorded the meeting):"]
    lines += [f"- {sid}: {name}" for sid, name in names.items()]
    lines.append('')
    lines.append('Transcript, one turn per line as [seconds from the start] speaker id (name): words.')
    rows = [(t['start'], 0, f"[{int(t['start'])}] {t['speaker']} ({names.get(t['speaker'], t['speaker'])}): {t['text']}")
            for t in m.get('turns', [])]
    rows += [(s['at'], 1, f"[{int(s['at'])}] SCREENSHOT: {s.get('caption') or 'no caption'}")
             for s in m.get('screenshots', [])]
    lines += [r[2] for r in sorted(rows)]
    return '\n'.join(lines)


def messages(m: dict, template: dict, library: TemplateLibrary, language: str) -> list:
    return [{'role': 'system', 'content': library.prompt(template, f"{language_name(language)} ({language})")},
            {'role': 'user', 'content': meeting_text(m)}]


# ── Checking the answer ──────────────────────────────────────────────────────

def _items(value, prefix: str, text_key: str = 'text') -> list:
    out = []
    for i, x in enumerate(value if isinstance(value, list) else [], 1):
        if isinstance(x, str):
            x = {text_key: x}
        if not isinstance(x, dict) or not str(x.get(text_key) or '').strip():
            continue
        x.setdefault('id', f'{prefix}{i}')
        if 'at' in x and not isinstance(x['at'], (int, float)):
            x['at'] = None
        out.append(x)
    return out


def _actions(value) -> list:
    out = _items(value, 'A', 'task')
    for a in out:
        if a.get('status') not in STATUSES:
            a['status'] = 'do_now'
        for key in ('owner', 'due'):
            if not a.get(key):
                a[key] = None
    return out


def tidy(answer, m: dict, template: dict, generator: str, language: str) -> dict:
    if not isinstance(answer, dict):
        raise SummaryError("The AI didn't answer with a summary (no JSON object)")
    b = dict(answer)
    b['version'] = 1
    b['template'] = template['id']
    b['generator'] = generator
    b.setdefault('language', language)
    for key in LIST_FIELDS:
        if key in b and not isinstance(b[key], list):
            b[key] = [b[key]] if b[key] else []
    b['tldr'] = [str(p) for p in b.get('tldr') or [] if str(p).strip()]
    b['notes'] = [str(p) for p in b.get('notes') or [] if str(p).strip()]
    b['decisions'] = _items(b.get('decisions'), 'D')
    b['action_items'] = _actions(b.get('action_items'))
    b['open_questions'] = _items(b.get('open_questions'), 'Q')
    b['parked'] = _items(b.get('parked'), 'P')
    b['next_meeting'] = _items(b.get('next_meeting'), 'N')
    b['sections'] = _items(b.get('sections'), 'S', 'title')
    b['topics'] = _items(b.get('topics'), 'T', 'title')
    agenda = []
    for i, a in enumerate(_items(b.get('agenda'), '', 'title'), 1):
        a['id'] = str(a.get('id') or i)
        a['discussion'] = [str(x) for x in a.get('discussion') or [] if str(x).strip()]
        a['decisions'] = _items(a.get('decisions'), 'D')
        a['action_items'] = _actions(a.get('action_items'))
        agenda.append(a)
    b['agenda'] = agenda
    ids = {s['id'] for s in m.get('speakers', [])}
    b['participants'] = {k: str(v) for k, v in (b.get('participants') or {}).items() if k in ids and v} \
        if isinstance(b.get('participants'), dict) else {}
    b['roles'] = {k: str(v) for k, v in (b.get('roles') or {}).items() if k in ids and v} \
        if isinstance(b.get('roles'), dict) else {}
    # A fixed-title template keeps one of its own titles.
    spec = template.get('title') or {}
    if isinstance(spec, dict) and spec.get('mode') == 'fixed':
        choices = spec.get('choices') or []
        if choices and b.get('title') not in choices:
            b['title'] = choices[0]
    if not str(b.get('title') or '').strip():
        b['title'] = m.get('title') or 'Meeting'
    if not b.get('date'):
        b['date'] = (m.get('started_at') or '')[:10 if template.get('date') == 'date' else 16]
    if not (b['tldr'] or b['decisions'] or b['action_items'] or b['agenda'] or b['topics'] or b['sections']):
        raise SummaryError('The AI answered with an empty summary')
    return b


# Enough of a meeting to summarize: a few turns of real talk.
def worth_summarizing(m: dict, min_minutes: float) -> bool:
    turns = m.get('turns', [])
    return len(turns) >= MIN_TURNS and _length(m) >= float(min_minutes or 0) * 60


def summarize(m: dict, library: TemplateLibrary, template_id: str, complete: Callable,
              language: str = '', language_setting: str = '', parse: Callable = None) -> dict:
    if not m.get('turns'):
        raise SummaryError('Nothing was said in this meeting, so there is nothing to summarize')
    template = library.get(template_id)
    lang = choose_language(m, template, language, language_setting)
    result = complete('meetings', messages(m, template, library, lang), json=True)
    if not result.ok:
        raise SummaryError(result.error or 'The AI did not answer')
    answer = (parse or json.loads)(result.text)
    model = '/'.join(p for p in (getattr(result, 'provider', ''), getattr(result, 'model', '')) if p)
    generator = f"{model} via Koli ({template.get('name')})" if model else f"Koli ({template.get('name')})"
    return tidy(answer, m, template, generator, lang)


# ── Drafting templates with the AI ───────────────────────────────────────────

DRAFT_GUIDE = """You write summary templates for Koli, a meeting transcription app. A template is ONE Markdown file:

1. A YAML header between two lines of "---":
   id: lowercase-with-hyphens
   name: a short name people see in a list
   description: one sentence on what this summary is for and what it holds
   language: meeting            (or an ISO code when the user wants one language always)
   title: {mode: generated}     (or {mode: fixed, choices: ["Exact Title"]} when every summary has the same title)
   date: date                   (date only) or datetime
   blocks: [the sections in order, from the list below]
   labels: {block: "Heading"}   (only to rename headings; use the user's own heading names)

2. Below the header, the instructions an AI model follows to turn a meeting transcript into this summary:
   a paragraph on the purpose and reader, the structure section by section, a line with {{common_rules}},
   then "## What to return" with a ```json example of the full answer using only the fields the blocks need.

Blocks and the JSON fields they show:
- header: "title", "date"
- tldr: "tldr": [short paragraphs]
- participants: "attendees": [{"name", "role"}]
- agenda, agenda_details: "agenda": [{"id": "1", "title", "discussion": [bullets], "decisions": [{"id","text","at"}], "action_items": [{"id","task","owner","status","due","at"}]}]
- decisions: "decisions": [{"id","text","at"}]
- action_items: "action_items": [{"id","task","owner","status","due","at"}] (status: do_now, plan_only, waiting, done)
- lists / open_questions / parked: "open_questions", "parked": [{"id","text","at"}]
- next_meeting: "next_meeting": [{"id","text","at"}]
- speakers: talk-time charts (no fields; "roles": {speaker id: one line} adds a role column)
- screenshots: what was on screen (no fields)
- topics: "topics": [{"id","title","start","end","led_by","lead","points": [..],"quotes": [{"text","who","at"}]}]
- sections: "sections": [{"id","title","items": [bullets],"text": [paragraphs]}] for anything else, asked for by exact title
- notes: "notes": [transcript problems]
- data: the summary as JSON (add it last)
Every JSON answer also has "version": 1, "template": "<id>", "title", "date", "language", "participants": {speaker id: name}.
Times ("at", "start", "end") are seconds from the start of the meeting.

Answer with the template file only: no code fence around it, no text before or after it."""


def draft_messages(kind: str, text: str, name: str = '') -> list:
    if kind == 'examples':
        ask = ("Here are past meeting summaries the user likes. Work out what they share: a fixed title or not, "
               "the date style, the section headings and their order, bullets or prose, how long each section is, "
               "the tone, how owners and dates of tasks are written, and what is never included. Write a template "
               "that produces summaries like these, using their own heading names. Where the examples disagree, "
               "follow the majority.\n\n=== SUMMARIES ===\n" + text)
    else:
        ask = "Write a template for this kind of meeting summary, as the user describes it:\n\n" + text
    if name:
        ask += f"\n\nName the template: {name}"
    return [{'role': 'system', 'content': DRAFT_GUIDE}, {'role': 'user', 'content': ask}]


def clean_draft(text: str) -> str:
    text = (text or '').strip()
    fence = re.match(r'^```[a-zA-Z]*\n(.*)\n```$', text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = text.find('---')
    return text[start:] if start > 0 else text


# Asks the model for a template; one more try with the problems listed when
# the first draft fails the check. Returns the file's text for the editor.
def draft_template(kind: str, text: str, complete: Callable, name: str = '', taken: Optional[set] = None) -> dict:
    if not str(text or '').strip():
        raise SummaryError('Write a description or paste at least one summary first')
    msgs = draft_messages(kind, text, name)
    last_problems = []
    for _ in range(2):
        result = complete('meetings', msgs, json=False)
        if not result.ok:
            raise SummaryError(result.error or 'The AI did not answer')
        draft = clean_draft(result.text)
        try:
            t = parse_text(draft, 'mine')
            last_problems = problems(t)
        except TemplateError as e:
            t, last_problems = None, [str(e)]
        if not last_problems:
            if taken and str(t['id']) in taken:
                base, n = str(t['id']), 2
                while f'{base}-{n}' in taken:
                    n += 1
                draft = re.sub(r'(?m)^id:.*$', f'id: {base}-{n}', draft, count=1)
            return {'text': draft, 'problems': []}
        msgs = msgs + [{'role': 'assistant', 'content': draft},
                       {'role': 'user', 'content': 'Fix these problems and answer with the whole file again: '
                                                   + '; '.join(last_problems)}]
    return {'text': draft, 'problems': last_problems}


__all__ = ['BLOCKS', 'SummaryError', 'choose_language', 'draft_template', 'meeting_language', 'meeting_text',
           'messages', 'summarize', 'tidy', 'worth_summarizing']
