# meeting/index.py
# The meaning index: each meeting's pieces embedded into "<id>.index.npz" beside it,
# and a search that holds every meeting's vectors in memory and ranks them by cosine.
import hashlib
import json
import logging
import os
import threading
from collections import Counter
from pathlib import Path
from typing import Callable

import numpy as np

from .pieces import embed_text, pieces
from .store import INDEX_SUFFIX

logger = logging.getLogger(__name__)

SEARCH_TOP = 120    # nearest pieces a search returns before callers cap them per meeting

# What the embedding model is told about the question so it looks for passages that answer it.
QUERY_INSTRUCTION = 'Given a question about past meetings, retrieve the passages that answer it'


# The question as the embedding model wants it: instruction line, then the question.
def query_text(question: str) -> str:
    return f'Instruct: {QUERY_INSTRUCTION}\nQuery: {question}'


# Keeps hits in rank order: at most `per_meeting` from one meeting, and `total` overall.
def cap(hits: list, per_meeting: int, total: int) -> list:
    taken = Counter()
    out = []
    for hit in hits:
        if len(out) >= total:
            break
        if taken[hit['id']] >= per_meeting:
            continue
        taken[hit['id']] += 1
        out.append(hit)
    return out


# The key a piece is recognised by when its meeting changes: the text the model sees.
def _hash(text: str) -> str:
    return hashlib.sha1(text.encode('utf-8')).hexdigest()


class MeetingIndex:
    # `embed` turns a list of texts into unit-length float32 rows (llm.embed).
    def __init__(self, folder: Path, model: str, embed: Callable[[list], np.ndarray]):
        self.folder = Path(folder)
        self.model = model
        self.embed = embed
        # {meeting_id: entry dict (mtime, vectors float32, rows, model, hashes)}, refreshed per file by mtime.
        self._cache: dict = {}
        self._lock = threading.RLock()

    def query_text(self, question: str) -> str:
        return query_text(question)

    def _path(self, meeting_id: str) -> Path:
        return self.folder / f'{meeting_id}{INDEX_SUFFIX}'

    # ── Writing ──────────────────────────────────────────────────────────────

    # Embeds only the pieces whose text is new (all of them after a retitle or a model change)
    # and rewrites the meeting's file. Returns how many pieces were embedded now.
    def update(self, meeting: dict, brief: dict | None) -> int:
        meeting_id = meeting['id']
        cut = pieces(meeting, brief)
        texts = [embed_text(meeting, piece) for piece in cut]
        hashes = [_hash(text) for text in texts]
        with self._lock:
            known = self._known_vectors(meeting_id)
            missing = {}                              # hash -> text, once each
            for h, text in zip(hashes, texts):
                if h not in known:
                    missing.setdefault(h, text)
            if missing:
                fresh = np.asarray(self.embed(list(missing.values())), dtype=np.float32)
                known.update(zip(missing.keys(), fresh))
            vectors = (np.stack([known[h] for h in hashes]) if hashes
                       else np.zeros((0, 0), dtype=np.float32))
            rows = [{**piece, 'id': meeting_id, 'title': meeting.get('title') or '',
                     'started_at': meeting.get('started_at') or ''} for piece in cut]
            self._write(meeting_id, vectors, hashes, rows)
            return len(missing)

    # The vectors already on disk for this meeting by piece hash; empty for another model.
    def _known_vectors(self, meeting_id: str) -> dict:
        entry = self._load(meeting_id)
        if entry is None or entry['model'] != self.model:
            return {}
        return dict(zip(entry['hashes'], entry['vectors']))

    # Temp file first, then a rename, so a crash never leaves half an index.
    def _write(self, meeting_id: str, vectors: np.ndarray, hashes: list, rows: list) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self._path(meeting_id)
        temp = target.with_name(f'{meeting_id}.index.tmp')
        try:
            with open(temp, 'wb') as f:                # a file object: np.savez adds no suffix
                np.savez(f, vectors=vectors.astype(np.float16), hashes=np.array(hashes, dtype=str),
                         rows=np.array(json.dumps(rows, ensure_ascii=False)), model=np.array(self.model))
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        self._cache.pop(meeting_id, None)

    def remove(self, meeting_id: str) -> None:
        with self._lock:
            self._path(meeting_id).unlink(missing_ok=True)
            self._cache.pop(meeting_id, None)

    # ── Reading ──────────────────────────────────────────────────────────────

    # True when the file exists for this model and was written after `mtime`.
    def is_current(self, meeting_id: str, mtime: float) -> bool:
        with self._lock:
            entry = self._load(meeting_id)
            return entry is not None and entry['model'] == self.model and entry['mtime'] > mtime

    # One meeting's index from the cache, read again when the file changed; None if missing or unreadable.
    def _load(self, meeting_id: str) -> dict | None:
        path = self._path(meeting_id)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            self._cache.pop(meeting_id, None)
            return None
        cached = self._cache.get(meeting_id)
        if cached and cached['mtime'] == mtime:
            return cached
        try:
            with np.load(path, allow_pickle=False) as data:
                entry = {'mtime': mtime, 'vectors': data['vectors'].astype(np.float32),
                         'rows': json.loads(str(data['rows'])), 'model': str(data['model']),
                         'hashes': [str(h) for h in data['hashes']]}
        except Exception as e:                         # a damaged file is rebuilt by the next update
            logger.warning(f'Unreadable meeting index {path.name}: {e}')
            self._cache.pop(meeting_id, None)
            return None
        self._cache[meeting_id] = entry
        return entry

    # The best `top` pieces across all meetings (or one) for this model, best first.
    def search(self, query_vector: np.ndarray, meeting_id: str | None = None, top: int = SEARCH_TOP) -> list[dict]:
        query = np.asarray(query_vector, dtype=np.float32).ravel()
        with self._lock:
            entries = [self._load(mid) for mid in self._meeting_ids(meeting_id)]
            # Other models' vectors never mix in; neither do vectors of another size.
            entries = [e for e in entries if e and e['model'] == self.model
                       and len(e['rows']) and e['vectors'].shape[1] == query.shape[0]]
            if not entries:
                return []
            scores = np.concatenate([e['vectors'] @ query for e in entries])
            rows = [row for e in entries for row in e['rows']]
        best = np.argsort(-scores, kind='stable')[:top]
        return [{**rows[i], 'score': float(scores[i])} for i in best]

    # Ids of the meetings that have an index file now (a cheap folder scan on every search).
    def _meeting_ids(self, meeting_id: str | None) -> list:
        if meeting_id is not None:
            return [meeting_id]
        if not self.folder.exists():
            return []
        ids = [p.name[:-len(INDEX_SUFFIX)] for p in self.folder.glob(f'*{INDEX_SUFFIX}')]
        for gone in set(self._cache) - set(ids):      # forget meetings whose file was deleted
            self._cache.pop(gone, None)
        return sorted(ids)
