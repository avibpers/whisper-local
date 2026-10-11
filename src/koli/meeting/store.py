# meeting/store.py
# One JSON file per meeting under <data>/meetings/: who spoke, what they said
# and when, plus the voiceprints that keep speaker labels stable when a
# meeting resumes. Written throttled while a meeting runs, so a crash loses
# seconds, not the meeting.

import datetime
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from ..utils import get_user_app_data_path

logger = logging.getLogger(__name__)

FOLDER = 'meetings'
SAVE_INTERVAL = 5.0
EXPORT_FORMATS = ('txt', 'md')


# ── Shapes ───────────────────────────────────────────────────────────────────

def meetings_dir() -> Path:
    return Path(get_user_app_data_path()) / FOLDER


def new_id(now: datetime.datetime) -> str:
    return now.strftime('%Y-%m-%d-%H%M%S')


# `source` is {'app', 'kind', 'title', 'code'} as the detector reports it.
def new_meeting(meeting_id: str, title: str, source: dict, started_at: datetime.datetime,
                language: str = 'auto') -> dict:
    return {
        'id': meeting_id,
        'title': title,
        'source': dict(source),
        'started_at': started_at.isoformat(timespec='seconds'),
        'ended_at': None,
        'language': language,
        'speakers': [{'id': 'you', 'name': 'You', 'colour': 0}],
        'turns': [],
        'gaps': [],
    }


def started_ts(meeting: dict) -> float:
    return datetime.datetime.fromisoformat(meeting['started_at']).timestamp()


# How long the meeting ran: to its end, or to its last turn while it is open.
def duration_s(meeting: dict) -> float:
    if meeting.get('ended_at'):
        return datetime.datetime.fromisoformat(meeting['ended_at']).timestamp() - started_ts(meeting)
    return float(meeting['turns'][-1]['end']) if meeting['turns'] else 0.0


# What the list shows: never the turns or the voiceprints.
def header(meeting: dict) -> dict:
    return {
        'id': meeting['id'], 'title': meeting['title'], 'source': meeting['source'],
        'started_at': meeting['started_at'], 'ended_at': meeting.get('ended_at'),
        'duration_s': round(duration_s(meeting)),
        'speakers': [s['name'] for s in meeting['speakers']],
        'turns': len(meeting['turns']),
    }


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# Speaker-tagged lines, like Read.ai's text view.
def export_text(meeting: dict, fmt: str) -> str:
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"Unknown export format: {fmt}")
    names = {s['id']: s['name'] for s in meeting['speakers']}
    when = meeting['started_at'][:16].replace('T', ' ')
    if fmt == 'md':
        lines = [f"# {meeting['title']}", '', f"{when} · {_clock(duration_s(meeting))}", '']
    else:
        lines = [meeting['title'], when, '']
    for turn in meeting['turns']:
        who = names.get(turn['speaker'], turn['speaker'])
        who = f"**{who}**" if fmt == 'md' else who
        lines.append(f"[{_clock(turn['start'])}] {who}: {turn['text']}")
    return '\n'.join(lines) + '\n'


# ── Files ────────────────────────────────────────────────────────────────────

class MeetingStore:
    def __init__(self, folder: Optional[Path] = None):
        self.folder = Path(folder) if folder else meetings_dir()
        self._lock = threading.Lock()

    def path(self, meeting_id: str) -> Path:
        return self.folder / f"{meeting_id}.json"

    # Written to a temp file first, so a crash mid-write never leaves half a
    # meeting behind.
    def save(self, meeting: dict) -> Path:
        path = self.path(meeting['id'])
        with self._lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(meeting, ensure_ascii=False, indent=1), encoding='utf-8')
            os.replace(tmp, path)
        return path

    def load(self, meeting_id: str) -> dict:
        with self._lock:
            return json.loads(self.path(meeting_id).read_text(encoding='utf-8'))

    # Newest first. A file that can't be read is skipped, not fatal.
    def list(self) -> list:
        out = []
        with self._lock:
            files = sorted(self.folder.glob('*.json'), reverse=True) if self.folder.exists() else []
            for path in files:
                try:
                    out.append(header(json.loads(path.read_text(encoding='utf-8'))))
                except Exception as e:
                    logger.warning(f"Skipping unreadable meeting {path.name}: {e}")
        return out

    def delete(self, meeting_id: str) -> bool:
        with self._lock:
            try:
                self.path(meeting_id).unlink()
                return True
            except FileNotFoundError:
                return False

    def export(self, meeting_id: str, fmt: str) -> str:
        return export_text(self.load(meeting_id), fmt)


# The first mark writes at once (so the file exists from the start); later
# marks wait until `interval` has passed since the last write, then write the
# newest version. flush() writes now, for the end of a meeting.
class ThrottledSaver:
    def __init__(self, store: MeetingStore, interval: float = SAVE_INTERVAL):
        self.store = store
        self.interval = interval
        self._lock = threading.Lock()
        self._pending = None
        self._timer = None
        self._last_write = 0.0

    def mark(self, meeting: dict) -> None:
        with self._lock:
            self._pending = meeting
            due = self._last_write + self.interval - time.monotonic()
            if due <= 0:
                self._write_locked()
                return
            if self._timer is None:
                self._timer = threading.Timer(due, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._pending is not None:
                self._write_locked()

    def _write_locked(self) -> None:
        meeting, self._pending = self._pending, None
        try:
            self.store.save(meeting)
        except Exception as e:
            logger.error(f"Could not save meeting {meeting.get('id')}: {e}")
        self._last_write = time.monotonic()
