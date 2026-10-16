# agent/journal.py
# What AI actions did: one line per run in %APPDATA%\koli\agent\history.jsonl
# (what you said, the script, the operations, what each did and how to undo
# it), kept to the last MAX_RUNS. Also tells repeats apart: a run's signature
# is the actions it called with their literal arguments, files left out, so
# "move today's photos to Pictures" on two days is the same plan. The third
# such run in REPEAT_DAYS is offered as a voice command, unless you saved or
# declined it before (agent/state.json).

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

MAX_RUNS = 500
REPEATS_TO_OFFER = 3
REPEAT_DAYS = 14


def signature(trace: List[tuple]) -> str:
    text = json.dumps([list(t) for t in trace], ensure_ascii=False, default=str)
    return hashlib.sha1(text.encode('utf-8')).hexdigest()[:16]


class Journal:
    def __init__(self, folder: Optional[Path] = None):
        if folder is None:
            from ..utils import get_user_app_data_path
            folder = Path(get_user_app_data_path()) / 'agent'
        self.folder = Path(folder)
        self.path = self.folder / 'history.jsonl'
        self.state_path = self.folder / 'state.json'
        self._lock = threading.Lock()

    # ── Runs ──

    def runs(self, limit: int = MAX_RUNS) -> List[dict]:
        try:
            lines = self.path.read_text(encoding='utf-8').splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def add(self, run: dict) -> dict:
        run = {'id': uuid.uuid4().hex[:10], 'at': time.time(), **run}
        with self._lock:
            runs = self.runs() + [run]
            self._write(runs[-MAX_RUNS:])
        return run

    def update(self, run_id: str, **fields) -> Optional[dict]:
        with self._lock:
            runs = self.runs()
            found = None
            for run in runs:
                if run.get('id') == run_id:
                    run.update(fields)
                    found = run
            if found is not None:
                self._write(runs)
            return found

    def get(self, run_id: str) -> Optional[dict]:
        return next((r for r in self.runs() if r.get('id') == run_id), None)

    # The newest run that changed something and wasn't undone.
    def last_undoable(self) -> Optional[dict]:
        for run in reversed(self.runs()):
            if run.get('undone'):
                continue
            if any(r.get('undo') and r.get('status') == 'done' for r in run.get('results') or []):
                return run
        return None

    # A run and every run joined to it by 'follows' (a follow-up that used
    # last(): "make a folder", then "call it Invoices"), newest first. They
    # are one action to Undo, from whichever bubble you press it.
    def chain(self, run_id: str) -> List[dict]:
        runs = {r['id']: r for r in self.runs() if r.get('id')}
        if run_id not in runs:
            return []
        ids, grew = {run_id}, True
        while grew:
            grew = False
            for run in runs.values():
                if run['id'] in ids:
                    continue
                if run.get('follows') in ids or any(runs[i].get('follows') == run['id'] for i in ids):
                    ids.add(run['id'])
                    grew = True
        return sorted((runs[i] for i in ids), key=lambda r: r.get('at', 0), reverse=True)

    def _write(self, runs: List[dict]):
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in runs), encoding='utf-8')
        os.replace(tmp, self.path)

    # ── Repeats ──

    def _state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _set_state(self, data: dict):
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')

    def mark(self, sig: str, how: str):
        with self._lock:
            data = self._state()
            data.setdefault(how, [])
            if sig not in data[how]:
                data[how].append(sig)
            self._set_state(data)

    # The run to offer as a command now, or None: this signature ran
    # REPEATS_TO_OFFER times in REPEAT_DAYS, every time it worked, and it was
    # neither saved nor declined.
    def offer_for(self, sig: str) -> Optional[dict]:
        if not sig:
            return None
        state = self._state()
        if sig in state.get('saved', []) or sig in state.get('declined', []):
            return None
        since = time.time() - REPEAT_DAYS * 86400
        same = [r for r in self.runs() if r.get('signature') == sig and r.get('at', 0) >= since
                and r.get('outcome') == 'done' and not r.get('undone')]
        return same[-1] if len(same) >= REPEATS_TO_OFFER else None
