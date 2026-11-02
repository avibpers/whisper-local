# agent/app_memory.py
# What Koli learned doing things in other apps: for each request done step by
# step, the app, what you said and the steps that worked ("click link “Daily
# Mix 6”, then button “Play Daily Mix 6”"), or where it got stuck. The next
# request in that app starts with the closest of these in front of the AI, so
# it follows the path instead of finding it again; the very same request is
# replayed by name without asking the AI at all (agent/app_steps.py). Steps
# are kept by label, never by the program's refs, which die with the screen.

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

MAX_ENTRIES = 200            # oldest dropped first
SIMILAR = 3                  # past entries the AI is shown
MIN_LIKENESS = 0.25          # word overlap a past request needs to be shown


def _words(text: str) -> set:
    return {w for w in re.sub(r'[^\w]+', ' ', (text or '').lower()).split() if len(w) >= 2}


# The request as a key: lower case, words only ("Play Daily Mix 6 on Spotify."
# and "play daily mix 6 on spotify" are the same request).
def normalize(text: str) -> str:
    return ' '.join(re.sub(r'[^\w]+', ' ', (text or '').lower()).split())


class AppMemory:
    def __init__(self, folder: Optional[Path] = None):
        if folder is None:
            from ..utils import get_user_app_data_path
            folder = Path(get_user_app_data_path()) / 'agent'
        self.path = Path(folder) / 'app_memory.json'
        self._lock = threading.Lock()

    # ── Reading ──

    def entries(self) -> List[dict]:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    # The past requests in this app most like this one, best first: what the
    # AI is told worked (or got stuck) before.
    def similar(self, app: str, sentence: str, limit: int = SIMILAR) -> List[dict]:
        wanted = _words(sentence)
        scored = []
        for entry in self.entries():
            if entry.get('app', '').lower() != app.lower():
                continue
            seen = _words(entry.get('said', ''))
            likeness = len(wanted & seen) / max(len(wanted | seen), 1)
            if likeness >= MIN_LIKENESS:
                scored.append((likeness, entry.get('at', 0), entry))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [entry for _, _, entry in scored[:limit]]

    # The steps that last did exactly this request in this app, or None.
    def recipe(self, app: str, sentence: str) -> Optional[dict]:
        key = normalize(sentence)
        for entry in reversed(self.entries()):
            if entry.get('app', '').lower() == app.lower() and normalize(entry.get('said', '')) == key:
                return entry if entry.get('worked') and entry.get('steps') else None
        return None

    # ── Writing ──

    # One finished request: `steps` as {'action', 'name', 'role', 'text'?,
    # 'option'?, 'risk'?}; `worked` when it ended done; `stuck` says why not.
    def add(self, app: str, sentence: str, steps: List[dict], worked: bool, stuck: str = '', answer: str = ''):
        entry = {'app': app, 'said': sentence, 'steps': steps, 'worked': worked, 'at': time.time()}
        if stuck:
            entry['stuck'] = stuck
        if answer:
            entry['answer'] = answer[:200]
        with self._lock:
            entries = self.entries()
            key = normalize(sentence)
            # The newest outcome of the same request replaces the old one.
            entries = [e for e in entries if not (e.get('app', '').lower() == app.lower()
                                                  and normalize(e.get('said', '')) == key)]
            entries.append(entry)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix('.tmp')
                tmp.write_text(json.dumps(entries[-MAX_ENTRIES:], ensure_ascii=False, indent=1), encoding='utf-8')
                tmp.replace(self.path)
            except OSError as e:
                logger.warning(f"App memory not saved: {e}")


# How a past entry reads to the AI: "play daily mix 6 on spotify → click link
# “Daily Mix 6”; click button “Play Daily Mix 6” (worked)".
def describe(entry: dict) -> str:
    steps = '; '.join(step_line(s) for s in entry.get('steps') or []) or 'no steps'
    outcome = 'worked' if entry.get('worked') else f"got stuck: {entry.get('stuck') or 'it did not work'}"
    return f"{entry.get('said', '')} → {steps} ({outcome})"


def step_line(step: dict) -> str:
    action, target = step.get('action'), f"{step.get('role') or 'control'} “{step.get('name', '')}”"
    if action == 'set_text':
        return f"write “{step.get('text', '')}” in {target}"
    if action == 'select':
        return f"choose “{step.get('option', '')}” in {target}"
    if action in ('check', 'uncheck'):
        return f"turn {'on' if action == 'check' else 'off'} {target}"
    if action == 'toggle':
        return f"switch {target}"
    if action == 'close_app':
        return 'close the app'
    return f"click {target}"
