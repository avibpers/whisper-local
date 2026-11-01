# transcript_log.py
# Append-only journal of every successful transcription. Backs the
# `--history` browser window so users can search their dictation history.
# Separate from `stats.jsonl` (which only stores metadata) — this file
# contains the actual transcribed text and is auto-rotated to keep the
# last ~2000 entries. It also tells which language you write in a chat
# (chat_language), for Whisper when it can't tell from the audio.

import datetime
import json
import logging
import threading
from pathlib import Path

from .utils import get_user_app_data_path

logger = logging.getLogger(__name__)

# Newline-delimited JSON — one transcript per line.
_LOG_FILE = 'transcripts.jsonl'
_MAX_ENTRIES = 2000

# Serialise append + rotate. Continuous mode and rapid voice commands can fire
# deliveries back-to-back from different threads; without this an append could
# interleave with the read-truncate-rewrite rotation and drop or corrupt lines.
_write_lock = threading.Lock()


# Called from state_manager._transcription_pipeline after every successful
# delivery. Silent no-op for empty text (which means a failed/silent recording).
# `window` is the foreground.get_foreground_app() dict at delivery time. Besides
# the app, it keeps what tells one conversation from another: the window title
# usually names the chat, channel, tab or file ("@Efrat - Discord"), and the
# window id + pid separate two windows of the same app. Windows reuses window
# ids after a window closes, so the pid is kept alongside to tell them apart.
# `clip_id`: the kept clip of this dictation ("Learn from my dictations"), so
# History can find its audio.
# `original`: what the bubble's engine heard, when you changed it before it
# was written; `cancelled`: a bubble's text you threw away (kept findable).
def record_transcript(text: str, window: dict | None = None, duration_s: float = 0.0,
                      language: str = '', clip_id: str | None = None, original: str = '',
                      cancelled: bool = False):
    if not text:
        return
    window = window or {}
    entry = {
        'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
        'text': text,
        'app': window.get('exe', ''),
        'duration_s': round(duration_s, 2),
        'chars': len(text),
    }
    if language:
        entry['language'] = language
    if clip_id:
        entry['clip_id'] = clip_id
    if original and original != text:
        entry['original'] = original
    if cancelled:
        entry['cancelled'] = True
    # Window identity — only what this platform reports (macOS has no window id,
    # and its "title" is the app name until a per-window probe exists).
    if window.get('title'):
        entry['title'] = window['title']
    if window.get('hwnd'):
        entry['window_id'] = window['hwnd']
    if window.get('pid'):
        entry['pid'] = window['pid']
    path = Path(get_user_app_data_path()) / _LOG_FILE
    try:
        with _write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            _maybe_rotate(path)
    except Exception as e:
        logger.debug(f"Transcript log write failed: {e}")


# Reads the journal back into a list of dicts, newest-first. Used by the
# history window. Silently skips malformed lines so a single corrupted entry
# doesn't break the whole UI.
def load_transcripts() -> list:
    path = Path(get_user_app_data_path()) / _LOG_FILE
    entries = []
    if not path.exists():
        return entries
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get('text'):
                        entries.append(obj)
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        logger.warning(f"Failed to load transcripts: {e}")
    return list(reversed(entries))


# The language you've been writing in this chat: the most common one among
# your last `limit` dictations into this app and window title (the title
# names the chat, channel or file), newer ones breaking a tie. Only `allowed`
# languages count; '' when the chat has none. Only the chat, never the whole
# app: a replay of 443 unsure clips (2026-11-01) fixed 38 and broke 1 with the
# chat, but broke 6 when the app filled in (a quick Hebrew "תודה" in Cursor,
# where you mostly write English).
CHAT_HISTORY = 5


def chat_language(app: str, title: str, allowed, entries: list | None = None, limit: int = CHAT_HISTORY) -> str:
    if not app or not title:
        return ''
    recent = []
    for entry in entries if entries is not None else load_transcripts():   # newest first
        if entry.get('app') != app or entry.get('title') != title or entry.get('cancelled'):
            continue
        if entry.get('language') in allowed:
            recent.append(entry['language'])
            if len(recent) >= limit:
                break
    if not recent:
        return ''
    counts = {lang: recent.count(lang) for lang in recent}
    top = max(counts.values())
    return next(lang for lang in recent if counts[lang] == top)   # the newest of the most common


# Cap the journal at roughly _MAX_ENTRIES. We allow it to grow to 1.2× before
# truncating to amortise the cost — counting lines on every write would dominate
# the actual transcription work.
def _maybe_rotate(path: Path):
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
        if len(lines) > _MAX_ENTRIES * 1.2:
            trimmed = lines[-_MAX_ENTRIES:]
            path.write_text('\n'.join(trimmed) + '\n', encoding='utf-8')
    except Exception:
        pass
