# meeting/voices.py
# Every voice Koli has heard on a call, kept across meetings in voices.json
# beside the meetings folder. Each voice has a permanent id ("v-3f9a2c1b7e04")
# that meeting files point to, so one person can be followed through every
# meeting (search, analysis, an LLM's notes) whether or not they have a name.
# A meeting that ends refines the voices it heard and adds the new ones; a
# name, when you give one, is just a label on the id. Local only.

import datetime
import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from ..utils import get_user_app_data_path

logger = logging.getLogger(__name__)

FILE = 'voices.json'
# Cosine for a speaker to be a known voice. A firm voiceprint (built from a
# few chunks) is trusted from MATCH; a speaker's first chunk is a noisy
# sample, so it needs FIRST_MATCH. On a real Meet the three people's
# voiceprints were 0.12-0.30 apart and one person split in two was 0.86.
MATCH = 0.5
FIRST_MATCH = 0.65
_DEFAULT_NAME = re.compile(r'^Speaker \d+$')


def voices_path() -> Path:
    return Path(get_user_app_data_path()) / FILE


# "Speaker 3" is a name Koli gave; anything else is one you chose.
def is_default_name(name: str) -> bool:
    return bool(_DEFAULT_NAME.match(str(name or '')))


def new_voice_id() -> str:
    return 'v-' + uuid.uuid4().hex[:12]


def _unit(vec) -> np.ndarray:
    vec = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


class VoiceBook:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else voices_path()
        self._lock = threading.Lock()
        self._voices = None    # [{'id', 'name', 'centroid': ndarray, 'count', 'first_seen', 'last_seen', 'meetings'}]

    # ── File ─────────────────────────────────────────────────────────────────

    def _loaded(self) -> list:
        if self._voices is None:
            self._voices = []
            try:
                for v in json.loads(self.path.read_text(encoding='utf-8')).get('voices', []):
                    if not v.get('centroid'):
                        continue
                    self._voices.append({
                        'id': v.get('id') or new_voice_id(), 'name': v.get('name', ''),
                        'centroid': _unit(v['centroid']), 'count': int(v.get('count', 1)),
                        'first_seen': v.get('first_seen', ''), 'last_seen': v.get('last_seen', ''),
                        'meetings': list(v.get('meetings', []))})
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning(f"Saved voices unreadable, starting empty: {e}")
        return self._voices

    def _save(self):
        data = {'voices': [{**{k: v[k] for k in ('id', 'name', 'count', 'first_seen', 'last_seen', 'meetings')},
                            'centroid': [round(float(x), 5) for x in v['centroid']]} for v in self._voices]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, self.path)

    def _find(self, voice_id: str) -> Optional[dict]:
        return next((v for v in self._loaded() if v['id'] == voice_id), None)

    @staticmethod
    def _public(v: dict) -> dict:
        return {k: v[k] for k in ('id', 'name', 'count', 'first_seen', 'last_seen', 'meetings')}

    # ── What the rest of meeting mode calls ──────────────────────────────────

    # Every saved voice, newest first, without voiceprints.
    def all(self) -> list:
        with self._lock:
            return [self._public(v) for v in sorted(self._loaded(), key=lambda v: v['last_seen'], reverse=True)]

    def get(self, voice_id: str) -> Optional[dict]:
        with self._lock:
            v = self._find(voice_id)
            return self._public(v) if v else None

    # The saved voice a voiceprint belongs to, as {'id', 'name'}, or None.
    # `taken` are voice ids already given to other speakers in this meeting;
    # `first` asks for the stricter bar of a single chunk.
    def match(self, vec, taken: Iterable[str] = (), first: bool = False) -> Optional[dict]:
        if vec is None:
            return None
        vec = _unit(vec)
        taken = set(taken)
        with self._lock:
            best, best_sim = None, FIRST_MATCH if first else MATCH
            for v in self._loaded():
                if v['id'] in taken:
                    continue
                sim = float(np.dot(v['centroid'], vec))
                if sim >= best_sim:
                    best, best_sim = v, sim
            return {'id': best['id'], 'name': best['name']} if best else None

    # Keep a speaker's voiceprint (`centroid` from `count` chunks) heard in
    # `meeting_id`: refines `voice_id`, or the saved voice it matches, or
    # starts a new one. Weighted by how much audio each side has. Returns
    # the voice id.
    def keep(self, centroid, count: int, meeting_id: str, voice_id: Optional[str] = None,
             taken: Iterable[str] = ()) -> str:
        count = max(1, int(count or 1))
        if voice_id is None:
            found = self.match(centroid, taken=taken)
            voice_id = found['id'] if found else None
        now = _now()
        with self._lock:
            v = self._find(voice_id) if voice_id else None
            if v is None:
                v = {'id': voice_id or new_voice_id(), 'name': '', 'centroid': _unit(centroid), 'count': count,
                     'first_seen': now, 'last_seen': now, 'meetings': []}
                self._voices.append(v)
            elif meeting_id and meeting_id in v['meetings']:
                v['last_seen'] = now     # a resumed meeting ending again: its audio already counts
            else:
                v['centroid'] = _unit(v['centroid'] * v['count'] + _unit(centroid) * count)
                v['count'] += count
                v['last_seen'] = now
            if meeting_id and meeting_id not in v['meetings']:
                v['meetings'].append(meeting_id)
            self._save()
            return v['id']

    def set_name(self, voice_id: str, name: str):
        name = str(name or '').strip()[:40]
        with self._lock:
            v = self._find(voice_id)
            if v is None:
                return
            v['name'] = '' if is_default_name(name) else name
            self._save()

    # Two saved voices that are one person: `from_id` joins `into_id`.
    def merge(self, from_id: str, into_id: str):
        with self._lock:
            source, target = self._find(from_id), self._find(into_id)
            if source is None or target is None or source is target:
                return
            target['centroid'] = _unit(target['centroid'] * target['count'] + source['centroid'] * source['count'])
            target['count'] += source['count']
            target['meetings'] += [m for m in source['meetings'] if m not in target['meetings']]
            target['first_seen'] = min(target['first_seen'], source['first_seen']) or target['first_seen']
            target['name'] = target['name'] or source['name']
            self._voices.remove(source)
            self._save()

    def forget(self, voice_id: str) -> bool:
        with self._lock:
            voices = self._loaded()
            kept = [v for v in voices if v['id'] != voice_id]
            if len(kept) == len(voices):
                return False
            self._voices = kept
            self._save()
        return True
