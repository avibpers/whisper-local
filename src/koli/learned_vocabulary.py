# learned_vocabulary.py
# What practice sessions taught Koli, kept apart from the words you typed
# yourself (My words › Learned). Favored words, corrections and prompt terms,
# each for one language and remembering the microphone and session it came
# from, plus the "how you speak" note per language and the practice intake.
# Stored in %APPDATA%\koli\learned.json, outside user_settings.yaml, so Reset
# all settings keeps it. The engine and post-processing read it on every
# dictation: favored words and prompt terms for the language being
# transcribed, corrections for the dictation's language.
# Design: docs/design/voice-practice.md.

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

FILE = 'learned.json'
KINDS = ('favored', 'correction', 'prompt')


def _empty() -> dict:
    return {'version': 1, 'intake': {}, 'items': [], 'notes': {}, 'sessions': []}


class LearnedVocabulary:
    def __init__(self, path: Optional[Path] = None):
        if path is None:
            from .utils import get_user_app_data_path
            path = Path(get_user_app_data_path()) / FILE
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = None
        self._mtime = None

    # ── File ────────────────────────────────────────────────────────────────

    # The data, re-read when the file changed on disk (another window, a hand
    # edit). Cheap enough for every dictation: a stat call.
    def _load(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if self._data is not None and mtime == self._mtime:
            return self._data
        data = _empty()
        if mtime is not None:
            try:
                loaded = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    data.update({k: loaded[k] for k in data if k in loaded})
            except (OSError, ValueError) as e:
                logger.warning(f"Couldn't read {FILE} ({e}); starting with nothing learned")
        self._data, self._mtime = data, mtime
        return data

    def _save(self, data: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, self.path)
        self._data, self._mtime = data, self.path.stat().st_mtime

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._load()))

    # ── Intake and sessions ─────────────────────────────────────────────────

    def intake(self) -> dict:
        return dict(self.snapshot().get('intake') or {})

    def set_intake(self, intake: dict):
        with self._lock:
            data = self._load()
            data['intake'] = dict(intake or {})
            self._save(data)

    def add_session(self, session: dict):
        with self._lock:
            data = self._load()
            data['sessions'] = [s for s in data['sessions'] if s.get('id') != session.get('id')] + [session]
            self._save(data)

    def set_note(self, language: str, note: str):
        with self._lock:
            data = self._load()
            if note:
                data['notes'][language] = note
            else:
                data['notes'].pop(language, None)
            self._save(data)

    # ── Items ───────────────────────────────────────────────────────────────

    def items(self) -> list:
        return self.snapshot()['items']

    @staticmethod
    def key(item: dict) -> tuple:
        heard = tuple(sorted(str(h).lower() for h in item.get('heard') or []))
        return (item.get('kind'), item.get('language') or '', str(item.get('term', '')).lower(),
                heard if item.get('kind') == 'correction' else (), item.get('profile') or '')

    # Saves findings as learned items; the same item twice is kept once.
    # Returns how many were new.
    def add(self, findings: list, mic: str = '', session: str = '') -> int:
        with self._lock:
            data = self._load()
            known = {self.key(i) for i in data['items']}
            added = 0
            for f in findings:
                if f.get('kind') not in KINDS or not str(f.get('term', '')).strip():
                    continue
                item = {
                    'id': uuid.uuid4().hex[:12], 'kind': f['kind'], 'language': f.get('language') or '',
                    'term': str(f['term']).strip(), 'heard': [str(h).strip() for h in f.get('heard') or [] if str(h).strip()],
                    'profile': f.get('profile') or '', 'mic': mic, 'session': session,
                    'created': time.strftime('%Y-%m-%dT%H:%M:%S'), 'reason': f.get('reason') or '',
                    'source': f.get('source') or 'rules',
                }
                if self.key(item) in known:
                    continue
                known.add(self.key(item))
                data['items'].append(item)
                added += 1
            self._save(data)
            return added

    def delete(self, ids: list) -> int:
        wanted = set(ids or [])
        with self._lock:
            data = self._load()
            before = len(data['items'])
            data['items'] = [i for i in data['items'] if i.get('id') not in wanted]
            self._save(data)
            return before - len(data['items'])

    # Start fresh: every learned item, the notes and the sessions. The intake
    # (your answers) stays. The caller removes the recordings folder.
    def delete_all(self):
        with self._lock:
            data = self._load()
            data.update(items=[], notes={}, sessions=[])
            self._save(data)

    # ── What dictation reads ────────────────────────────────────────────────

    @staticmethod
    def _for_language(items, language):
        return [i for i in items if not language or not i.get('language') or i['language'] == language]

    # Favored words and prompt terms for the language about to be transcribed
    # (every language when it isn't known yet) and the active profile.
    def extras(self, language: Optional[str] = None, profile: Optional[str] = None) -> dict:
        items = self._for_language(self.items(), language)
        favored = [i['term'] for i in items if i['kind'] == 'favored']
        prompt = [i['term'] for i in items if i['kind'] == 'prompt'
                  and (not i.get('profile') or i.get('profile') == profile)]
        return {'hotwords': list(dict.fromkeys(favored)), 'prompt_terms': list(dict.fromkeys(prompt))}

    # Your corrections with the learned ones for this language merged in
    # (canonical → mishearings). Yours are never changed.
    def merged_corrections(self, corrections, language: Optional[str]) -> dict:
        merged = {k: list(v) if isinstance(v, (list, tuple)) else [v]
                  for k, v in (corrections or {}).items()} if isinstance(corrections, dict) else {}
        for item in self._for_language(self.items(), language):
            if item['kind'] != 'correction' or not item.get('heard'):
                continue
            variants = merged.setdefault(item['term'], [])
            for heard in item['heard']:
                if heard not in variants:
                    variants.append(heard)
        return merged


_instance = None
_instance_lock = threading.Lock()


def get() -> LearnedVocabulary:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = LearnedVocabulary()
        return _instance
