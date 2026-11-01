# meeting/meet_feed.py
# What the Koli Chrome extension reads off a Google Meet page: whether you
# joined, the meeting's title, who is in it, Meet's live captions with the
# speaker's name, and (when Meet shows it) who is talking. Pure state with
# times in epoch seconds, so the meeting service can ask "who spoke between
# t0 and t1" of the same clock its audio chunks use. The HTTP side is
# meet_link.py; the design is docs/design/meet-extension.md.

import threading
import time
from typing import Optional

# ── Tuning ───────────────────────────────────────────────────────────────────

HEARTBEAT_TIMEOUT_S = 30.0   # a Meet tab not heard from this long counts as gone (browser closed, crashed)
# A caption block appears a moment after the person starts talking: its first
# words are already said when the page shows them. The span used for naming
# audio starts this much earlier than the block was first seen.
CAPTION_LEAD_S = 1.0
NAME_COVER = 0.5             # share of a chunk one person's caption spans must cover to name it
SPAN_KEEP_S = 600.0          # spans older than this are forgotten (only recent audio is named)
CAPTION_KEEP = 400           # caption blocks remembered per call (finished ones are handed out once)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


class MeetCall:
    """One Meet tab's call, keyed by its meeting code (abc-defg-hij)."""

    def __init__(self, code: str):
        self.code = code
        self.title = ''
        self.joined = False
        self.captions_on = False
        self.self_name = ''
        self.people = []           # [{'id', 'name', 'self'}], the latest list the page showed
        self.seen = 0.0            # time.time() of the last message from the tab
        self.joined_at = None      # epoch seconds the extension saw you get in
        self.captions = {}         # block id -> {'name','self','text','t0','t1','final'}
        self.handed = set()        # block ids already turned into transcript turns
        self.spans = []            # [(t0, t1, name)] of the other people talking, for naming audio

    def remote_spans(self, t0: float, t1: float) -> list:
        return [s for s in self.spans if _overlap(s[0], s[1], t0, t1) > 0]


class MeetFeed:
    """Every Meet call the extension reports, safe to use from any thread."""

    def __init__(self, now=time.time):
        self._now = now
        self._lock = threading.Lock()
        self._calls = {}           # code -> MeetCall

    # ── Incoming events ──────────────────────────────────────────────────────

    # Applies a batch from the extension. Returns what the meeting service
    # acts on: captions that just finished (each once), the codes of calls
    # you just joined or left, and whether the people or title changed.
    def apply(self, events: list) -> dict:
        out = {'finals': [], 'joined': [], 'left': [], 'changed': []}
        now = self._now()
        with self._lock:
            for e in events or []:
                if not isinstance(e, dict):
                    continue
                code = str(e.get('code') or '').strip().lower()
                if not code:
                    continue
                call = self._calls.get(code)
                if call is None:
                    call = self._calls[code] = MeetCall(code)
                call.seen = now
                kind = e.get('type')
                if kind == 'status':
                    self._status(call, e, now, out)
                elif kind == 'caption':
                    self._caption(call, e, out)
                elif kind == 'speaking':
                    self._speaking(call, e)
                elif kind == 'left':
                    if call.joined:
                        out['left'].append(code)
                    call.joined = False
                    call.captions_on = False
                    for block_id in list(call.captions):
                        self._finish(call, block_id, out)
            self._forget_old(now)
        return out

    def _status(self, call: MeetCall, e: dict, now: float, out: dict):
        joined = bool(e.get('joined'))
        if joined and not call.joined:
            call.joined_at = float(e.get('at_ms') or now * 1000) / 1000.0
            out['joined'].append(call.code)
        elif not joined and call.joined:
            out['left'].append(call.code)
        call.joined = joined
        call.captions_on = bool(e.get('captions')) and joined
        call.self_name = str(e.get('self_name') or call.self_name or '')
        title = str(e.get('title') or '').strip()
        people = [{'id': str(p.get('id') or ''), 'name': str(p.get('name') or '').strip(), 'self': bool(p.get('self'))}
                  for p in (e.get('people') or []) if isinstance(p, dict) and str(p.get('name') or '').strip()]
        if title != call.title or (people and people != call.people):
            out['changed'].append(call.code)
        call.title = title or call.title
        if people:
            call.people = people

    # A caption block grows in place while its speaker talks; the extension
    # sends its whole text each time, and final once a newer block took over.
    def _caption(self, call: MeetCall, e: dict, out: dict):
        block_id = str(e.get('id') or '')
        name = str(e.get('name') or '').strip()
        if not block_id or not name or block_id in call.handed:
            return
        t0 = float(e.get('first_ms') or 0) / 1000.0
        t1 = max(t0, float(e.get('last_ms') or 0) / 1000.0)
        block = call.captions.get(block_id)
        if block is None:
            block = call.captions[block_id] = {'name': name, 'self': bool(e.get('self')), 'text': '', 't0': t0, 't1': t1}
        block['text'] = str(e.get('text') or '').strip()
        block['t1'] = max(block['t1'], t1)
        if not block['self']:
            self._add_span(call, block['t0'] - CAPTION_LEAD_S, block['t1'], name, key=block_id)
        if e.get('final'):
            self._finish(call, block_id, out)

    def _finish(self, call: MeetCall, block_id: str, out: dict):
        block = call.captions.pop(block_id, None)
        if block is None or block_id in call.handed:
            return
        call.handed.add(block_id)
        if len(call.handed) > CAPTION_KEEP * 4:
            call.handed = set(list(call.handed)[-CAPTION_KEEP:])
        if block['text']:
            out['finals'].append({'code': call.code, 'id': block_id, **block})

    # Meet's own "this tile is talking" highlight, when the extension could see it.
    def _speaking(self, call: MeetCall, e: dict):
        name = str(e.get('name') or '').strip()
        if not name or e.get('self'):
            return
        t0 = float(e.get('start_ms') or 0) / 1000.0
        t1 = max(t0, float(e.get('end_ms') or 0) / 1000.0)
        self._add_span(call, t0, t1, name)

    # One span per caption block (its key), stretched as the block grows.
    @staticmethod
    def _add_span(call: MeetCall, t0: float, t1: float, name: str, key: Optional[str] = None):
        if key is not None:
            for i, s in enumerate(call.spans):
                if len(s) > 3 and s[3] == key:
                    call.spans[i] = (min(s[0], t0), max(s[1], t1), name, key)
                    return
        call.spans.append((t0, t1, name, key))

    def _forget_old(self, now: float):
        for call in self._calls.values():
            call.spans = [s for s in call.spans if s[1] >= now - SPAN_KEEP_S]
        stale = [c for c, call in self._calls.items()
                 if not call.joined and now - call.seen > HEARTBEAT_TIMEOUT_S * 4]
        for code in stale:
            del self._calls[code]

    # ── Queries ──────────────────────────────────────────────────────────────

    # Calls you are in whose tab still reports: [{'code','title','people','captions_on','joined_at'}].
    def joined_calls(self) -> list:
        now = self._now()
        with self._lock:
            return [{'code': c.code, 'title': c.title, 'people': [dict(p) for p in c.people],
                     'captions_on': c.captions_on, 'joined_at': c.joined_at, 'self_name': c.self_name}
                    for c in self._calls.values() if c.joined and now - c.seen <= HEARTBEAT_TIMEOUT_S]

    def call(self, code: str) -> Optional[dict]:
        return next((c for c in self.joined_calls() if c['code'] == (code or '').lower()), None)

    # Whether Meet's captions are on in this call right now (the extension says so).
    def captions_on(self, code: str) -> bool:
        c = self.call(code)
        return bool(c and c['captions_on'])

    # The one other person who was talking between t0 and t1 (epoch seconds),
    # by Meet's captions and speaking highlight; None when nobody, more than
    # one person, or too little of the stretch is covered. Voiceprints are
    # taught from this, so anything unsure is None: a skipped chunk costs
    # nothing, a voice taught under the wrong name spoils it.
    def name_at(self, code: str, t0: float, t1: float) -> Optional[str]:
        if t1 <= t0:
            return None
        with self._lock:
            call = self._calls.get((code or '').lower())
            if call is None:
                return None
            spans = call.remote_spans(t0, t1)
        names = {s[2] for s in spans}
        if len(names) != 1:
            return None
        name = names.pop()
        covered = self._covered([(s[0], s[1]) for s in spans], t0, t1)
        return name if covered >= NAME_COVER * (t1 - t0) else None

    # Seconds of [t0, t1] inside the union of `ranges`.
    @staticmethod
    def _covered(ranges: list, t0: float, t1: float) -> float:
        total, end = 0.0, t0
        for a, b in sorted(ranges):
            a, b = max(a, end), min(b, t1)
            if b > a:
                total += b - a
                end = b
        return total
