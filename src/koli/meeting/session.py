# meeting/session.py
# When a meeting starts, pauses, resumes and ends. Pure logic: audio, windows
# and files are reached only through the callbacks handed in, so every rule in
# docs/design/meeting-mode.md is tested without hardware. One meeting at a time.

import datetime
import logging
import threading
from typing import Callable, Optional

from .store import MeetingStore, ThrottledSaver, duration_s, new_id, new_meeting, started_ts

logger = logging.getLogger(__name__)

STATES = ('idle', 'offered', 'live', 'paused', 'ended')
NOTICE_SECONDS = 6.0

# Friendly names for the apps the shipped rules know.
APP_NAMES = {'discord.exe': 'Discord', 'zoom.exe': 'Zoom', 'ms-teams.exe': 'Teams', 'teams.exe': 'Teams',
             'slack.exe': 'Slack', 'chrome.exe': 'Chrome', 'msedge.exe': 'Edge', 'brave.exe': 'Brave'}
MANUAL_SOURCE = {'key': 'manual', 'app': '', 'kind': 'manual', 'title': '', 'code': ''}


def _default_schedule(seconds: float, fn: Callable) -> Callable[[], None]:
    timer = threading.Timer(seconds, fn)
    timer.daemon = True
    timer.start()
    return timer.cancel


def _now() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


# "Meet - abc-defg-hij" reads as Google Meet; a native app by its name.
def title_for(source: dict) -> str:
    if source.get('kind') == 'manual':
        return 'Meeting'
    title = (source.get('title') or '').strip()
    if title.startswith('Meet - ') or title.startswith('Meet – '):
        return 'Google Meet'
    if title:
        return title[:60]
    app = source.get('app', '')
    return APP_NAMES.get(app, app.replace('.exe', '').title() or 'Meeting')


class SessionManager:
    def __init__(self, store: MeetingStore, *, start_capture: Callable[[dict], None], stop_capture: Callable[[], None],
                 publish: Callable[[str, dict], None], settings: Callable, now: Callable = None,
                 schedule: Callable = None, save_interval: float = 5.0):
        self.store = store
        self.saver = ThrottledSaver(store, save_interval)
        self._start_capture = start_capture
        self._stop_capture = stop_capture
        self._publish = publish
        self._settings = settings
        self._now = now or _now
        self._schedule = schedule or _default_schedule
        self._lock = threading.RLock()
        self.state = 'idle'
        self.meeting: Optional[dict] = None
        self.excluded = False
        self.paused_by_user = False
        self.behind_s = 0.0
        self.notice = None
        self._cancel_notice = None
        self._sources = []               # what the detector saw last
        self._pending = None             # the source waiting for speech, or offered
        self._speech_s = 0.0             # speech heard since the pending source appeared
        self._declined = set()           # source keys answered "Not now"
        self._paused_at = None           # when the current pause began
        self._gap_open = None            # index into meeting['gaps'] of the open gap
        self._cancel_gap_timer = None
        self._previous = None            # the meeting ended while a new one started inside its gap
        self._previous_until = None
        self._previous_paused_s = 0.0
        self._resumed_turn_index = None  # turns from here came after the last resume

    # ── Queries ──────────────────────────────────────────────────────────────

    def current(self) -> Optional[dict]:
        return self.meeting

    def _elapsed(self) -> float:
        if not self.meeting:
            return 0.0
        return max(0.0, self._now().timestamp() - started_ts(self.meeting))

    def state_payload(self) -> dict:
        with self._lock:
            m = self.meeting
            return {
                'state': self.state,
                'id': m['id'] if m else None,
                'title': m['title'] if m else '',
                'source': m['source'] if m else (self._pending or {}),
                'started_at': m['started_at'] if m else None,
                'elapsed_s': round(self._elapsed(), 1),
                'paused_by_user': self.paused_by_user,
                'excluded': self.excluded,
                'behind_s': round(self.behind_s, 1),
                'speakers': [{'id': s['id'], 'name': s['name'], 'colour': s['colour']}
                             for s in (m['speakers'] if m else [])],
                'notice': self.notice,
                'turns': len(m['turns']) if m else 0,
                'can_continue': self._previous is not None and self.state == 'live',
                'can_split': self._resumed_turn_index is not None and self.state == 'live',
                'offer': dict(self._pending) if self.state == 'offered' and self._pending else None,
            }

    def _emit(self):
        self._publish('meeting', self.state_payload())

    def _set_notice(self, notice: Optional[str]):
        if self._cancel_notice:
            self._cancel_notice()
            self._cancel_notice = None
        self.notice = notice
        if notice:
            self._cancel_notice = self._schedule(NOTICE_SECONDS, self._clear_notice)

    def _clear_notice(self):
        with self._lock:
            self.notice = None
            self._cancel_notice = None
            self._emit()

    # ── Detection ────────────────────────────────────────────────────────────

    # The detector's view, once a second. Only changes matter.
    def on_sources(self, sources: list):
        with self._lock:
            keys = {s['key'] for s in sources}
            self._declined &= keys
            self._sources = list(sources)
            if self.state in ('live', 'paused'):
                self._sources_while_meeting(sources)
                return
            if self.state == 'offered':
                if self._pending['key'] not in keys:
                    self._pending = None
                    self.state = 'idle'
                    self._emit()
                return
            # Idle: pick a source to wait for speech on. The same source
            # staying keeps the speech heard so far.
            candidates = [s for s in sources if s['key'] not in self._declined and s.get('mode') != 'off']
            if self._pending and any(c['key'] == self._pending['key'] for c in candidates):
                return
            self._pending = candidates[0] if candidates else None
            self._speech_s = 0.0

    def _sources_while_meeting(self, sources: list):
        m = self.meeting
        match = next((s for s in sources if self._same_meeting(m, s)), None)
        if self.state == 'live' and match is None and m['source'].get('kind') != 'manual':
            self._pause(by_user=False)
            return
        if self.state == 'paused' and not self.paused_by_user:
            if match is not None:
                self._resume(notice='resumed')
                return
            other = next((s for s in sources if s.get('mode') != 'off'), None)
            if other is not None:
                # A different call: this one ends, the new one waits for speech.
                self._end()
                self._pending = other
                self._speech_s = 0.0

    @staticmethod
    def _same_meeting(meeting: dict, source: dict) -> bool:
        src = meeting['source']
        if src.get('kind') == 'manual':
            return True
        if src.get('code') or source.get('code'):
            return src.get('code') == source.get('code')
        return src.get('app') == source.get('app') and src.get('title') == source.get('title')

    # Speech heard on the loopback: a pending source starts once enough of it.
    def on_speech(self, seconds: float):
        with self._lock:
            if self.state != 'idle' or not self._pending:
                return
            self._speech_s += float(seconds)
            if self._speech_s < float(self._settings('meeting.start_speech_seconds', 3) or 3):
                return
            source = self._pending
            if source.get('mode') == 'ask':
                self.state = 'offered'
                self._emit()
                return
            self._begin(source, notice=None)

    def offer_accept(self):
        with self._lock:
            if self.state != 'offered' or not self._pending:
                return
            self._begin(self._pending, notice=None)

    def offer_decline(self):
        with self._lock:
            if self.state != 'offered' or not self._pending:
                return
            self._declined.add(self._pending['key'])
            self._pending = None
            self.state = 'idle'
            self._emit()

    # ── Commands ─────────────────────────────────────────────────────────────

    def start(self, source: Optional[dict] = None):
        with self._lock:
            if self.state in ('live', 'paused'):
                return
            source = source or self._pending or (self._sources[0] if self._sources else None) or MANUAL_SOURCE
            self._begin(source, notice=None)

    def end(self):
        with self._lock:
            if self.state == 'offered':
                self.offer_decline()
            elif self.state in ('live', 'paused'):
                self._end()

    def new(self):
        with self._lock:
            if self.state not in ('live', 'paused'):
                return
            source = dict(self.meeting['source'])
            self._end(remember=False)
            self._begin(source, notice=None)

    def pause(self):
        with self._lock:
            if self.state == 'live':
                self._pause(by_user=True)

    def resume(self):
        with self._lock:
            if self.state == 'paused':
                self._resume(notice=None)

    def set_excluded(self, on: bool):
        with self._lock:
            on = bool(on)
            if on == self.excluded:
                return
            self.excluded = on
            if self.meeting and self.state == 'live':
                if on:
                    self._open_gap('excluded')
                else:
                    self._close_gap()
            self._emit()

    def set_behind(self, seconds: float):
        with self._lock:
            self.behind_s = float(seconds)

    # ── Turns and speakers ───────────────────────────────────────────────────

    # A turn from the transcriber, with wall-clock t0/t1 (time.time()).
    def add_turn(self, turn: dict):
        with self._lock:
            m = self.meeting
            if not m or self.state not in ('live', 'paused'):
                return
            base = started_ts(m)
            stored = {'start': round(float(turn['t0']) - base, 2), 'end': round(float(turn['t1']) - base, 2),
                      'stream': turn['stream'], 'speaker': turn['speaker'], 'text': turn['text'],
                      'lang': turn.get('lang', '')}
            m['turns'].append(stored)
            self.saver.mark(m)
            self._publish('meeting_turn', {'id': m['id'], 'turn': stored})

    # A speaker seen for the first time, or one whose voiceprint moved. The
    # name is the user's to change, so an update never touches it.
    def ensure_speaker(self, speaker: dict):
        with self._lock:
            m = self.meeting
            if not m:
                return
            for i, s in enumerate(m['speakers']):
                if s['id'] == speaker['id']:
                    m['speakers'][i] = {**s, **{k: v for k, v in speaker.items() if k != 'name'}}
                    self.saver.mark(m)
                    return
            m['speakers'].append(dict(speaker))
            self.saver.mark(m)
            self._emit()

    def rename_speaker(self, speaker_id: str, name: str):
        with self._lock:
            m = self.meeting
            if not m:
                return
            for s in m['speakers']:
                if s['id'] == speaker_id:
                    s['name'] = str(name).strip()[:40] or s['name']
            self.saver.mark(m)
            self._emit()

    # ── Resume or new, by hand ───────────────────────────────────────────────

    # "Continue previous instead": the turns of this new meeting move onto the
    # previous one, which comes back live; the new file goes away.
    def continue_previous(self):
        with self._lock:
            if self.state != 'live' or not self._previous:
                return
            new, prev = self.meeting, self._previous
            offset = started_ts(new) - started_ts(prev)
            for t in new['turns']:
                prev['turns'].append({**t, 'start': round(t['start'] + offset, 2), 'end': round(t['end'] + offset, 2)})
            for s in new['speakers']:
                if not any(p['id'] == s['id'] for p in prev['speakers']):
                    prev['speakers'].append(s)
            prev['ended_at'] = None
            prev['gaps'].append({'start': round(self._previous_paused_s, 2), 'end': round(offset, 2), 'reason': 'paused'})
            self.store.delete(new['id'])
            self.meeting = prev
            self._previous = None
            self._resumed_turn_index = len(prev['turns']) - len(new['turns'])
            self.saver.mark(prev)
            self._set_notice('resumed')
            self._publish('meeting_turn', {'id': prev['id'], 'reload': True})
            self._emit()

    # "Start new instead" after a resume: what came after the resume becomes
    # a meeting of its own; the previous one ends where it paused.
    def split_resumed(self):
        with self._lock:
            if self.state != 'live' or self._resumed_turn_index is None:
                return
            old = self.meeting
            cut = self._resumed_turn_index
            moved, old['turns'] = old['turns'][cut:], old['turns'][:cut]
            gap = old['gaps'].pop() if old['gaps'] and old['gaps'][-1]['reason'] == 'paused' else None
            resumed_s = gap['end'] if gap else (moved[0]['start'] if moved else self._elapsed())
            paused_s = gap['start'] if gap else resumed_s
            base = started_ts(old)
            start_dt = datetime.datetime.fromtimestamp(base + resumed_s, tz=datetime.timezone.utc).astimezone()
            old['ended_at'] = datetime.datetime.fromtimestamp(base + paused_s, tz=datetime.timezone.utc) \
                .astimezone().isoformat(timespec='seconds')
            self.store.save(old)
            fresh = new_meeting(self._unique_id(start_dt), old['title'], old['source'], start_dt, old.get('language', 'auto'))
            fresh['speakers'] = [dict(s) for s in old['speakers']]
            fresh['turns'] = [{**t, 'start': round(t['start'] - resumed_s, 2), 'end': round(t['end'] - resumed_s, 2)}
                              for t in moved]
            self.meeting = fresh
            self._resumed_turn_index = None
            self._previous = None
            self.store.save(fresh)
            self._set_notice('new')
            self._publish('meeting_turn', {'id': fresh['id'], 'reload': True})
            self._emit()

    # ── Transitions ──────────────────────────────────────────────────────────

    # Two meetings in one second ("New meeting" right away) get -2, -3 ids.
    def _unique_id(self, when: datetime.datetime) -> str:
        base = new_id(when)
        candidate, n = base, 1
        while self.store.path(candidate).exists():
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    def _begin(self, source: dict, notice: Optional[str]):
        now = self._now()
        source = {k: source.get(k, '') for k in ('key', 'app', 'kind', 'title', 'code')}
        language = str(self._settings('meeting.language', 'auto') or 'auto')
        self.meeting = new_meeting(self._unique_id(now), title_for(source), source, now, language)
        self.state = 'live'
        self.excluded = False
        self.paused_by_user = False
        self.behind_s = 0.0
        self._pending = None
        self._speech_s = 0.0
        self._resumed_turn_index = None
        self._gap_open = None
        self.store.save(self.meeting)
        if self._previous is not None and self._previous_until and now <= self._previous_until:
            notice = 'new'
        else:
            self._previous = None
        self._set_notice(notice)
        logger.info(f"Meeting started: {self.meeting['title']} ({source['kind']})")
        self._start_capture(self.meeting)
        self._emit()

    def _pause(self, by_user: bool):
        self.state = 'paused'
        self.paused_by_user = by_user
        self._paused_at = self._now()
        self._open_gap('paused')
        self._stop_capture()
        self.saver.flush()
        gap_s = float(self._settings('meeting.gap_minutes', 10) or 10) * 60
        self._cancel_gap_timer = self._schedule(gap_s, self._gap_elapsed)
        logger.info(f"Meeting paused ({'by you' if by_user else 'source gone'})")
        self._emit()

    def _resume(self, notice: Optional[str]):
        if self._cancel_gap_timer:
            self._cancel_gap_timer()
            self._cancel_gap_timer = None
        self.state = 'live'
        self.paused_by_user = False
        self._paused_at = None
        self._close_gap()
        self._resumed_turn_index = len(self.meeting['turns'])
        self._set_notice(notice)
        self._start_capture(self.meeting)
        self._emit()

    def _gap_elapsed(self):
        with self._lock:
            if self.state == 'paused' and not self.paused_by_user:
                self._end()

    def _end(self, remember: bool = True):
        m = self.meeting
        if self._cancel_gap_timer:
            self._cancel_gap_timer()
            self._cancel_gap_timer = None
        if self.state == 'live':
            self._stop_capture()
        self._close_gap()
        ended = self._paused_at if (self.state == 'paused' and self._paused_at) else self._now()
        m['ended_at'] = ended.isoformat(timespec='seconds')
        self.saver.flush()
        self.store.save(m)
        if remember:
            gap_s = float(self._settings('meeting.gap_minutes', 10) or 10) * 60
            self._previous = m
            self._previous_paused_s = max(0.0, ended.timestamp() - started_ts(m))
            self._previous_until = self._now() + datetime.timedelta(seconds=gap_s)
        else:
            self._previous = None
        self.state = 'ended'
        self.excluded = False
        self.paused_by_user = False
        self._paused_at = None
        logger.info(f"Meeting ended: {m['title']}, {duration_s(m):.0f} s, {len(m['turns'])} turns")
        payload = self.state_payload()
        payload['duration_s'] = round(duration_s(m))
        self._publish('meeting', payload)
        self.meeting = None
        self.state = 'idle'
        self._resumed_turn_index = None
        self._emit()

    def _open_gap(self, reason: str):
        if self._gap_open is not None:
            return
        m = self.meeting
        m['gaps'].append({'start': round(self._elapsed(), 2), 'end': None, 'reason': reason})
        self._gap_open = len(m['gaps']) - 1

    def _close_gap(self):
        m = self.meeting
        if m is None or self._gap_open is None:
            return
        m['gaps'][self._gap_open]['end'] = round(self._elapsed(), 2)
        self._gap_open = None
        self.saver.mark(m)
