# meeting/summary_templates.py
# The summary templates: one Markdown file each, a YAML header for the layout
# (title, date style, language, blocks, headings) and a body that is the
# system prompt the AI follows to write a meeting's brief. Built-ins ship in
# meeting/templates/; the user's own live in %APPDATA%\koli\summary_templates\,
# and templates made with the meeting-sum skill (~/.claude/meeting-templates/)
# show up too. Design: docs/design/meeting-summary.md.

import io
import json
import logging
import re
from pathlib import Path
from typing import Optional

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).resolve().parent / 'templates'
SKILL_DIR = Path.home() / '.claude' / 'meeting-templates'
DEFAULT_ID = 'standard'
ID_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,62}')

# Every block a template may list; the app, the PDF and the Google Doc know them.
BLOCKS = {
    'header': 'Title and date',
    'quick': 'Google Doc: the bottom lines on their own tab',
    'tldr': 'In short',
    'counts': 'Number cards',
    'participants': 'Participants',
    'agenda': 'Agenda',
    'agenda_details': 'Discussion, decisions and action items per agenda item',
    'decisions': 'Decisions',
    'action_items': 'Action items',
    'lists': 'Open questions and parked items',
    'open_questions': 'Open questions',
    'parked': 'Parked items',
    'next_meeting': 'Points for the next meeting',
    'speakers': 'Who spoke, with charts',
    'screenshots': 'What was on screen',
    'topics': 'Topics with quotes',
    'sections': 'Sections the template defines',
    'notes': 'Transcript notes',
    'transcript': 'The transcript',
    'data': 'The summary as JSON',
    'page_break': 'Google Doc: a new page',
}


# Every fixed word a report shows: section headings, tab names, table
# headers, statuses. A summary in another language carries its own
# translation in "labels" (the model writes it), so a Hebrew meeting gets
# Hebrew headings in the app and the Google Doc; these English words are the
# fallback. A template's own `labels` rename headings in English.
WORDS = {
    'tldr': 'Summary', 'decisions': 'Decisions', 'action_items': 'Action items', 'open_questions': 'Open questions',
    'parked': 'Parked', 'next_meeting': 'Points for the next meeting', 'speakers': 'Who spoke',
    'screenshots': 'What was on screen', 'topics': 'How the meeting went', 'participants': 'Participants',
    'agenda': 'Meeting Agenda', 'agenda_details': 'Key Points, Decisions, and Action Items',
    'agenda_discussion': 'Discussion', 'agenda_decisions': 'Decisions', 'agenda_actions': 'Action items',
    'notes': 'Transcription notes', 'in_short': 'In short', 'topics_short': 'Topics',
    'tab_quick': 'Quick summary', 'tab_report': 'Report', 'tab_summary': 'Summary', 'tab_transcript': 'Transcript',
    'transcript_title': 'transcript', 'turns_word': 'turns', 'verbatim': 'verbatim as Koli heard it',
    'col_task': 'Task', 'col_owner': 'Owner', 'col_status': 'Status', 'col_due': 'Due', 'col_at': 'At',
    'not_set': 'Not set', 'owner_not_set': 'Owner not set', 'due': 'due',
    'col_speaker': 'Speaker', 'col_talk_time': 'Talk time', 'col_share': 'Share', 'col_turns': 'Turns',
    'col_role': 'Role in this meeting', 'did_not_speak': 'Did not speak',
    'floor_title': 'Who held the floor, minute by minute',
    'floor_note': 'Seconds spoken in each minute, stacked. A minute passes 60 s when people talk over each other.',
    'screenshot_at': 'Screenshot at',
    'note_transcript': 'The Transcript tab has every word.',
    'note_transcript_quick': 'The Transcript tab has every word; the Quick summary tab has the bottom lines.',
    'note_quick': 'The Report tab has the charts, quotes and screenshots; the Transcript tab has every word.',
    'do_now': 'Do now', 'plan_only': 'Plan only', 'waiting': 'Waiting', 'done': 'Done',
}


# A word for the report: the summary's own translation, then the template's
# heading, then English.
def word(key: str, brief: dict = None, layout: dict = None) -> str:
    return ((brief or {}).get('labels') or {}).get(key) or ((layout or {}).get('labels') or {}).get(key) \
        or WORDS.get(key, key)


class TemplateError(ValueError):
    pass


# ── Reading ──────────────────────────────────────────────────────────────────

def parse_text(text: str, origin: str = '', path: str = '') -> dict:
    m = re.match(r'^﻿?---\s*\n(.*?)\n---\s*\n?(.*)$', text.replace('\r\n', '\n'), re.S)
    if not m:
        raise TemplateError('A template starts with a header between two "---" lines')
    try:
        head = YAML(typ='safe').load(m.group(1)) or {}
    except Exception as e:
        raise TemplateError(f'The header is not valid YAML: {e}')
    if not isinstance(head, dict):
        raise TemplateError('The header must be "key: value" lines')
    head['body'] = m.group(2).strip()
    head['text'] = text
    head['origin'] = origin
    head['file'] = path
    return head


def problems(t: dict) -> list:
    out = []
    for key in ('id', 'name', 'description', 'blocks'):
        if not t.get(key):
            out.append(f'"{key}" is missing from the header')
    if t.get('id') and not ID_RE.fullmatch(str(t['id'])):
        out.append('"id" must be lowercase letters, digits and hyphens')
    for key in ('blocks', 'doc_blocks'):
        value = t.get(key)
        if value is not None and not isinstance(value, list):
            out.append(f'"{key}" must be a list, like [header, tldr]')
            continue
        for b in value or []:
            if b not in BLOCKS:
                out.append(f'"{b}" in {key} is not a known block')
    title = t.get('title') or {}
    if isinstance(title, dict) and title.get('mode') == 'fixed' and not title.get('choices'):
        out.append('a fixed title needs "choices"')
    if t.get('date') not in (None, 'date', 'datetime'):
        out.append('"date" must be date or datetime')
    if not t.get('body'):
        out.append('the instructions for the AI (below the header) are empty')
    elif '```json' not in t['body']:
        out.append('the instructions should show the JSON to return in a ```json block')
    return out


class TemplateLibrary:
    """The template collection: built-ins, the skill's and the user's own."""

    def __init__(self, user_dir: Path, builtin_dir: Path = BUILTIN_DIR, skill_dir: Optional[Path] = SKILL_DIR):
        self.user_dir = Path(user_dir)
        self.builtin_dir = Path(builtin_dir)
        self.skill_dir = Path(skill_dir) if skill_dir else None

    def _sources(self):
        yield 'built-in', self.builtin_dir
        if self.skill_dir:
            yield 'skill', self.skill_dir
        yield 'mine', self.user_dir

    # id -> template; later folders win on the same id.
    def all(self) -> dict:
        found = {}
        for origin, folder in self._sources():
            if not folder.is_dir():
                continue
            for f in sorted(folder.glob('*.md')):
                if f.name.startswith('_'):
                    continue
                try:
                    t = parse_text(f.read_text(encoding='utf-8'), origin, str(f))
                except (OSError, TemplateError) as e:
                    logger.warning(f"Summary template {f.name} skipped: {e}")
                    continue
                t.setdefault('id', f.stem)
                if problems(t):
                    logger.warning(f"Summary template {f.name} skipped: {'; '.join(problems(t))}")
                    continue
                t['builtin_copy'] = origin != 'built-in' and (self.builtin_dir / f"{t['id']}.md").exists()
                found[str(t['id'])] = t
        return found

    def get(self, template_id: str) -> dict:
        found = self.all()
        if template_id in found:
            return found[template_id]
        if DEFAULT_ID in found:
            logger.warning(f"Summary template '{template_id}' not found; using '{DEFAULT_ID}'")
            return found[DEFAULT_ID]
        raise TemplateError(f"No summary template '{template_id}'")

    def common_rules(self) -> str:
        for _, folder in reversed(list(self._sources())):
            f = folder / '_common.md'
            if f.exists():
                return f.read_text(encoding='utf-8').strip()
        return ''

    # The system prompt: the body with the shared rules, and the language rule.
    # The system prompt. In any language but English it also asks for the
    # report's fixed words in that language ("labels"), the fixed title too.
    def prompt(self, t: dict, language_name: str) -> str:
        body = t['body'].replace('{{common_rules}}', self.common_rules())
        text = (f"{body}\n\n## Language\n\nWrite the report in {language_name}. Keep names, product names and "
                f"quotes as they were said. Set \"language\" in the JSON to the ISO code of the language you wrote in.")
        if not language_name.lower().startswith('english'):
            words = {k: word(k, layout=t) for k in WORDS}
            spec = t.get('title') or {}
            if isinstance(spec, dict) and spec.get('mode') == 'fixed' and spec.get('choices'):
                words['title'] = ' / '.join(spec['choices'])
            text += ("\n\nAlso return \"labels\": these headings and words of the report, translated into "
                     f"{language_name}, short and natural, with the same keys"
                     + (" (\"title\": the one fixed title you chose, translated)" if 'title' in words else '')
                     + ":\n" + json.dumps(words, ensure_ascii=False))
        return text

    # ── Changing the user's collection ───────────────────────────────────────

    def save(self, text: str, replaces: str = '') -> dict:
        t = parse_text(text, 'mine')
        found = problems(t)
        if found:
            raise TemplateError('; '.join(found))
        new_id = str(t['id'])
        self.user_dir.mkdir(parents=True, exist_ok=True)
        path = self.user_dir / f'{new_id}.md'
        path.write_text(text.replace('\r\n', '\n').rstrip() + '\n', encoding='utf-8')
        # Renamed while editing: the old file of the user's goes.
        if replaces and replaces != new_id:
            old = self.user_dir / f'{replaces}.md'
            if old.exists():
                old.unlink()
        t['file'] = str(path)
        logger.info(f"Summary template saved: {new_id}")
        return t

    def delete(self, template_id: str) -> bool:
        path = self.user_dir / f'{template_id}.md'
        if not path.exists():
            raise TemplateError("Only your own templates can be deleted")
        path.unlink()
        logger.info(f"Summary template deleted: {template_id}")
        return True

    # A copy under a new id and name, ready to edit.
    def copy_text(self, template_id: str, new_id: str, new_name: str) -> str:
        text = self.get(template_id)['text']
        text = re.sub(r'(?m)^id:.*$', f'id: {new_id}', text, count=1)
        return re.sub(r'(?m)^name:.*$', f'name: {_quote(new_name)}', text, count=1)

    def free_id(self, base: str) -> str:
        base = re.sub(r'[^a-z0-9-]+', '-', base.lower()).strip('-')[:50] or 'template'
        taken = set(self.all())
        if base not in taken:
            return base
        n = 2
        while f'{base}-{n}' in taken:
            n += 1
        return f'{base}-{n}'


def _quote(value: str) -> str:
    buf = io.StringIO()
    YAML().dump(value, buf)
    return buf.getvalue().strip().removesuffix('...').strip()


# The list the app shows: no prompt bodies, just what a row needs.
def summary(t: dict, default_id: str) -> dict:
    return {
        'id': t['id'], 'name': t.get('name'), 'description': t.get('description', ''),
        'origin': t.get('origin'), 'language': t.get('language') or 'meeting',
        'blocks': [b for b in t.get('blocks') or [] if b not in ('page_break', 'quick')],
        'sections': [BLOCKS.get(b, b) for b in t.get('blocks') or [] if b not in ('page_break', 'quick', 'header', 'data', 'transcript', 'counts')],
        'default': t['id'] == default_id, 'editable': t.get('origin') == 'mine',
        'overrides_builtin': bool(t.get('builtin_copy')),
    }


# What the pages need to lay a brief out.
def layout(t: dict) -> dict:
    return {key: t.get(key) for key in ('id', 'name', 'title', 'date', 'blocks', 'doc_blocks', 'labels', 'language')}
