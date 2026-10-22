# meeting/gdoc.py
# A meeting saved to Google Drive as one Google Doc in "Koli/Meetings/<name>/":
# a Quick summary tab first (the bottom lines, no charts), the Report tab (the
# full brief with charts, quotes and screenshots, written as HTML that Drive
# converts) and the Transcript tab (every turn, speakers in colour, Hebrew
# right to left). The meeting remembers the folder and the doc, so the app can
# offer Open and Share, and a later export updates that same doc in place
# (its link and sharing stay).

import base64
import datetime
import html
import io
import logging
import re

from .store import roster
from .summarize import fold_extras
from .summary_templates import WORDS, word

logger = logging.getLogger(__name__)

DOC_MIME = 'application/vnd.google-apps.document'
ROOT_FOLDER = 'Koli/Meetings'
# The app's speaker colours (screens/brief.js), by a speaker's colour index.
COLOURS = ['#0A6E82', '#B45309', '#6D28D9', '#15803D', '#BE185D', '#0369A1', '#C2410C', '#4D7C0F']
GREY = (0.45, 0.47, 0.50)


# ── Small helpers ────────────────────────────────────────────────────────────

def clock(seconds) -> str:
    s = max(0, int(seconds or 0))
    return f"{s // 60:02d}:{s % 60:02d}"


# Docs counts positions in UTF-16 code units.
def u16(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def rgb(hex_or_tuple):
    if isinstance(hex_or_tuple, str):
        h = hex_or_tuple.lstrip('#')
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    else:
        r, g, b = hex_or_tuple
    return {'color': {'rgbColor': {'red': r, 'green': g, 'blue': b}}}


def _speakers(m: dict) -> dict:
    return {s['id']: s for s in m.get('speakers', [])}


def _who(m: dict, speaker_id: str) -> dict:
    return _speakers(m).get(speaker_id) or {'id': speaker_id, 'name': 'Speaker ?' if speaker_id == '?' else speaker_id, 'colour': 7}


# "2026-10-19 Dev sync - SEO": the folder's and the doc's name. A fixed title
# ("Board Meeting Summary") gets the meeting's own name after it.
def folder_name(m: dict, brief: dict, fixed_title: bool = False) -> str:
    title = str((brief or {}).get('title') or m.get('title') or 'Meeting')
    if fixed_title and m.get('title') and m['title'] != title:
        title = f"{title} - {m['title']}"
    text = re.sub(r'\s*:\s*', ' - ', f"{str(m.get('started_at') or '')[:10]} {title}".strip())
    return re.sub(r'[\\/*?"<>|]', '-', text).strip() or m['id']


def _length(m: dict) -> float:
    if m.get('ended_at'):
        try:
            return (datetime.datetime.fromisoformat(m['ended_at']) - datetime.datetime.fromisoformat(m['started_at'])).total_seconds()
        except ValueError:
            pass
    return max([t['end'] for t in m.get('turns', [])] or [0])


def _day(m: dict) -> str:
    try:
        return datetime.datetime.fromisoformat(m['started_at']).strftime('%a %d %b %Y, %H:%M')
    except (KeyError, ValueError):
        return ''


# Seconds and turns per speaker, loudest first; under 1% is folded away.
def talk_stats(m: dict) -> list:
    by = {}
    for t in m.get('turns', []):
        s = by.setdefault(t['speaker'], {'id': t['speaker'], 'seconds': 0.0, 'turns': 0})
        s['seconds'] += max(0.0, t['end'] - t['start'])
        s['turns'] += 1
    total = sum(s['seconds'] for s in by.values()) or 1.0
    rows = sorted(by.values(), key=lambda s: -s['seconds'])
    return [{**s, 'share': s['seconds'] / total} for s in rows if s['seconds'] / total >= 0.01]


def per_minute(m: dict) -> list:
    minutes = {}
    for t in m.get('turns', []):
        for mn in range(int(t['start'] // 60), int(t['end'] // 60) + 1):
            v = min(t['end'], (mn + 1) * 60) - max(t['start'], mn * 60)
            if v > 0:
                row = minutes.setdefault(mn, {})
                row[t['speaker']] = row.get(t['speaker'], 0.0) + v
    return [minutes.get(i, {}) for i in range(max(minutes) + 1)] if minutes else []


# ── Charts, drawn as PNG for the Report tab ──────────────────────────────────

def _font(size: int):
    from PIL import ImageFont
    for name in ('segoeui.ttf', 'arial.ttf', 'DejaVuSans.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _png(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


def share_chart(m: dict, stats: list) -> str:
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (640, 20 + 40 * max(len(stats), 1)), 'white')
    d, f = ImageDraw.Draw(img), _font(16)
    top = max([s['seconds'] for s in stats] or [1])
    for i, s in enumerate(stats):
        sp, y = _who(m, s['id']), 14 + i * 40
        w = int(400 * s['seconds'] / top)
        d.text((14, y + 2), sp['name'], fill=(60, 70, 80), font=f)
        d.rounded_rectangle((130, y, 130 + w, y + 24), radius=4, fill=COLOURS[sp['colour'] % 8])
        d.text((138 + w, y + 2), f"{clock(s['seconds'])} · {round(s['share'] * 100)}%", fill=(60, 70, 80), font=f)
    return _png(img)


def timeline_chart(m: dict, stats: list) -> str:
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (900, 300), 'white')
    d, f = ImageDraw.Draw(img), _font(14)
    order = [s['id'] for s in stats]
    mins = per_minute(m) or [{}]
    ymax = max(20, int((max(sum(mm.get(k, 0) for k in order) for mm in mins) + 19) // 20 * 20))
    left, top, right, bottom = 50, 20, 880, 250
    ph, pw = bottom - top, right - left
    for v in range(0, ymax + 1, 20):
        y = bottom - ph * v / ymax
        d.line((left, y, right, y), fill=(225, 229, 234))
        d.text((8, y - 8), f"{v}s", fill=(120, 130, 140), font=f)
    colw = pw / len(mins)
    step = 10 if len(mins) > 40 else 5
    for i, mm in enumerate(mins):
        x, y = left + i * colw + 2, bottom
        for k in order:
            v = mm.get(k, 0)
            if v > 0:
                h = ph * v / ymax
                d.rectangle((x, y - h + 1, x + max(colw - 4, 1), y), fill=COLOURS[_who(m, k)['colour'] % 8])
                y -= h
        if i % step == 0:
            d.text((left + i * colw + colw / 2 - 14, bottom + 8), clock(i * 60), fill=(120, 130, 140), font=f)
    lx = 50
    for k in order:
        d.rectangle((lx, 275, lx + 14, 289), fill=COLOURS[_who(m, k)['colour'] % 8])
        d.text((lx + 20, 272), _who(m, k)['name'], fill=(60, 70, 80), font=f)
        lx += 120
    return _png(img)


# The visual brief's order, for briefs written before templates existed.
STANDARD_DOC_BLOCKS = ['header', 'quick', 'page_break', 'tldr', 'decisions', 'action_items', 'lists', 'speakers', 'screenshots',
                       'topics', 'notes', 'page_break', 'transcript']
LABELS = WORDS      # the headings, in English; a summary's own `labels` translate them
RTL = ('he', 'ar', 'fa', 'ur', 'yi')


def doc_blocks(layout: dict = None) -> list:
    layout = layout or {}
    return list(layout.get('doc_blocks') or layout.get('blocks') or STANDARD_DOC_BLOCKS)


# Sections the user asked for show even when the template doesn't list them:
# before the notes and the transcript, so the Doc matches the Summary tab.
def with_sections(blocks: list, brief: dict) -> list:
    if not (brief or {}).get('sections') or 'sections' in blocks:
        return blocks
    tail = ('notes', 'page_break', 'transcript', 'data')
    i = len(blocks)
    while i > 0 and blocks[i - 1] in tail:
        i -= 1
    return blocks[:i] + ['sections'] + blocks[i:]


def _all_decisions(brief: dict) -> list:
    return brief.get('decisions') or [d for a in brief.get('agenda') or [] for d in a.get('decisions') or []]


def _all_actions(brief: dict) -> list:
    return brief.get('action_items') or [x for a in brief.get('agenda') or [] for x in a.get('action_items') or []]


# ── The Report tab: HTML Drive converts, laid out by the template ────────────

def report_html(m: dict, brief: dict, shot_data: dict, layout: dict = None) -> str:
    e = html.escape
    brief = fold_extras(brief)
    layout = layout or {}
    stats = talk_stats(m)
    # People who were there but said almost nothing: under the charts' 1%
    # or silent, they are still in the speakers table and the participants.
    quiet = [r for r in roster(m) if r['speaker'] not in {s['id'] for s in stats}]
    roles = brief.get('roles') or {}
    shots = sorted(m.get('screenshots', []), key=lambda s: s['at'])
    in_topic = set()

    def label(key):
        return word(key, brief, layout)

    def at(sec):
        return f" <i style='color:#777'>({clock(sec)})</i>" if sec is not None else ''

    def img(shot, width):
        data = shot_data.get(shot['image'])
        if not data:
            return ''
        src = 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')
        return (f"<p style='text-align:center'><img src='{src}' width='{width}'></p>"
                f"<p style='text-align:center;color:#777;font-size:9pt'><i>{e(label('screenshot_at'))} {clock(shot['at'])} · {e(shot.get('caption') or '')}</i></p>")

    def items(rows, numbered=False):
        tag = 'ol' if numbered else 'ul'
        return f"<{tag}>" + ''.join(f"<li>{e(x['text'])}{at(x.get('at'))}</li>" for x in rows) + f"</{tag}>"

    def actions_table(rows):
        out = ["<table border='1' cellpadding='5'><tr><th>#</th>" + ''.join(
            f"<th>{e(label(k))}</th>" for k in ('col_task', 'col_owner', 'col_status', 'col_due', 'col_at')) + "</tr>"]
        out += [f"<tr><td>{e(str(a.get('id', '')))}</td><td>{e(a['task'])}</td><td>{e(a.get('owner') or label('not_set'))}</td>"
                f"<td>{e(label(a.get('status')) if a.get('status') else '')}</td><td>{e(a.get('due') or '')}</td>"
                f"<td>{clock(a.get('at'))}</td></tr>" for a in rows]
        return ''.join(out) + "</table>"

    def b_header():
        if layout.get('date') == 'date':
            meta = str(brief.get('date') or m.get('started_at') or '')[:10]
        else:
            meta = f"{_day(m)} · {clock(_length(m))} · {', '.join(_who(m, s['id'])['name'] for s in stats)}"
        return f"<h1>{e(brief.get('title') or m['title'])}</h1><p style='color:#666'>{e(meta)}</p>"

    def b_tldr():
        return (f"<h2>{e(label('tldr'))}</h2>" + ''.join(f"<p>{e(p)}</p>" for p in brief['tldr'])) if brief.get('tldr') else ''

    def b_participants():
        people = brief.get('attendees') or [{'name': _who(m, s['id'])['name'], 'role': roles.get(s['id'], '')} for s in stats] +             [{'name': r['name'], 'role': '' if r['spoke'] else label('did_not_speak')} for r in quiet]
        return (f"<h2>{e(label('participants'))}</h2><ul>" + ''.join(
            f"<li>{e(str(a.get('name', '')))}{(' — ' + e(a['role'])) if a.get('role') else ''}</li>" for a in people) + "</ul>")

    def b_agenda():
        rows = brief.get('agenda') or []
        return (f"<h2>{e(label('agenda'))}</h2><ol>" + ''.join(f"<li>{e(a['title'])}</li>" for a in rows) + "</ol>") if rows else ''

    def b_agenda_details():
        rows = brief.get('agenda') or []
        if not rows:
            return ''
        out = [f"<h2>{e(label('agenda_details'))}</h2>"]
        for n, a in enumerate(rows, 1):
            out.append(f"<h3>{n}. {e(a['title'])}</h3>")
            if a.get('discussion'):
                out.append(f"<p><b>{e(label('agenda_discussion'))}</b></p><ul>" + ''.join(f"<li>{e(x)}</li>" for x in a['discussion']) + "</ul>")
            if a.get('decisions'):
                out.append(f"<p><b>{e(label('agenda_decisions'))}</b></p>" + items(a['decisions']))
            if a.get('action_items'):
                out.append(f"<p><b>{e(label('agenda_actions'))}</b></p>" + actions_table(a['action_items']))
        return ''.join(out)

    def b_decisions():
        rows = _all_decisions(brief)
        return (f"<h2>{e(label('decisions'))}</h2>" + items(rows, numbered=True)) if rows else ''

    def b_action_items():
        rows = _all_actions(brief)
        if not rows:
            return ''
        note = "<p><i>No deadlines were stated.</i></p>" if all(not a.get('due') for a in rows) else ''
        return f"<h2>{e(label('action_items'))}</h2>" + actions_table(rows) + note

    def b_open_questions():
        return (f"<h2>{e(label('open_questions'))}</h2>" + items(brief['open_questions'])) if brief.get('open_questions') else ''

    def b_parked():
        return (f"<h2>{e(label('parked'))}</h2>" + items(brief['parked'])) if brief.get('parked') else ''

    def b_lists():
        return b_open_questions() + b_parked()

    def b_next_meeting():
        return (f"<h2>{e(label('next_meeting'))}</h2>" + items(brief['next_meeting'])) if brief.get('next_meeting') else ''

    def b_speakers():
        if not stats and not quiet:
            return ''
        out = [f"<h2>{e(label('speakers'))}</h2>", f"<p><img src='{share_chart(m, stats)}' width='520'></p>",
               f"<h3>{e(label('floor_title'))}</h3>",
               f"<p style='color:#666'>{e(label('floor_note'))}</p>",
               f"<p><img src='{timeline_chart(m, stats)}' width='620'></p>",
               "<table border='1' cellpadding='5'><tr>" + ''.join(
                   f"<th>{e(label(k))}</th>" for k in ('col_speaker', 'col_talk_time', 'col_share', 'col_turns', 'col_role')) + "</tr>"]
        out += [f"<tr><td>{e(_who(m, s['id'])['name'])}</td><td>{clock(s['seconds'])}</td><td>{round(s['share'] * 100)}%</td>"
                f"<td>{s['turns']}</td><td>{e(roles.get(s['id'], ''))}</td></tr>" for s in stats]
        out += [f"<tr><td>{e(r['name'])}</td><td>{clock(r['seconds']) if r['spoke'] else e(label('did_not_speak'))}</td><td></td>"
                f"<td>{r['turns'] or ''}</td><td>{e(roles.get(r['speaker'] or '', ''))}</td></tr>" for r in quiet]
        return ''.join(out) + "</table>"

    def b_screenshots():
        # With topic cards, each screenshot sits inside its topic instead.
        if not shots or 'topics' in blocks and brief.get('topics'):
            return ''
        return f"<h2>{e(label('screenshots'))}</h2>" + ''.join(img(s, 460) for s in shots)

    def b_topics():
        if not brief.get('topics'):
            return ''
        out = [f"<h2>{e(label('topics'))}</h2>"]
        for tp in brief['topics']:
            out.append(f"<h3>{clock(tp['start'])}–{clock(tp['end'])} · {e(tp['title'])}</h3>")
            if tp.get('lead'):
                out.append(f"<p><i>{e(tp['lead'])}</i></p>")
            if tp.get('points'):
                out.append("<ul>" + ''.join(f"<li>{e(x)}</li>" for x in tp['points']) + "</ul>")
            for q in tp.get('quotes') or []:
                out.append(f"<blockquote><p dir='rtl'>{e(q['text'])}</p><p><i>{e(q.get('who') or '')} · {clock(q.get('at'))}</i></p></blockquote>")
            if 'screenshots' in blocks:
                for s in shots:
                    if tp['start'] <= s['at'] < tp['end'] and s['image'] not in in_topic:
                        in_topic.add(s['image'])
                        out.append(img(s, 460))
        return ''.join(out)

    def b_sections():
        out = []
        for s in brief.get('sections') or []:
            out.append(f"<h2>{e(s['title'])}</h2>" + ''.join(f"<p>{e(x)}</p>" for x in s.get('text') or []))
            if s.get('items'):
                out.append("<ul>" + ''.join(f"<li>{e(x)}</li>" for x in s['items']) + "</ul>")
        return ''.join(out)

    def b_notes():
        return f"<p style='color:#777'><i>{e(label('notes'))}: {e('; '.join(brief['notes']))}.</i></p>" if brief.get('notes') else ''

    blocks = with_sections(doc_blocks(layout), brief)
    makers = {name[2:]: fn for name, fn in locals().items() if name.startswith('b_')}
    # The Quick summary and the transcript have their own tabs; the rest goes here.
    body = [makers[b]() for b in blocks if b in makers]
    tabs = f"<p style='color:#777'><i>{e(label('note_transcript_quick' if 'quick' in blocks else 'note_transcript'))}</i></p>"
    direction = " dir='rtl'" if str(brief.get('language') or '')[:2] in RTL else ''
    return f"<html><head><meta charset='utf-8'></head><body{direction}>" + "\n".join(x for x in body if x) + tabs + "</body></html>"


# ── Text tabs, written with Docs edits ───────────────────────────────────────
# Paragraphs are (text, kind, spans); kind picks the paragraph style and spans
# colour parts of the line: (offset, length, colour, bold, size).

def _write(tab: str, paras: list) -> list:
    text = '\n'.join(p[0] for p in paras) + '\n'
    reqs = [{'insertText': {'location': {'index': 1, 'tabId': tab}, 'text': text}}]
    pos, bullets = 1, []
    for body, kind, spans in paras:
        start, end = pos, pos + u16(body) + 1
        rng = {'startIndex': start, 'endIndex': end, 'tabId': tab}
        named = {'title': 'TITLE', 'heading': 'HEADING_2', 'subheading': 'HEADING_3'}.get(kind, 'NORMAL_TEXT')
        style, fields = {'namedStyleType': named}, ['namedStyleType']
        if kind == 'turn':
            style.update({'direction': 'RIGHT_TO_LEFT', 'alignment': 'START', 'spaceBelow': {'magnitude': 4, 'unit': 'PT'}})
            fields += ['direction', 'alignment', 'spaceBelow']
        reqs.append({'updateParagraphStyle': {'range': rng, 'paragraphStyle': style, 'fields': ','.join(fields)}})
        if kind in ('meta', 'note'):
            reqs.append({'updateTextStyle': {'range': {'startIndex': start, 'endIndex': end - 1, 'tabId': tab},
                                             'textStyle': {'italic': kind == 'note', 'foregroundColor': rgb(GREY)},
                                             'fields': 'italic,foregroundColor'}})
        if kind == 'bullet':
            bullets.append(rng)
        for off, ln, colour, bold, size in spans:
            ts, f = {'foregroundColor': rgb(colour), 'bold': bold}, ['foregroundColor', 'bold']
            if size:
                ts['fontSize'] = {'magnitude': size, 'unit': 'PT'}
                f.append('fontSize')
            reqs.append({'updateTextStyle': {'range': {'startIndex': start + off, 'endIndex': start + off + ln, 'tabId': tab},
                                             'textStyle': ts, 'fields': ','.join(f)}})
        pos = end
    # Bullets last, from the bottom: they add no characters but keep it safe.
    for rng in reversed(bullets):
        reqs.append({'createParagraphBullets': {'range': rng, 'bulletPreset': 'BULLET_DISC_CIRCLE_SQUARE'}})
    return reqs


# The Quick summary tab: what was decided, who does what, what's open.
def quick_requests(m: dict, brief: dict, tab: str) -> list:
    stats = talk_stats(m)
    w = lambda key: word(key, brief)      # noqa: E731
    paras = [(brief.get('title') or m['title'], 'title', []),
             (f"{_day(m)} · {clock(_length(m))} · {', '.join(_who(m, s['id'])['name'] for s in stats)}", 'meta', []),
             (w('in_short'), 'heading', [])]
    paras += [(p, 'normal', []) for p in brief.get('tldr') or []]
    if brief.get('decisions'):
        paras.append((w('decisions'), 'heading', []))
        paras += [(f"{d['text']} ({clock(d.get('at'))})", 'bullet', []) for d in brief['decisions']]
    if brief.get('action_items'):
        paras.append((w('action_items'), 'heading', []))
        for a in brief['action_items']:
            owner = a.get('owner') or w('owner_not_set')
            line = f"{a['task']} · {owner} · {w(a['status']) if a.get('status') else ''}"
            if a.get('due'):
                line += f" · {w('due')} {a['due']}"
            paras.append((line, 'bullet', [(u16(a['task']) + 3, u16(owner), (0.12, 0.15, 0.17), True, None)]))
    if brief.get('open_questions'):
        paras.append((w('open_questions'), 'heading', []))
        paras += [(q['text'], 'bullet', []) for q in brief['open_questions']]
    if brief.get('parked'):
        paras.append((w('parked'), 'heading', []))
        paras += [(p['text'], 'bullet', []) for p in brief['parked']]
    if brief.get('topics'):
        paras.append((w('topics_short'), 'heading', []))
        for t in brief['topics']:
            head = f"{clock(t['start'])}–{clock(t['end'])} {t['title']}"
            paras.append((f"{head}: {t.get('lead') or ''}".rstrip(': '), 'bullet', [(0, u16(head), (0.12, 0.15, 0.17), True, None)]))
    paras.append((w('note_quick'), 'note', []))
    return _write(tab, paras)


# The Transcript tab: speakers in their colours, time in grey, right to left,
# topic headings at the moments they start.
def transcript_requests(m: dict, brief: dict, tab: str) -> list:
    turns = sorted(m.get('turns', []), key=lambda t: t['start'])
    topics = sorted((brief or {}).get('topics') or [], key=lambda t: t['start'])
    stats = talk_stats(m)
    w = lambda key: word(key, brief)      # noqa: E731
    paras = [(f"{brief.get('title') or m['title']}: {w('transcript_title')}", 'title', []),
             (f"{_day(m)} · {clock(_length(m))} · {len(turns)} {w('turns_word')} · {w('verbatim')}", 'meta', [])]
    legend, spans = '', []
    for s in stats:
        sp = _who(m, s['id'])
        piece = f"■ {sp['name']}"
        spans.append((u16(legend), u16(piece), COLOURS[sp['colour'] % 8], True, None))
        legend += piece + '    '
    if legend:
        paras.append((legend.rstrip(), 'normal', spans))
    ti = 0
    for t in turns:
        while ti < len(topics) and topics[ti]['start'] <= t['start']:
            paras.append((f"{clock(topics[ti]['start'])} · {topics[ti]['title']}", 'heading', []))
            ti += 1
        sp = _who(m, t['speaker'])
        stamp, name = f"[{clock(t['start'])}] ", f"{sp['name']}: "
        paras.append((stamp + name + t['text'], 'turn',
                      [(0, u16(stamp), GREY, False, 9), (u16(stamp), u16(name), COLOURS[sp['colour'] % 8], True, None)]))
    return _write(tab, paras)


# ── The export ───────────────────────────────────────────────────────────────

# The tabs after the first (Report): the Quick summary first for templates
# that ask for one, and the Transcript last.
def _fill_tabs(docs, doc_id: str, m: dict, brief: dict, layout: dict):
    first = docs.get(doc_id)['tabs'][0]['tabProperties']['tabId']
    with_quick = 'quick' in doc_blocks(layout)
    tab = lambda key: word(key, brief, layout)      # noqa: E731
    requests = [{'updateDocumentTabProperties': {'tabProperties': {'tabId': first, 'title': tab('tab_report' if with_quick else 'tab_summary')},
                                                 'fields': 'title'}}]
    if with_quick:
        requests.append({'addDocumentTab': {'tabProperties': {'title': tab('tab_quick'), 'index': 0}}})
    requests.append({'addDocumentTab': {'tabProperties': {'title': tab('tab_transcript')}}})
    replies = docs.batch_update(doc_id, requests)['replies']
    transcript = replies[-1]['addDocumentTab']['tabProperties']['tabId']
    edits = transcript_requests(m, brief, transcript)
    if with_quick:
        edits = quick_requests(m, brief, replies[1]['addDocumentTab']['tabProperties']['tabId']) + edits
    docs.batch_update(doc_id, edits)


# The doc exported before, given new content in place: its id, link and
# sharing stay, so a link you sent keeps working. Every tab but the first
# goes, the report replaces the first tab's content, and the tabs are made
# again. Raises when the doc is gone or can't be changed.
def _refresh(drive, docs, doc_id: str, page: str, name: str):
    tabs = docs.get(doc_id).get('tabs') or []
    extra = [{'deleteTab': {'tabId': t['tabProperties']['tabId']}} for t in tabs[1:]]
    if extra:
        docs.batch_update(doc_id, extra)
    drive.upload_bytes(page.encode('utf-8'), name, 'text/html', file_id=doc_id)


# Builds the meeting's doc, or refreshes the one exported before, and
# remembers where it is. Returns {'name', 'folder_id', 'folder_url',
# 'doc_id', 'doc_url', 'exported_at'}.
def export(store, meeting_id: str, drive=None, docs=None, layout: dict = None) -> dict:
    brief = fold_extras(store.load_brief(meeting_id))
    if not brief:
        raise ValueError("This meeting has no summary yet, so there is nothing to export")
    if drive is None:
        from ..drive import api as drive
    if docs is None:
        from ..drive import docs
    m = store.load(meeting_id)
    spec = (layout or {}).get('title') or {}
    name = folder_name(m, brief, isinstance(spec, dict) and spec.get('mode') == 'fixed')
    kept = m.get('drive') or {}
    shot_data = {}
    for s in m.get('screenshots', []):
        try:
            shot_data[s['image']] = store.shot_path(meeting_id, s['image']).read_bytes()
        except FileNotFoundError:
            pass
    page = report_html(m, brief, shot_data, layout)
    doc_id, folder = None, kept.get('folder_id')
    if kept.get('doc_id'):
        try:
            _refresh(drive, docs, kept['doc_id'], page, name)
            doc_id = kept['doc_id']
            # A new title renames the folder too, so the doc and its folder match.
            if folder and kept.get('name') != name:
                drive.rename(folder, name)
        except Exception as e:
            logger.warning(f"The doc saved before couldn't be updated ({e}); a new one is made")
            try:
                drive.delete(kept['doc_id'])
            except Exception:
                pass
    if doc_id is None:
        folder = drive.ensure_folder(f"{ROOT_FOLDER}/{name}")
        doc_id = drive.upload_bytes(page.encode('utf-8'), name, 'text/html', folder_id=folder, convert_to=DOC_MIME)['id']
    _fill_tabs(docs, doc_id, m, brief, layout)
    info = {'name': name, 'folder_id': folder, 'folder_url': f"https://drive.google.com/drive/folders/{folder}", 'doc_id': doc_id,
            'doc_url': str(drive.info(doc_id).get('webViewLink') or f"https://docs.google.com/document/d/{doc_id}/edit"),
            'exported_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
    m = store.load(meeting_id)
    m['drive'] = info
    store.save(m)
    logger.info(f"Meeting {meeting_id} {'updated in' if doc_id == kept.get('doc_id') else 'saved to'} Google Drive as {doc_id}")
    return info
