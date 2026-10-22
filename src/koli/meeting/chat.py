# meeting/chat.py
# The meeting's Chat tab: you ask the AI about one meeting (what was decided,
# what Dana said about the budget) and it answers from the transcript and the
# summary. It may also change the meeting when you ask it to: name a speaker,
# add someone who was there, fix the title or a part of the summary, or write
# the summary again. It answers JSON; the changes are checked here and done by
# the meetings service. Design: docs/design/meeting-summary.md (Chat).

import json
import logging
from typing import Callable

from .summarize import LIST_FIELDS, SummaryError, meeting_text

logger = logging.getLogger(__name__)

HISTORY = 20          # earlier messages the model sees again
SUMMARY_FIELDS = set(LIST_FIELDS) | {'title', 'participants', 'roles'}
OPS = ('rename_speaker', 'add_attendee', 'remove_attendee', 'set_title', 'edit_summary', 'resummarize')

SYSTEM = """You are Koli's assistant for one recorded meeting. You get the meeting (speakers, who was there, the transcript with times in seconds) and its summary as JSON, if there is one.

Answer the user's questions from the meeting only. Say so when the meeting doesn't tell. Quote short exact words when that helps, with the time as m:ss. Answer in the language the user writes in. Keep answers short and concrete.

When the user asks you to change the meeting, add the changes; when they only ask a question, add none. The changes you can make:
- {"op": "rename_speaker", "speaker": "<speaker id from the list>", "name": "<name>"}: who a voice is. The name sticks to that voice in later meetings too.
- {"op": "add_attendee", "name": "<name>"}: someone who was in the meeting (also when they said nothing).
- {"op": "remove_attendee", "name": "<name>"}
- {"op": "set_title", "title": "<new title>"}
- {"op": "edit_summary", "set": {"<field>": <new value>}}: replace whole fields of the summary JSON, in the same shape they have now. Fields: title, tldr, decisions, action_items, open_questions, parked, next_meeting, agenda, topics, sections, notes, attendees, participants, roles.
- {"op": "resummarize", "instructions": "<what the new summary should do differently>"}: write the whole summary again. Use it for big changes; use edit_summary for small ones.
Never invent speaker ids. In "reply", tell the user in one line what you changed.

Answer with one JSON object only: {"reply": "<your answer>", "changes": [<changes, or empty>]}"""


# What the model reads: the meeting, its summary, then the conversation so far.
def messages(m: dict, brief: dict, history: list, question: str) -> list:
    context = meeting_text(m)
    if brief:
        context += "\n\nThe summary (JSON):\n" + json.dumps(brief, ensure_ascii=False)
    else:
        context += "\n\nThis meeting has no summary yet."
    msgs = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': context},
            {'role': 'assistant', 'content': '{"reply": "I have read the meeting.", "changes": []}'}]
    for h in history[-HISTORY:]:
        if h.get('role') in ('user', 'assistant') and h.get('text'):
            msgs.append({'role': h['role'], 'content': h['text']})
    msgs.append({'role': 'user', 'content': question})
    return msgs


# Only the changes this module knows, with the fields each needs; anything
# else the model wrote is dropped.
def clean_changes(changes, speaker_ids: set) -> list:
    out = []
    for c in changes if isinstance(changes, list) else []:
        if not isinstance(c, dict) or c.get('op') not in OPS:
            continue
        op = c['op']
        if op == 'rename_speaker':
            if c.get('speaker') in speaker_ids and str(c.get('name') or '').strip():
                out.append({'op': op, 'speaker': c['speaker'], 'name': str(c['name']).strip()[:40]})
        elif op in ('add_attendee', 'remove_attendee'):
            if str(c.get('name') or '').strip():
                out.append({'op': op, 'name': str(c['name']).strip()[:40]})
        elif op == 'set_title':
            if str(c.get('title') or '').strip():
                out.append({'op': op, 'title': str(c['title']).strip()[:120]})
        elif op == 'edit_summary':
            fields = {k: v for k, v in (c.get('set') or {}).items() if k in SUMMARY_FIELDS} \
                if isinstance(c.get('set'), dict) else {}
            if fields:
                out.append({'op': op, 'set': fields})
        elif op == 'resummarize':
            out.append({'op': op, 'instructions': str(c.get('instructions') or '').strip()})
    return out


# One question: {'reply', 'changes'}. Raises SummaryError when the AI fails.
def ask(m: dict, brief: dict, history: list, question: str, complete: Callable, parse: Callable = None) -> dict:
    if not str(question or '').strip():
        raise SummaryError('Write a question first')
    result = complete('meetings', messages(m, brief, history, question), json=True)
    if not result.ok:
        raise SummaryError(result.error or 'The AI did not answer')
    try:
        answer = (parse or json.loads)(result.text)
    except Exception:
        answer = {'reply': result.text}
    if not isinstance(answer, dict):
        answer = {'reply': str(answer)}
    ids = {s['id'] for s in m.get('speakers', [])} | {t['speaker'] for t in m.get('turns', [])}
    return {'reply': str(answer.get('reply') or '').strip() or '…',
            'changes': clean_changes(answer.get('changes'), ids)}


__all__ = ['OPS', 'ask', 'clean_changes', 'messages']
