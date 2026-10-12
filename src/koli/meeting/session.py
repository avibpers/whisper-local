# meeting/session.py
# When a meeting starts, pauses, resumes and ends. Pure logic: audio, windows
# and files are reached only through the callbacks handed in, so every rule in
# docs/design/meeting-mode.md is tested without hardware. One meeting at a time.
# Capture callbacks run after the session lock is released (a transition
# queues them as effects), so the audio threads can never wait on this lock
# while it waits on them.

import contextlib
import datetime
import difflib
import logging
import re
import threading
from typing import Callable, Optional

import numpy as np

from .store import MeetingStore, ThrottledSaver, duration_s, new_id, new_meeting, started_ts
from .voices import is_default_name

logger = logging.getLogger(__name__)

STATES = ('idle', 'offered', 'live', 'paused', 'ended')
NOTICE_SECONDS = 6.0
ECHO_SIMILARITY = 0.6    # text match for a short mic turn to count as the call's echo
ECHO_WORD_SHARE = 0.35   # share of a mic turn's words also heard in the call at that time
ECHO_MIN_WORDS = 3       # shorter mic turns ("כן") need the strict one-turn match
ECHO_WINDOW = 20         # recent turns an echo is looked for in


def _overlap(a: dict, b: dict) -> float:
    return min(a['end'], b['end']) - max(a['start'], b['start'])


def _words(text: str) -> list:
    return re.findall(r'\w+', text.lower())


# True when `mic` (a microphone turn) repeats what the call (`loops`, the
# loopback turns) said at the same time. The mic's copy is transcribed from
# fainter audio and is cut at other seconds, so one mic turn often spans two
# loopback turns and reads differently from both. Its words are therefore
# looked for in all the overlapping loopback turns joined: in a real meeting
# copies kept 48–78% of their words and unrelated speech under 11%. Talking
# over each other has different words; saying the same thing later has no
# overlap. A turn of a word or two must match one loopback turn closely, so a
# quick "yes" is never dropped because someone else said it too.
# (autojunk is off: on texts over 200 characters it treats common letters as
# noise, and two copies of the same sentence scored 0.02.)
def is_echo(mic: dict, loops: list) -> bool:
    duration = mic['end'] - mic['start']
    overlapping = sorted((t for t in loops if _overlap(mic, t) > 0), key=lambda t: t['start'])
    if duration <= 0 or not overlapping:
        return False
    mic_words = _words(mic['text'])
    if len(mic_words) < ECHO_MIN_WORDS:
        return any(_overlap(mic, t) >= 0.5 * min(duration, t['end'] - t['start'])
                   and difflib.SequenceMatcher(None, mic['text'].lower(), t['text'].lower(),
                                               autojunk=False).ratio() >= ECHO_SIMILARITY
                   for t in overlapping)
    if sum(_overlap(mic, t) for t in overlapping) < 0.5 * duration:
        return False
    call_words = [w for t in overlapping for w in _words(t['text'])]
    matcher = difflib.SequenceMatcher(None, mic_words, call_words, autojunk=False)
    found = sum(block.size for block in matcher.get_matching_blocks())
    return found / len(mic_words) >= ECHO_WORD_SHARE

# Two labels that are one person, in a meeting dict: `from_id`'s turns
# become `into_id`'s, the voiceprints combine by how many chunks made each,
# and `from_id` goes away. Returns whether anything changed.
def merge_speaker_into(meeting: dict, from_id: str, into_id: str) -> bool:
    speakers = {s['id']: s for s in meeting['speakers']}
    source, target = speakers.get(from_id), speakers.get(into_id)
    if source is None or target is None or source is target or 'you' in (from_id, into_id):
        return False
    if source.get('centroid') and target.get('centroid'):
        a, b = np.asarray(target['centroid']), np.asarray(source['centroid'])
        na, nb = int(target.get('count', 1)), int(source.get('count', 1))
        merged = a * na + b * nb
        target['centroid'] = [round(float(x), 5) for x in merged / (np.linalg.norm(merged) or 1.0)]
        target['count'] = na + nb
    for turn in meeting['turns']:
        if turn['speaker'] == from_id:
            turn['speaker'] = into_id
    meeting['speakers'] = [s for s in meeting['speakers'] if s['id'] != from_id]
    return True


# The speaker in `meeting` other than `speaker_id` already called `name`.
def same_name_as(meeting: dict, speaker_id: str, name: str) -> Optional[str]:
    name = str(name).strip().lower()
    return next((s['id'] for s in meeting['speakers']
                 if s['id'] not in (speaker_id, 'you') and s['name'].strip().lower() == name), None)

# Friendly names for the apps the shipped rules know.
APP_NAMES = {'discord.exe': 'Discord', 'zoom.exe': 'Zoom', 'ms-teams.exe': 'Teams', 'teams.exe': 'Teams',
             'slack.exe': 'Slack', 'chrome.exe': 'Chrome', 'msedge.exe': 'Edge', 'brave.exe': 'Brave'}
MANUAL_SOURCE = {'key': 'manual', 'app': '', 'kind': 'manual', 'title': '', 'code': '', 'device': ''}
SOURCE_KEYS = ('key', 'app', 'kind', 'title', 'code', 'device')


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
                 schedule: Callable = None, save_interval: float = 5.0,
                 on_speakers: Optional[Callable[[list], None]] = None):
        self.store = store
        self.saver = ThrottledSaver(store, save_interval)
        self._start_capture = start_capture
        self._stop_capture = stop_capture
        self._publish = publish
        self._settings = settings
        self._now = now or _now
        self._schedule = schedule or _default_schedule
        self._on_speakers = on_speakers   # the speaker list to work with changed (resume, continue, split)
        self._lock = threading.RLock()
        self._effects = []               # callbacks to run once the lock is released
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

    # ── Lock discipline ──────────────────────────────────────────────────────

    # Every public method changes state under the lock and runs the capture
    # callbacks it queued after letting go of it.
    @contextlib.contextmanager
    def _transition(self):
        with self._lock:
            yield
            effects, self._effects = self._effects, []
        for fn in effects:
            try:
                fn()
            except Exception as e:
                logger.error(f"Meeting capture callback failed: {e}")

    def _after(self, fn: Callable):
        self._effects.append(fn)

    # ── Queries ──────────────────────────────────────────────────────────────

    def current(self) -> Optional[dict]:
        return self.meeting

    # The source waiting for speech while nothing runs, or None.
    def pending_source(self) -> Optional[dict]:
        with self._lock:
            return dict(self._pending) if self.state == 'idle' and self._pending else None

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
        with self._transition():
            self.notice = None
            self._cancel_notice = None
            self._emit()

    # ── Detection ────────────────────────────────────────────────────────────

    # The detector's view, once a second. Only changes matter.
    def on_sources(self, sources: list):
        with self._transition():
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
        with self._transition():
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
        with self._transition():
            if self.state != 'offered' or not self._pending:
                return
            self._begin(self._pending, notice=None)

    def offer_decline(self):
        with self._transition():
            if self.state != 'offered' or not self._pending:
                return
            self._declined.add(self._pending['key'])
            self._pending = None
            self.state = 'idle'
            self._emit()

    # ── Commands ─────────────────────────────────────────────────────────────

    def start(self, source: Optional[dict] = None):
        with self._transition():
            if self.state in ('live', 'paused'):
                return
            source = source or self._pending or (self._sources[0] if self._sources else None) or MANUAL_SOURCE
            self._begin(source, notice=None)

    def end(self):
        with self._transition():
            if self.state == 'offered':
                self._declined.add(self._pending['key'])
                self._pending = None
                self.state = 'idle'
                self._emit()
            elif self.state in ('live', 'paused'):
                self._end()

    def new(self):
        with self._transition():
            if self.state not in ('live', 'paused'):
                return
            source = dict(self.meeting['source'])
            self._end(remember=False)
            self._begin(source, notice=None)

    def pause(self):
        with self._transition():
            if self.state == 'live':
                self._pause(by_user=True)

    def resume(self):
        with self._transition():
            if self.state == 'paused':
                self._resume(notice=None)

    def set_excluded(self, on: bool):
        with self._transition():
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

    # A plain write: the transcriber reports this from inside the audio path.
    def set_behind(self, seconds: float):
        self.behind_s = float(seconds)

    # ── Turns and speakers ───────────────────────────────────────────────────

    def _stored_turn(self, meeting: dict, turn: dict) -> dict:
        base = started_ts(meeting)
        return {'start': round(float(turn['t0']) - base, 2), 'end': round(float(turn['t1']) - base, 2),
                'stream': turn['stream'], 'speaker': turn['speaker'], 'text': turn['text'],
                'lang': turn.get('lang', '')}

    # A turn from the transcriber, with wall-clock t0/t1 (time.time()) and the
    # id of the meeting its audio came from. One that arrives after its
    # meeting ended still lands in that meeting's file, never in the next one.
    # The microphone can hear the call (a headset leaking, a desk mic): a mic
    # turn that repeats an overlapping loopback turn is an echo and is
    # dropped, whichever of the two was transcribed first.
    def add_turn(self, turn: dict):
        meeting_id = turn.get('meeting_id')
        with self._transition():
            m = self.meeting
            if m and (meeting_id is None or meeting_id == m['id']):
                if self.state not in ('live', 'paused'):
                    return
                stored = self._stored_turn(m, turn)
                recent = m['turns'][-ECHO_WINDOW:]
                recent_call = [t for t in recent if t['stream'] == 'loopback']
                if stored['stream'] == 'mic' and is_echo(stored, recent_call):
                    logger.info("Dropped a microphone turn that echoed the call")
                    return
                echoes = [t for t in recent if t['stream'] == 'mic' and is_echo(t, recent_call + [stored])] \
                    if stored['stream'] == 'loopback' else []
                for echo in echoes:
                    m['turns'].remove(echo)
                m['turns'].append(stored)
                self.saver.mark(m)
                if echoes:
                    logger.info(f"Dropped {len(echoes)} microphone turn(s) that echoed the call")
                    self._publish('meeting_turn', {'id': m['id'], 'reload': True})
                else:
                    self._publish('meeting_turn', {'id': m['id'], 'turn': stored})
            elif meeting_id:
                self._append_saved(meeting_id, turn)

    def _append_saved(self, meeting_id: str, turn: dict):
        target = self._previous if self._previous and self._previous['id'] == meeting_id else None
        if target is None:
            try:
                target = self.store.load(meeting_id)
            except Exception as e:
                logger.warning(f"Turn for unknown meeting {meeting_id} dropped: {e}")
                return
        stored = self._stored_turn(target, turn)
        target['turns'].append(stored)
        self.store.save(target)
        self._publish('meeting_turn', {'id': meeting_id, 'turn': stored})

    # A speaker seen for the first time, or one whose voiceprint moved. The
    # name is the user's to change, so an update never touches it, with one
    # exception: `voice`, a name you gave this voice in an earlier meeting,
    # replaces a "Speaker N" that Koli gave.
    def ensure_speaker(self, speaker: dict):
        speaker = dict(speaker)
        voice = speaker.pop('voice', None)
        with self._transition():
            m = self.meeting
            if not m:
                return
            for i, s in enumerate(m['speakers']):
                if s['id'] == speaker['id']:
                    m['speakers'][i] = {**s, **{k: v for k, v in speaker.items() if k != 'name'}}
                    named = bool(voice) and is_default_name(s.get('name'))
                    if named:
                        m['speakers'][i]['name'] = voice
                    self.saver.mark(m)
                    if named:
                        self._emit()
                    return
            m['speakers'].append(speaker)
            self.saver.mark(m)
            self._emit()

    def rename_speaker(self, speaker_id: str, name: str):
        with self._transition():
            m = self.meeting
            if not m:
                return
            for s in m['speakers']:
                if s['id'] == speaker_id:
                    s['name'] = str(name).strip()[:40] or s['name']
            self.saver.mark(m)
            self._emit()

    def merge_speakers(self, from_id: str, into_id: str):
        with self._transition():
            m = self.meeting
            if not m or not merge_speaker_into(m, from_id, into_id):
                return
            self.saver.mark(m)
            self._emit()
            self._publish('meeting_turn', {'id': m['id'], 'reload': True})

    def _speakers_changed(self, speakers: list):
        if self._on_speakers:
            self._after(lambda: self._on_speakers([dict(s) for s in speakers]))

    # ── Resume or new, by hand ───────────────────────────────────────────────

    # "Continue previous instead": the turns of this new meeting move onto the
    # previous one, which comes back live; the new file goes away.
    def continue_previous(self):
        with self._transition():
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
            self.saver.flush()
            self.store.delete(new['id'])
            self.meeting = prev
            self._previous = None
            self._resumed_turn_index = len(prev['turns']) - len(new['turns'])
            self.saver.mark(prev)
            self._speakers_changed(prev['speakers'])
            self._set_notice('resumed')
            self._publish('meeting_turn', {'id': prev['id'], 'reload': True})
            self._emit()

    # "Start new instead" after a resume: what came after the resume becomes
    # a meeting of its own; the previous one ends where it paused.
    def split_resumed(self):
        with self._transition():
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
            self.saver.flush()
            self.store.save(old)
            fresh = new_meeting(self._unique_id(start_dt), old['title'], old['source'], start_dt, old.get('language', 'auto'))
            fresh['speakers'] = [dict(s) for s in old['speakers']]
            fresh['turns'] = [{**t, 'start': round(t['start'] - resumed_s, 2), 'end': round(t['end'] - resumed_s, 2)}
                              for t in moved]
            self.meeting = fresh
            self._resumed_turn_index = None
            self._previous = None
            self.store.save(fresh)
            self._speakers_changed(fresh['speakers'])
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
        source = {k: source.get(k, '') for k in SOURCE_KEYS}
        language = str(self._settings('meeting.language', 'auto') or 'auto')
        meeting = new_meeting(self._unique_id(now), title_for(source), source, now, language)
        self.meeting = meeting
        self.state = 'live'
        self.excluded = False
        self.paused_by_user = False
        self.behind_s = 0.0
        self._pending = None
        self._speech_s = 0.0
        self._resumed_turn_index = None
        self._gap_open = None
        self.store.save(meeting)
        if self._previous is not None and self._previous_until and now <= self._previous_until:
            notice = 'new'
        else:
            self._previous = None
        self._set_notice(notice)
        logger.info(f"Meeting started: {meeting['title']} ({source['kind']})")
        self._after(lambda: self._start_capture(meeting))
        self._emit()

    def _pause(self, by_user: bool):
        self.state = 'paused'
        self.paused_by_user = by_user
        self._paused_at = self._now()
        self._open_gap('paused')
        self._after(self._stop_capture)
        self.saver.flush()
        gap_s = float(self._settings('meeting.gap_minutes', 10) or 10) * 60
        self._cancel_gap_timer = self._schedule(gap_s, self._gap_elapsed)
        logger.info(f"Meeting paused ({'by you' if by_user else 'source gone'})")
        self._emit()

    def _resume(self, notice: Optional[str]):
        if self._cancel_gap_timer:
            self._cancel_gap_timer()
            self._cancel_gap_timer = None
        meeting = self.meeting
        self.state = 'live'
        self.paused_by_user = False
        self._paused_at = None
        self._close_gap()
        self._resumed_turn_index = len(meeting['turns'])
        self._set_notice(notice)
        self._after(lambda: self._start_capture(meeting))
        self._emit()

    def _gap_elapsed(self):
        with self._transition():
            if self.state == 'paused' and not self.paused_by_user:
                self._end()

    def _end(self, remember: bool = True):
        m = self.meeting
        if self._cancel_gap_timer:
            self._cancel_gap_timer()
            self._cancel_gap_timer = None
        if self.state == 'live':
            self._after(self._stop_capture)
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
