# meeting/detector.py
# Which call is on right now. A process with an open audio session (Discord
# keeps one for the whole call; a notification ping doesn't) that matches an
# app rule with a `meeting` setting is a source; a browser counts through its
# tabs, whose names carry the tab title and media state ("Meet - abc-defg-hij
# - Microphone in use"). The pure part is find_sources(); the thread just
# polls the platform probes once a second.

import logging
import re
import threading
from typing import Callable, Optional

from ..app_rules import rule_matches

logger = logging.getLogger(__name__)

BROWSERS = ('chrome.exe', 'msedge.exe', 'brave.exe')
MEET_CODE = re.compile(r'\b([a-z]{3}-[a-z]{4}-[a-z]{3})\b')
MODES = ('auto', 'ask', 'off')
SESSION_ACTIVE = 1
# Chrome and Edge append these to a tab's accessible name (tab_utils.cc).
MEDIA_STATES = ('Audio playing', 'Audio muted', 'Microphone in use', 'Camera in use',
                'Camera and microphone in use', 'Sharing screen', 'Playing in picture-in-picture',
                'Media recording', 'Connected to a device')


# ── Pure ─────────────────────────────────────────────────────────────────────

def strip_media(name: str) -> tuple:
    name = name or ''
    for state in MEDIA_STATES:
        suffix = f' - {state}'
        if name.endswith(suffix):
            return name[:-len(suffix)], state
    return name, ''


def _mode_of(rule: dict, detect_default: str) -> str:
    mode = rule.get('meeting')
    return mode if mode in MODES else detect_default


def _meeting_rules(rules: list) -> list:
    return [r for r in rules if r.get('meeting') and not r.get('disabled')]


# The sources on right now, in a stable order. `sessions` come from
# audio_sessions.active(), `tabs` from browser_tabs.tabs(), `rules` from the
# app rules (first match wins, as for delivery).
def find_sources(sessions: list, tabs: list, rules: list, detect_default: str) -> list:
    detect_default = detect_default if detect_default in MODES else 'auto'
    meeting_rules = _meeting_rules(rules)
    active = []
    for s in sessions:
        exe = (s.get('exe') or '').lower()
        if exe and int(s.get('state', 0)) == SESSION_ACTIVE and exe not in active:
            active.append(exe)
    sources = []
    for exe in active:
        if exe in BROWSERS:
            for tab in tabs:
                if (tab.get('exe') or '').lower() != exe:
                    continue
                title, _ = strip_media(tab.get('title') or '')
                rule = next((r for r in meeting_rules if rule_matches(r, exe, title)), None)
                if rule is None:
                    continue
                mode = _mode_of(rule, detect_default)
                if mode == 'off':
                    continue
                found = MEET_CODE.search(title.lower())
                sources.append({'key': f'{exe}|{title}', 'app': exe, 'kind': 'browser', 'title': title,
                                'code': found.group(1) if found else '', 'mode': mode})
            continue
        rule = next((r for r in meeting_rules if rule_matches(r, exe, '')), None)
        if rule is None:
            continue
        mode = _mode_of(rule, detect_default)
        if mode == 'off':
            continue
        sources.append({'key': exe, 'app': exe, 'kind': 'native', 'title': '', 'code': '', 'mode': mode})
    return sources


# ── Thread ───────────────────────────────────────────────────────────────────

class MeetingDetector:
    def __init__(self, on_sources: Callable[[list], None], rules_provider: Callable[[], list],
                 mode_provider: Callable[[], str], interval: float = 1.0,
                 sessions_fn: Optional[Callable[[], list]] = None, tabs_fn: Optional[Callable[[], list]] = None):
        self.on_sources = on_sources
        self.rules_provider = rules_provider
        self.mode_provider = mode_provider
        self.interval = interval
        self._sessions_fn = sessions_fn
        self._tabs_fn = tabs_fn
        self._stop = threading.Event()
        self._thread = None
        self._last_keys = None
        self._probe_failed = False

    def _sessions(self) -> list:
        if self._sessions_fn is not None:
            return self._sessions_fn()
        from ..platform import audio_sessions
        return audio_sessions.active()

    def _tabs(self, needed: bool) -> list:
        if not needed:
            return []
        if self._tabs_fn is not None:
            return self._tabs_fn()
        from ..platform import browser_tabs
        return browser_tabs.tabs()

    # One poll. A probe that fails counts as "nothing on", logged once until
    # it works again. The session manager hears the result every time.
    def tick(self):
        try:
            sessions = self._sessions()
            browser_on = any((s.get('exe') or '').lower() in BROWSERS and int(s.get('state', 0)) == SESSION_ACTIVE
                             for s in sessions)
            sources = find_sources(sessions, self._tabs(browser_on), self.rules_provider(), self.mode_provider())
            self._probe_failed = False
        except Exception as e:
            if not self._probe_failed:
                logger.warning(f"Meeting detection probe failed: {e}")
                self._probe_failed = True
            sources = []
        keys = [s['key'] for s in sources]
        if keys != self._last_keys:
            logger.info(f"Meeting sources: {keys or 'none'}")
            self._last_keys = keys
        self.on_sources(sources)

    def _run(self):
        while not self._stop.wait(self.interval):
            self.tick()

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name='meeting-detector')
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread = None
