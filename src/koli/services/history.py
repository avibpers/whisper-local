# services/history.py
# The History screen: search and page through transcripts.jsonl, export it,
# delete one entry or clear it, read an audio or video file for transcribing
# (decode_file), and turn a misheard word into a permanent replacement.
# Replacements
# go through the settings service so the running app and the file stay in step.

import datetime
import logging
from pathlib import Path

from .. import transcript_log
from ..utils import get_user_app_data_path

logger = logging.getLogger(__name__)

PERIOD_DAYS = {'today': 0, 'week': 7, 'month': 30}


def _since(period: str):
    if period not in PERIOD_DAYS:
        return None
    days = PERIOD_DAYS[period]
    start = datetime.date.today() - datetime.timedelta(days=days)
    return start.isoformat()


# Newest first. `text` matches anywhere in the transcript (case-insensitive),
# `app` is an exe name, `period` is today|week|month|all.
def query(text: str = '', app: str = '', period: str = 'all', offset: int = 0, limit: int = 50) -> dict:
    entries = transcript_log.load_transcripts()
    needle = (text or '').strip().lower()
    since = _since(period)
    matches = []
    for e in entries:
        if needle and needle not in (e.get('text') or '').lower():
            continue
        if app and (e.get('app') or '').lower() != app.lower():
            continue
        if since and (e.get('timestamp') or '') < since:
            continue
        matches.append(e)
    page = matches[offset:offset + limit]
    return {'items': page, 'total': len(matches), 'all': len(entries)}


def apps() -> list:
    seen = {}
    for e in transcript_log.load_transcripts():
        a = (e.get('app') or '').lower()
        if a:
            seen[a] = seen.get(a, 0) + 1
    return [a for a, _ in sorted(seen.items(), key=lambda kv: -kv[1])]


def export(path: str) -> str:
    from ..stats import export_transcripts
    code = export_transcripts(path)
    if code != 0:
        raise RuntimeError("Nothing to export yet" if not transcript_log.load_transcripts() else "Export failed")
    return path


# Audio and video files the History screen offers to transcribe.
FILE_TYPES = ('Audio and video (*.mp3;*.m4a;*.wav;*.ogg;*.opus;*.flac;*.aac;*.wma;*.webm;*.mp4;*.mov;*.mkv)',
              'All files (*.*)')


# A file's sound as Whisper wants it: 16 kHz mono float32, through PyAV
# (any format FFmpeg reads, the sound track of a video too).
def decode_file(path: str):
    import av
    import numpy as np
    chunks = []
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise RuntimeError("That file has no sound")
        resampler = av.AudioResampler(format='flt', layout='mono', rate=16000)
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        raise RuntimeError("No sound could be read from that file")
    return np.concatenate(chunks).astype(np.float32)


# One entry, found by when it was written and its text.
def delete(timestamp: str, text: str) -> bool:
    if not transcript_log.delete_entry(str(timestamp or ''), str(text or '')):
        raise RuntimeError("That dictation isn't in History anymore")
    return True


def clear() -> int:
    path = Path(get_user_app_data_path()) / transcript_log._LOG_FILE
    count = len(transcript_log.load_transcripts())
    with transcript_log._write_lock:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    logger.info(f"Transcript history cleared ({count} entries)")
    return count


# Add or update a replacement so `frm` is always written as `to`. Matching on
# "from" is case-insensitive, like corrections.add_replacement.
def fix_everywhere(settings, frm: str, to: str) -> bool:
    frm = (frm or '').strip()
    to = to or ''
    if not frm or frm == to:
        raise ValueError("Enter the misheard text and what it should be")
    items = [dict(i) for i in (settings.get('postprocess.replacements') or []) if isinstance(i, dict)]
    for item in items:
        if str(item.get('from', '')).lower() == frm.lower():
            item['from'], item['to'] = frm, to
            break
    else:
        items.append({'from': frm, 'to': to})
    settings.set('postprocess.replacements', items)
    return True


def suggest_words(settings, limit: int = 20) -> list:
    from ..dictionary import suggest_hotwords
    texts = [e.get('text', '') for e in transcript_log.load_transcripts()]
    known = set(settings.get('whisper.hotwords') or [])
    return [{'word': w, 'count': c} for w, c in suggest_hotwords(texts, known=known, min_count=3, limit=limit)]
