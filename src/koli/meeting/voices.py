# meeting/voices.py
# Voices you named, remembered across meetings. Naming "Speaker 2" as "Eran"
# keeps his voiceprint in voices.json next to the meetings folder; in a later
# meeting a voice that matches it is called "Eran" from his first turn.
# Naming him again (another meeting, more audio) refines the same voiceprint.
# Local only, like the meetings themselves.

import datetime
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from ..utils import get_user_app_data_path

logger = logging.getLogger(__name__)

FILE = 'voices.json'
# Cosine a voice must reach to be given a remembered name. Stricter than
# joining a speaker inside a meeting (0.30): a wrong name is worse than
# "Speaker 3". Same voice scored 0.6-0.8, different voices under 0.3.
MATCH = 0.5
_DEFAULT_NAME = re.compile(r'^Speaker \d+$')


def voices_path() -> Path:
    return Path(get_user_app_data_path()) / FILE


# "Speaker 3" is a name Koli gave; anything else is one you chose.
def is_default_name(name: str) -> bool:
    return bool(_DEFAULT_NAME.match(str(name or '')))


def _unit(vec) -> np.ndarray:
    vec = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


class VoiceBook:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else voices_path()
        self._lock = threading.Lock()
        self._voices = None    # [{'name', 'centroid': np.ndarray, 'count', 'updated'}], read on first use

    # ── File ─────────────────────────────────────────────────────────────────

    def _loaded(self) -> list:
        if self._voices is None:
            self._voices = []
            try:
                for v in json.loads(self.path.read_text(encoding='utf-8')).get('voices', []):
                    if v.get('name') and v.get('centroid'):
                        self._voices.append({'name': v['name'], 'centroid': _unit(v['centroid']),
                                             'count': int(v.get('count', 1)), 'updated': v.get('updated', '')})
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning(f"Remembered voices unreadable, starting empty: {e}")
        return self._voices

    def _save(self):
        data = {'voices': [{'name': v['name'], 'centroid': [round(float(x), 5) for x in v['centroid']],
                            'count': v['count'], 'updated': v['updated']} for v in self._voices]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, self.path)

    # ── What the rest of meeting mode calls ──────────────────────────────────

    def names(self) -> list:
        with self._lock:
            return [v['name'] for v in self._loaded()]

    # Keep `centroid` (a speaker's voiceprint, from `count` chunks) under
    # `name`. A name already known is refined, weighted by how much audio
    # each side has; Koli's own "Speaker N" names are never remembered.
    def remember(self, name: str, centroid, count: int = 1):
        name = str(name or '').strip()[:40]
        if not name or is_default_name(name) or centroid is None or not len(centroid):
            return
        count = max(1, int(count or 1))
        now = datetime.datetime.now().isoformat(timespec='seconds')
        with self._lock:
            voices = self._loaded()
            known = next((v for v in voices if v['name'].lower() == name.lower()), None)
            if known is None:
                voices.append({'name': name, 'centroid': _unit(centroid), 'count': count, 'updated': now})
            else:
                known['centroid'] = _unit(known['centroid'] * known['count'] + _unit(centroid) * count)
                known['count'] += count
                known['name'], known['updated'] = name, now
            try:
                self._save()
            except Exception as e:
                logger.warning(f"Could not save the voice of {name}: {e}")
        logger.info(f"Remembered the voice of {name}")

    def forget(self, name: str) -> bool:
        with self._lock:
            voices = self._loaded()
            kept = [v for v in voices if v['name'].lower() != str(name).lower()]
            if len(kept) == len(voices):
                return False
            self._voices = kept
            self._save()
        return True

    # The remembered name for a voiceprint, or None. Names in `taken` are
    # already someone else's in this meeting, so they are skipped.
    def match(self, vec, taken: Iterable[str] = ()) -> Optional[str]:
        if vec is None:
            return None
        vec = _unit(vec)
        taken = {str(t).lower() for t in taken}
        with self._lock:
            best, best_sim = None, MATCH
            for v in self._loaded():
                if v['name'].lower() in taken:
                    continue
                sim = float(np.dot(v['centroid'], vec))
                if sim >= best_sim:
                    best, best_sim = v['name'], sim
        return best
