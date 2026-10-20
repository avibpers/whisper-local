# meeting/store.py
# One JSON file per meeting under <data>/meetings/: who spoke, what they said
# and when, the screenshots taken during it, plus the voiceprints that keep
# speaker labels stable when a meeting resumes. Written throttled while a
# meeting runs, so a crash loses seconds, not the meeting. With
# meeting.keep_audio on, the call's audio is kept beside it in "<id>.audio/";
# screenshots are PNGs in "<id>.shots/". The brief (the summary written from
# the meeting: decisions, action items, topics) is "<id>.brief.json".

import datetime
import json
import logging
import os
import shutil
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np

from ..utils import get_user_app_data_path

logger = logging.getLogger(__name__)

FOLDER = 'meetings'
SAVE_INTERVAL = 5.0
EXPORT_FORMATS = ('txt', 'md')
BRIEF_SUFFIX = '.brief.json'


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
        'screenshots': [],
        'gaps': [],
    }


def started_ts(meeting: dict) -> float:
    return datetime.datetime.fromisoformat(meeting['started_at']).timestamp()


# How long the meeting ran: to its end, or to its last turn or screenshot
# while it is open.
def duration_s(meeting: dict) -> float:
    if meeting.get('ended_at'):
        return datetime.datetime.fromisoformat(meeting['ended_at']).timestamp() - started_ts(meeting)
    last_turn = float(meeting['turns'][-1]['end']) if meeting['turns'] else 0.0
    last_shot = max((float(s['at']) for s in meeting.get('screenshots', [])), default=0.0)
    return max(last_turn, last_shot)


# What the list shows: never the turns or the voiceprints.
# All the words of a summary, for search: titles, lists, agenda, topics.
def _brief_text(brief: dict) -> str:
    parts = []

    def walk(value):
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, dict):
            for k, v in value.items():
                if k not in ('id', 'at', 'start', 'end', 'version', 'generator', 'template', 'written', 'language', 'status'):
                    walk(v)
    walk(brief)
    return ' '.join(parts)


# About 70 characters around the first place the term appears.
def _snippet(text: str, term: str, width: int = 70) -> str:
    i = text.lower().find(term)
    if i < 0:
        return text[:width]
    start = max(0, i - width // 3)
    piece = text[start:start + width].strip()
    return ('…' if start > 0 else '') + piece + ('…' if start + width < len(text) else '')


def header(meeting: dict) -> dict:
    return {
        'id': meeting['id'], 'title': meeting['title'], 'source': meeting['source'],
        'started_at': meeting['started_at'], 'ended_at': meeting.get('ended_at'),
        'duration_s': round(duration_s(meeting)),
        'speakers': [s['name'] for s in meeting['speakers']],
        'turns': len(meeting['turns']),
        'screenshots': len(meeting.get('screenshots', [])),
    }


# ── Screenshots ──────────────────────────────────────────────────────────────
# A screenshot entry is {'at', 'image', 'caption', 'description'}: the offset
# it was taken at, its file name in "<id>.shots/", the user's caption and a
# slot for a later AI description. It is a list of its own, not a turn, so the
# session's append-only turns stay untouched; every view merges the two by
# time with rows_by_time().

# "0279.40.png": offsets sort the folder in meeting order.
def shot_name(offset: float) -> str:
    return f"{float(offset):07.2f}.png"


# Turns and screenshots in time order: a screenshot sits before the first
# turn that starts after it. The file keeps turns in arrival order (the mic
# and the call are transcribed apart, and transcription lags), so the order
# is made when shown, never when stored.
def rows_by_time(meeting: dict) -> list:
    shots = sorted(meeting.get('screenshots', []), key=lambda s: float(s['at']))
    rows, i = [], 0
    for turn in sorted(meeting['turns'], key=lambda t: float(t['start'])):
        while i < len(shots) and float(shots[i]['at']) <= float(turn['start']):
            rows.append({'kind': 'shot', **shots[i]})
            i += 1
        rows.append({'kind': 'turn', **turn})
    rows.extend({'kind': 'shot', **s} for s in shots[i:])
    return rows


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# Speaker-tagged lines, like Read.ai's text view, with screenshots at their
# moment. `shots_folder` is where the md's image links point (the bridge
# copies the PNGs there, next to the exported file).
def export_text(meeting: dict, fmt: str, shots_folder: str = '') -> str:
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"Unknown export format: {fmt}")
    names = {s['id']: s['name'] for s in meeting['speakers']}
    when = meeting['started_at'][:16].replace('T', ' ')
    folder = shots_folder or f"{meeting['id']}-shots"
    if fmt == 'md':
        lines = [f"# {meeting['title']}", '', f"{when} · {_clock(duration_s(meeting))}", '']
    else:
        lines = [meeting['title'], when, '']
    for row in rows_by_time(meeting):
        if row['kind'] == 'shot':
            label = row.get('caption') or row['image']
            if fmt == 'md':
                lines.append(f"![{label}]({folder}/{row['image']})")
            else:
                lines.append(f"[{_clock(row['at'])}] Screenshot: {label}")
            continue
        who = names.get(row['speaker'], row['speaker'])
        who = f"**{who}**" if fmt == 'md' else who
        lines.append(f"[{_clock(row['start'])}] {who}: {row['text']}")
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
                if path.name.endswith(BRIEF_SUFFIX):
                    continue
                try:
                    head = header(json.loads(path.read_text(encoding='utf-8')))
                    head['brief'] = (self.folder / f"{head['id']}{BRIEF_SUFFIX}").exists()
                    out.append(head)
                except Exception as e:
                    logger.warning(f"Skipping unreadable meeting {path.name}: {e}")
        return out

    # Meetings that hold every word of the query, in their title, speakers,
    # summary, screenshot captions or transcript; newest first. Each result
    # says where it matched, with a short snippet and, for the transcript, the
    # second of the first matching turn so the page can open it there.
    def search(self, query: str, limit: int = 50) -> list:
        terms = [w for w in str(query or '').lower().split() if w]
        if not terms:
            return []
        out = []
        for head in self.list():
            try:
                m = self.load(head['id'])
            except Exception:
                continue
            brief = self.load_brief(head['id']) or {}
            title = str(m.get('title') or '')
            speakers = ' '.join(str(s.get('name') or '') for s in m.get('speakers', []))
            summary = _brief_text(brief)
            captions = ' '.join(str(s.get('caption') or '') for s in m.get('screenshots', []))
            turns = [(t.get('start', 0), str(t.get('text') or '')) for t in m.get('turns', [])]
            everything = ' '.join([title, speakers, summary, captions] + [text for _, text in turns]).lower()
            if not all(term in everything for term in terms):
                continue
            hits = sum(everything.count(term) for term in terms)
            hit = {'id': head['id'], 'title': title, 'started_at': head.get('started_at'), 'hits': hits,
                   'where': 'title', 'snippet': title, 'at': None}
            if not all(term in (title + ' ' + speakers).lower() for term in terms):
                first = terms[0]
                turn = next(((at, text) for at, text in turns if first in text.lower()), None)
                if turn:
                    hit.update(where='transcript', snippet=_snippet(turn[1], first), at=turn[0])
                elif first in summary.lower():
                    hit.update(where='summary', snippet=_snippet(summary, first))
                elif first in captions.lower():
                    hit.update(where='screenshot', snippet=_snippet(captions, first))
            out.append(hit)
            if len(out) >= limit:
                break
        return out

    # Deleting a meeting deletes its kept audio and its screenshots too.
    def delete(self, meeting_id: str) -> bool:
        with self._lock:
            shutil.rmtree(self.audio_dir(meeting_id), ignore_errors=True)
            shutil.rmtree(self.shots_dir(meeting_id), ignore_errors=True)
            self.brief_path(meeting_id).unlink(missing_ok=True)
            try:
                self.path(meeting_id).unlink()
                return True
            except FileNotFoundError:
                return False

    # ── Kept audio (meeting.keep_audio, off by default) ──────────────────────
    # Each chunk as a 16 kHz WAV in "<id>.audio/", named by its wall-clock
    # start and stream, so a meeting can be replayed to tune how speakers are
    # told apart. Everyone's voice is in it; only kept when you turn it on.

    def audio_dir(self, meeting_id: str) -> Path:
        return self.folder / f"{meeting_id}.audio"

    def save_audio(self, meeting_id: str, chunk: dict) -> Path:
        folder = self.audio_dir(meeting_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{float(chunk['t0']):.2f}-{chunk['stream']}.wav"
        samples = np.clip(np.asarray(chunk['audio'], dtype=np.float32).reshape(-1), -1.0, 1.0)
        with wave.open(str(path), 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((samples * 32767).astype(np.int16).tobytes())
        return path

    # ── The brief: "<id>.brief.json" ─────────────────────────────────────────
    # Written by the summary step (the AI, later), read by the Meetings
    # screen's Summary tab and the exports. A missing or broken file reads as
    # "no summary yet", never as an error.

    def brief_path(self, meeting_id: str) -> Path:
        return self.folder / f"{meeting_id}{BRIEF_SUFFIX}"

    def load_brief(self, meeting_id: str) -> Optional[dict]:
        try:
            return json.loads(self.brief_path(meeting_id).read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.warning(f"Unreadable brief for {meeting_id}: {e}")
            return None

    # The brief's title becomes the meeting's title ("Meeting" or "Discord"
    # until then), so the list, the header and the export names say what the
    # meeting was about. An empty title leaves the meeting's as it is.
    # The meeting takes the brief's title, unless the template's title is a
    # fixed one ("Board Meeting Summary") that would name every meeting alike.
    def save_brief(self, meeting_id: str, brief: dict, rename_meeting: bool = True) -> Path:
        path = self.brief_path(meeting_id)
        with self._lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(brief, ensure_ascii=False, indent=1), encoding='utf-8')
            os.replace(tmp, path)
        title = str(brief.get('title') or '').strip()
        if rename_meeting and title and self.path(meeting_id).exists():
            meeting = self.load(meeting_id)
            if meeting.get('title') != title:
                meeting['title'] = title
                self.save(meeting)
        return path

    # ── Screenshots: PNGs in "<id>.shots/" ───────────────────────────────────

    def shots_dir(self, meeting_id: str) -> Path:
        return self.folder / f"{meeting_id}.shots"

    # A free name in the folder: the offset's name, or -2, -3 when taken.
    @staticmethod
    def _free_name(folder: Path, name: str) -> str:
        stem, n = name[:-4], 1
        while (folder / name).exists():
            n += 1
            name = f"{stem}-{n}.png"
        return name

    # Saves the image as PNG, whatever mode Pillow handed over; returns the name.
    def save_shot(self, meeting_id: str, offset: float, image) -> str:
        folder = self.shots_dir(meeting_id)
        with self._lock:
            folder.mkdir(parents=True, exist_ok=True)
            name = self._free_name(folder, shot_name(offset))
            image.save(folder / name, format='PNG')
        return name

    # The file behind an entry's name; a name that isn't a plain file name
    # (a path, "..") is treated as missing, so the bridge never reads outside
    # the folder.
    def shot_path(self, meeting_id: str, name: str) -> Path:
        if not name or Path(name).name != name:
            raise FileNotFoundError(name)
        path = self.shots_dir(meeting_id) / name
        if not path.is_file():
            raise FileNotFoundError(name)
        return path

    # Moves the entries' files into another meeting's folder (continue
    # previous, split). A clash gets -2, -3 and the entry's name is updated
    # in place. An empty source folder is removed.
    def move_shots(self, from_id: str, to_id: str, entries: list) -> None:
        src, dst = self.shots_dir(from_id), self.shots_dir(to_id)
        with self._lock:
            for entry in entries:
                path = src / entry['image']
                if not path.is_file():
                    continue
                dst.mkdir(parents=True, exist_ok=True)
                name = self._free_name(dst, entry['image'])
                shutil.move(str(path), str(dst / name))
                entry['image'] = name
            if src.is_dir() and not any(src.iterdir()):
                src.rmdir()

    def export(self, meeting_id: str, fmt: str, shots_folder: str = '') -> str:
        return export_text(self.load(meeting_id), fmt, shots_folder)


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
