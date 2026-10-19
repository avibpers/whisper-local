# meeting/gdoc.py
# A meeting saved to Google Drive as one Google Doc in "Koli/Meetings/<name>/":
# a Quick summary tab first (the bottom lines, no charts), the Report tab (the
# full brief with charts, quotes and screenshots, written as HTML that Drive
# converts) and the Transcript tab (every turn, speakers in colour, Hebrew
# right to left). The meeting remembers the folder and the doc, so the app can
# offer Open and Share, and a second export replaces the doc in that folder.

import base64
import datetime
import html
import io
import logging
import re

logger = logging.getLogger(__name__)

DOC_MIME = 'application/vnd.google-apps.document'
ROOT_FOLDER = 'Koli/Meetings'
# The app's speaker colours (screens/brief.js), by a speaker's colour index.
COLOURS = ['#0A6E82', '#B45309', '#6D28D9', '#15803D', '#BE185D', '#0369A1', '#C2410C', '#4D7C0F']
GREY = (0.45, 0.47, 0.50)
STATUS = {'do_now': 'Do now', 'plan_only': 'Plan only', 'waiting': 'Waiting', 'done': 'Done'}


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


# "2026-10-19 Dev sync - SEO": the folder's and the doc's name.
def folder_name(m: dict, brief: dict) -> str:
    title = str((brief or {}).get('title') or m.get('title') or 'Meeting')
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


# ── The Report tab: HTML Drive converts ──────────────────────────────────────

def report_html(m: dict, brief: dict, shot_data: dict) -> str:
    e = html.escape
    stats = talk_stats(m)
    roles = brief.get('roles') or {}
    shots = sorted(m.get('screenshots', []), key=lambda s: s['at'])

    def at(sec):
        return f" <i style='color:#777'>({clock(sec)})</i>" if sec is not None else ''

    def img(shot, width):
        data = shot_data.get(shot['image'])
        if not data:
            return ''
        src = 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')
        return (f"<p style='text-align:center'><img src='{src}' width='{width}'></p>"
                f"<p style='text-align:center;color:#777;font-size:9pt'><i>Screenshot at {clock(shot['at'])} · {e(shot.get('caption') or '')}</i></p>")

    out = [f"<h1>{e(brief.get('title') or m['title'])}</h1>",
           f"<p style='color:#666'>{e(_day(m))} · {clock(_length(m))} · {e(', '.join(_who(m, s['id'])['name'] for s in stats))}</p>",
           "<h2>Summary</h2>"] + [f"<p>{e(p)}</p>" for p in brief.get('tldr') or []]
    if brief.get('decisions'):
        out += ["<h2>Decisions</h2>", "<ol>"] + [f"<li><b>{e(d['id'])}</b> {e(d['text'])}{at(d.get('at'))}</li>" for d in brief['decisions']] + ["</ol>"]
    if brief.get('action_items'):
        out += ["<h2>Action items</h2>", "<table border='1' cellpadding='5'><tr><th>#</th><th>Task</th><th>Owner</th><th>Status</th><th>At</th></tr>"]
        for a in brief['action_items']:
            out.append(f"<tr><td>{e(a['id'])}</td><td>{e(a['task'])}</td><td>{e(a.get('owner') or 'Not set')}</td>"
                       f"<td>{e(STATUS.get(a.get('status'), a.get('status') or ''))}</td><td>{clock(a.get('at'))}</td></tr>")
        out.append("</table>")
        if all(not a.get('due') for a in brief['action_items']):
            out.append("<p><i>No deadlines were stated.</i></p>")
    if brief.get('open_questions'):
        out += ["<h2>Open questions</h2>", "<ul>"] + [f"<li><b>{e(q['id'])}</b> {e(q['text'])}{at(q.get('at'))}</li>" for q in brief['open_questions']] + ["</ul>"]
    if brief.get('parked'):
        out += ["<h2>Parked</h2>", "<ul>"] + [f"<li><b>{e(p['id'])}</b> {e(p['text'])}</li>" for p in brief['parked']] + ["</ul>"]
    if stats:
        out += ["<h2>Who spoke</h2>", f"<p><img src='{share_chart(m, stats)}' width='520'></p>",
                "<h3>Who held the floor, minute by minute</h3>",
                "<p style='color:#666'>Seconds spoken in each minute, stacked. A minute passes 60 s when people talk over each other.</p>",
                f"<p><img src='{timeline_chart(m, stats)}' width='620'></p>",
                "<table border='1' cellpadding='5'><tr><th>Speaker</th><th>Talk time</th><th>Share</th><th>Turns</th><th>Role in this meeting</th></tr>"]
        out += [f"<tr><td>{e(_who(m, s['id'])['name'])}</td><td>{clock(s['seconds'])}</td><td>{round(s['share'] * 100)}%</td>"
                f"<td>{s['turns']}</td><td>{e(roles.get(s['id'], ''))}</td></tr>" for s in stats] + ["</table>"]
    if brief.get('topics'):
        out.append("<h2>How the meeting went</h2>")
        for t in brief['topics']:
            out.append(f"<h3>{clock(t['start'])}–{clock(t['end'])} · {e(t['title'])}</h3>")
            if t.get('lead'):
                out.append(f"<p><i>{e(t['lead'])}</i></p>")
            if t.get('points'):
                out.append("<ul>" + ''.join(f"<li>{e(p)}</li>" for p in t['points']) + "</ul>")
            for q in t.get('quotes') or []:
                out.append(f"<blockquote><p dir='rtl'>{e(q['text'])}</p><p><i>{e(q.get('who') or '')} · {clock(q.get('at'))}</i></p></blockquote>")
            for s in shots:
                if t['start'] <= s['at'] < t['end']:
                    out.append(img(s, 460))
    if brief.get('notes'):
        out.append(f"<p style='color:#777'><i>Transcription notes: {e('; '.join(brief['notes']))}.</i></p>")
    out.append("<p style='color:#777'><i>The Quick summary tab has the bottom lines; the Transcript tab has every word.</i></p>")
    return "<html><head><meta charset='utf-8'></head><body>" + "\n".join(out) + "</body></html>"


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
    paras = [(brief.get('title') or m['title'], 'title', []),
             (f"{_day(m)} · {clock(_length(m))} · {', '.join(_who(m, s['id'])['name'] for s in stats)}", 'meta', []),
             ('In short', 'heading', [])]
    paras += [(p, 'normal', []) for p in brief.get('tldr') or []]
    if brief.get('decisions'):
        paras.append(('Decisions', 'heading', []))
        paras += [(f"{d['text']} ({clock(d.get('at'))})", 'bullet', []) for d in brief['decisions']]
    if brief.get('action_items'):
        paras.append(('Action items', 'heading', []))
        for a in brief['action_items']:
            owner = a.get('owner') or 'Owner not set'
            line = f"{a['task']} · {owner} · {STATUS.get(a.get('status'), a.get('status') or '')}"
            if a.get('due'):
                line += f" · due {a['due']}"
            paras.append((line, 'bullet', [(u16(a['task']) + 3, u16(owner), (0.12, 0.15, 0.17), True, None)]))
    if brief.get('open_questions'):
        paras.append(('Open questions', 'heading', []))
        paras += [(q['text'], 'bullet', []) for q in brief['open_questions']]
    if brief.get('parked'):
        paras.append(('Parked', 'heading', []))
        paras += [(p['text'], 'bullet', []) for p in brief['parked']]
    if brief.get('topics'):
        paras.append(('Topics', 'heading', []))
        for t in brief['topics']:
            head = f"{clock(t['start'])}–{clock(t['end'])} {t['title']}"
            paras.append((f"{head}: {t.get('lead') or ''}".rstrip(': '), 'bullet', [(0, u16(head), (0.12, 0.15, 0.17), True, None)]))
    paras.append(('The Report tab has the charts, quotes and screenshots; the Transcript tab has every word.', 'note', []))
    return _write(tab, paras)


# The Transcript tab: speakers in their colours, time in grey, right to left,
# topic headings at the moments they start.
def transcript_requests(m: dict, brief: dict, tab: str) -> list:
    turns = sorted(m.get('turns', []), key=lambda t: t['start'])
    topics = sorted((brief or {}).get('topics') or [], key=lambda t: t['start'])
    stats = talk_stats(m)
    paras = [(f"{brief.get('title') or m['title']}: transcript", 'title', []),
             (f"{_day(m)} · {clock(_length(m))} · {len(turns)} turns · verbatim as Koli heard it", 'meta', [])]
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

# Builds (or replaces) the meeting's doc and remembers where it is. Returns
# {'folder_id', 'folder_url', 'doc_id', 'doc_url', 'exported_at'}.
def export(store, meeting_id: str, drive=None, docs=None) -> dict:
    brief = store.load_brief(meeting_id)
    if not brief:
        raise ValueError("This meeting has no summary yet, so there is nothing to export")
    if drive is None:
        from ..drive import api as drive
    if docs is None:
        from ..drive import docs
    m = store.load(meeting_id)
    name = folder_name(m, brief)
    folder = drive.ensure_folder(f"{ROOT_FOLDER}/{name}")
    old = (m.get('drive') or {}).get('doc_id')
    if old:
        try:
            drive.delete(old)
        except Exception as e:
            logger.info(f"The previous doc wasn't trashed ({e}); a new one is made anyway")
    shot_data = {}
    for s in m.get('screenshots', []):
        try:
            shot_data[s['image']] = store.shot_path(meeting_id, s['image']).read_bytes()
        except FileNotFoundError:
            pass
    page = report_html(m, brief, shot_data)
    doc_id = drive.upload_bytes(page.encode('utf-8'), name, 'text/html', folder_id=folder, convert_to=DOC_MIME)['id']
    first = docs.get(doc_id)['tabs'][0]['tabProperties']['tabId']
    replies = docs.batch_update(doc_id, [
        {'updateDocumentTabProperties': {'tabProperties': {'tabId': first, 'title': 'Report'}, 'fields': 'title'}},
        {'addDocumentTab': {'tabProperties': {'title': 'Quick summary', 'index': 0}}},
        {'addDocumentTab': {'tabProperties': {'title': 'Transcript'}}},
    ])['replies']
    quick = replies[1]['addDocumentTab']['tabProperties']['tabId']
    transcript = replies[2]['addDocumentTab']['tabProperties']['tabId']
    docs.batch_update(doc_id, quick_requests(m, brief, quick) + transcript_requests(m, brief, transcript))
    info = {'folder_id': folder, 'folder_url': f"https://drive.google.com/drive/folders/{folder}", 'doc_id': doc_id,
            'doc_url': str(drive.info(doc_id).get('webViewLink') or f"https://docs.google.com/document/d/{doc_id}/edit"),
            'exported_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
    m = store.load(meeting_id)
    m['drive'] = info
    store.save(m)
    logger.info(f"Meeting {meeting_id} saved to Google Drive as {doc_id}")
    return info
