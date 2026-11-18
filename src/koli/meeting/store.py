# meeting/store.py
# One JSON file per meeting under <data>/meetings/: who spoke, what they said
# and when, the screenshots taken during it, plus the voiceprints that keep
# speaker labels stable when a meeting resumes. Written throttled while a
# meeting runs, so a crash loses seconds, not the meeting. With
# meeting.keep_audio on, the call's audio is kept beside it in "<id>.audio/";
# screenshots are PNGs in "<id>.shots/". The brief (the summary written from
# the meeting: decisions, action items, topics) is "<id>.brief.json"; the
# conversation with the AI about the meeting (its Chat tab) is "<id>.chat.json".

import datetime
import json
import logging
import math
import os
import re
import shutil
import threading
import time
import wave
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from ..utils import get_user_app_data_path

logger = logging.getLogger(__name__)

FOLDER = 'meetings'
SAVE_INTERVAL = 5.0
EXPORT_FORMATS = ('txt', 'md')
BRIEF_SUFFIX = '.brief.json'
CHAT_SUFFIX = '.chat.json'
INDEX_SUFFIX = '.index.npz'

# Word search (measured in the lab): whole words, Hebrew prefixes, rare words count most.
WORD_RE = re.compile(r"[\w״\"׳'-]+")
HEBREW_PREFIXES = 'והבלמשכ'   # one of these glued to the front of a word is not part of it
COMMON_SHARE = 0.3                   # a word in more than this share of meetings tells nothing...
COMMON_MIN_MEETINGS = 3              # ...but only once more than this many meetings hold it (1-3 meetings stay searchable)


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


# ── Who was there ────────────────────────────────────────────────────────────
# Everyone in the meeting, said something or not: the voices heard, the
# people the call app showed in the call (m['call']['people']) and the names
# you added by hand (m['attendees']). A name that is also a speaker's is one
# person. Each row: {'name', 'speaker' (id or None), 'seconds', 'turns',
# 'spoke'}, the voices by talk time first, then the silent ones.

def _norm(name) -> str:
    return ' '.join(str(name or '').split()).lower()


def roster(meeting: dict) -> list:
    by_id = {}
    for s in meeting.get('speakers', []):
        by_id[s['id']] = {'name': s.get('name') or s['id'], 'speaker': s['id'], 'seconds': 0.0, 'turns': 0}
    for t in meeting.get('turns', []):
        # A turn whose speaker entry went missing still counts, under "Speaker N".
        row = by_id.setdefault(t['speaker'], {'name': 'Speaker ' + t['speaker'].lstrip('s'),
                                              'speaker': t['speaker'], 'seconds': 0.0, 'turns': 0})
        row['seconds'] += max(0.0, float(t['end']) - float(t['start']))
        row['turns'] += 1
    rows = sorted((r for r in by_id.values() if r['turns'] or r['speaker'] == 'you'), key=lambda r: -r['seconds'])
    known = {_norm(r['name']) for r in rows}
    present = [p.get('name') for p in (meeting.get('call') or {}).get('people') or []] + list(meeting.get('attendees') or [])
    for name in present:
        if _norm(name) and _norm(name) not in known:
            known.add(_norm(name))
            rows.append({'name': str(name).strip(), 'speaker': None, 'seconds': 0.0, 'turns': 0})
    for r in rows:
        r['seconds'] = round(r['seconds'], 1)
        r['spoke'] = r['turns'] > 0
    return rows


# All the words of a summary (or one item of it), without ids, times and layout fields.
def plain_text(value) -> str:
    parts = []

    def walk(item):
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, list):
            for v in item:
                walk(v)
        elif isinstance(item, dict):
            for k, v in item.items():
                if k not in ('id', 'at', 'start', 'end', 'version', 'generator', 'template', 'written', 'language', 'status'):
                    walk(v)
    walk(value)
    return ' '.join(parts)


# All the words of a summary, for search: titles, lists, agenda, topics.
def _brief_text(brief: dict) -> str:
    return plain_text(brief)


# Every whole word of a text, lower-cased.
def words_of(text: str) -> list:
    return WORD_RE.findall(str(text or '').lower())


# A word and, for a Hebrew word longer than 3 letters that starts with a prefix letter, the word without it.
def word_variants(word: str) -> set:
    variants = {word}
    if len(word) > 3 and word[0] in HEBREW_PREFIXES:
        variants.add(word[1:])
    return variants


# The words of a meeting (counts by word) that are one of the variants: the word itself or a prefixed form of it.
def _matching_words(counts: Counter, variants: set) -> set:
    found = set()
    for variant in variants:
        if variant in counts:
            found.add(variant)
        for prefix in HEBREW_PREFIXES:
            prefixed = prefix + variant
            if len(prefixed) > 3 and prefixed in counts:
                found.add(prefixed)
    return found


# The first word of a text that is one of the variants, as it is written there.
def _first_word_matching(text: str, variants: set) -> Optional[str]:
    return next((w for w in words_of(text) if word_variants(w) & variants), None)


# About 70 characters around the first place the term appears.
def _snippet(text: str, term: str, width: int = 70) -> str:
    lowered = text.lower()
    i = next((m.start() for m in WORD_RE.finditer(lowered) if m.group() == term), lowered.find(term))
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
        self._word_cache = {}        # meeting id -> (mtimes, entry) for search
        self._word_lock = threading.Lock()   # overlapping searches build and prune that cache one at a time
        # Told the meeting id after every save, brief save and delete, so the search indexer
        # follows the files (set by whoever owns the store; never called under the store's lock).
        self.on_change: Optional[Callable[[str], None]] = None

    # Calls on_change outside the lock; a failing listener never breaks a save.
    def _notify(self, meeting_id: str) -> None:
        listener = self.on_change
        if listener is None:
            return
        try:
            listener(meeting_id)
        except Exception as e:
            logger.warning(f"Meeting change listener failed for {meeting_id}: {e}")

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
        self._notify(meeting['id'])
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
                if path.name.endswith((BRIEF_SUFFIX, CHAT_SUFFIX)):
                    continue
                try:
                    head = header(json.loads(path.read_text(encoding='utf-8')))
                    head['brief'] = (self.folder / f"{head['id']}{BRIEF_SUFFIX}").exists()
                    out.append(head)
                except Exception as e:
                    logger.warning(f"Skipping unreadable meeting {path.name}: {e}")
        return out

    # ── Word search ──────────────────────────────────────────────────────────

    # Everything a meeting says, as parts: title and speakers, summary, screenshot captions, transcript turns.
    @staticmethod
    def _search_parts(meeting: dict, brief: dict) -> dict:
        title = str(meeting.get('title') or '')
        speakers = ' '.join(str(s.get('name') or '') for s in meeting.get('speakers', []))
        return {
            'title': title, 'names': title + ' ' + speakers,
            'summary': _brief_text(brief),
            'captions': ' '.join(str(s.get('caption') or '') for s in meeting.get('screenshots', [])),
            'turns': [(t.get('start', 0), str(t.get('text') or '')) for t in meeting.get('turns', [])],
        }

    # How often each word is said in every meeting, kept per file modification time so a
    # search over many meetings only reads the ones that changed. Newest first.
    def _word_index(self) -> list:
        if not self.folder.exists():
            return []
        with self._word_lock:
            seen, index = set(), []
            for path in sorted(self.folder.glob('*.json'), reverse=True):
                if path.name.endswith((BRIEF_SUFFIX, CHAT_SUFFIX)):
                    continue
                meeting_id = path.name[:-len('.json')]
                try:
                    brief_path = self.brief_path(meeting_id)
                    mtimes = (path.stat().st_mtime_ns, brief_path.stat().st_mtime_ns if brief_path.exists() else None)
                    cached = self._word_cache.get(meeting_id)
                    if not cached or cached[0] != mtimes:
                        meeting = self.load(meeting_id)
                        parts = self._search_parts(meeting, self.load_brief(meeting_id) or {})
                        counts = Counter(words_of(' '.join([parts['names'], parts['summary'], parts['captions']]
                                                           + [text for _, text in parts['turns']])))
                        cached = (mtimes, {'id': meeting_id, 'title': parts['title'],
                                           'started_at': meeting.get('started_at'), 'counts': counts})
                        self._word_cache[meeting_id] = cached
                except Exception as e:      # a file that isn't a readable meeting is skipped, not fatal
                    logger.warning(f"Skipping unreadable meeting {path.name}: {e}")
                    continue
                seen.add(meeting_id)
                index.append(cached[1])
            for gone in set(self._word_cache) - seen:
                self._word_cache.pop(gone, None)
            return index

    # Meetings that hold every query word that is not common (a word in over 30% of meetings, and more than
    # 3 meetings, tells nothing; one in no meeting still has to be there). A query of common words only
    # needs all of them, and so does a search inside one meeting (`meeting_id`), where nothing is common.
    # Score: rare words said often rank first; ties newest first. Each hit says where the rarest word is (title, transcript, summary or a
    # screenshot caption) with a snippet and, for the transcript, the second of the first matching
    # turn so the page can open it there. `meeting_id` limits the hits to one meeting.
    def search(self, query: str, limit: int = 50, meeting_id: Optional[str] = None) -> list:
        query_words = list(dict.fromkeys(words_of(query)))
        index = self._word_index()
        if not query_words or not index:
            return []
        total = len(index)
        every = []       # (query word, its variants, meetings holding it as {id: times said})
        for word in query_words:
            variants = word_variants(word)
            holders = {}
            for entry in index:
                matched = _matching_words(entry['counts'], variants)
                if matched:
                    holders[entry['id']] = sum(entry['counts'][w] for w in matched)
            every.append((word, variants, holders))
        wanted = [item for item in every
                  if len(item[2]) <= COMMON_MIN_MEETINGS or len(item[2]) / total <= COMMON_SHARE]
        if meeting_id is not None or not wanted:
            wanted = every
        scored = []
        for entry in index:                 # newest first, so a stable sort keeps ties newest first
            if meeting_id is not None and entry['id'] != meeting_id:
                continue
            if not all(entry['id'] in holders for _, _, holders in wanted):
                continue
            score = sum(math.log(1 + total / len(holders)) * math.log(1 + holders[entry['id']])
                        for _, _, holders in wanted)
            scored.append((score, entry))
        scored.sort(key=lambda pair: -pair[0])
        rarest_variants = min(wanted, key=lambda item: len(item[2]))[1]
        return [self._hit(entry, score, [variants for _, variants, _ in wanted], rarest_variants)
                for score, entry in scored[:limit]]

    # One result: the title unless the rarest word is somewhere else, then its first place there.
    def _hit(self, entry: dict, score: float, all_variants: list, rarest_variants: set) -> dict:
        hit = {'id': entry['id'], 'title': entry['title'], 'started_at': entry['started_at'], 'score': score,
               'where': 'title', 'snippet': entry['title'], 'at': None}
        try:
            parts = self._search_parts(self.load(entry['id']), self.load_brief(entry['id']) or {})
        except Exception as e:
            logger.warning(f"Search could not read meeting {entry['id']} for its snippet: {e}")
            return hit
        if all(_first_word_matching(parts['names'], variants) for variants in all_variants):
            return hit
        places = [('transcript', at, text) for at, text in parts['turns']] + [
            ('summary', None, parts['summary']), ('screenshot', None, parts['captions'])]
        for where, at, text in places:
            term = _first_word_matching(text, rarest_variants)
            if term:
                hit.update(where=where, snippet=_snippet(text, term), at=at)
                break
        return hit

    # Deleting a meeting deletes its kept audio and its screenshots too.
    def delete(self, meeting_id: str) -> bool:
        deleted = self._delete_files(meeting_id)
        self._notify(meeting_id)
        return deleted

    # The files of one meeting, removed under the lock; True when the meeting file itself existed.
    def _delete_files(self, meeting_id: str) -> bool:
        with self._lock:
            shutil.rmtree(self.audio_dir(meeting_id), ignore_errors=True)
            shutil.rmtree(self.shots_dir(meeting_id), ignore_errors=True)
            self.brief_path(meeting_id).unlink(missing_ok=True)
            self.chat_path(meeting_id).unlink(missing_ok=True)
            try:
                (self.folder / f"{meeting_id}{INDEX_SUFFIX}").unlink(missing_ok=True)
            except OSError as e:        # an indexer, antivirus or Explorer may hold the file (Windows); the meeting still goes
                logger.warning(f"Could not delete the search index of {meeting_id}: {e}")
            try:
                self.path(meeting_id).unlink()
                return True
            except FileNotFoundError:
                return False

    # ── The chat: "<id>.chat.json" ───────────────────────────────────────────
    # [{'role': 'user'|'assistant', 'text', 'at', 'changes'?}], oldest first.

    def chat_path(self, meeting_id: str) -> Path:
        return self.folder / f"{meeting_id}{CHAT_SUFFIX}"

    def load_chat(self, meeting_id: str) -> list:
        try:
            data = json.loads(self.chat_path(meeting_id).read_text(encoding='utf-8'))
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            logger.warning(f"Unreadable chat for {meeting_id}: {e}")
            return []

    def save_chat(self, meeting_id: str, messages: list) -> None:
        path = self.chat_path(meeting_id)
        with self._lock:
            path.write_text(json.dumps(messages, ensure_ascii=False, indent=1), encoding='utf-8')

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
                self.save(meeting)       # tells the listener too, after the brief is on disk
                return path
        self._notify(meeting_id)
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
