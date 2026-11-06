# agent/chat.py
# The conversation with the agent in its own floating window. It opens on what
# you say after "Anything else?", or when the AI takes a command, and closes
# after you're quiet for wake_word.chat_silence_seconds, or with its Close
# button. It holds what you said and what Koli did, and tells the window
# through the 'chat' event. The wake listener hears you while it is open
# (wake_word.CHAT). Design: docs/design/agent-chat.md.

import logging
import threading
import time
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

MAX_TURNS = 60
DEFAULT_SILENCE_S = 20.0

# The agent's phases (agent/runner.py) as the conversation shows them.
WORKING = ('thinking', 'checking', 'working')                    # a status line, the clock waits
WAITING_ON_YOU = ('ask', 'approve')                             # Koli's turn, the clock waits for your answer
FINISHED = ('done', 'failed', 'not_understood', 'nothing', 'cancelled', 'undone', 'offer_save', 'needs_password')


class ChatSession:
    # `publish(payload)` sends a 'chat' event; `silence_seconds()` reads the
    # setting each time, so a change applies to the open conversation.
    def __init__(self, publish: Callable[[dict], None], silence_seconds: Callable[[], float] = lambda: DEFAULT_SILENCE_S,
                 clock: Callable[[], float] = time.monotonic, timer: Callable = threading.Timer):
        self._publish = publish
        self._silence_seconds = silence_seconds
        self._clock = clock
        self._timer_factory = timer
        self._lock = threading.RLock()
        self._timer = None
        self._open = False
        self.state = 'closed'          # closed, listening, working
        self.turns: List[dict] = []
        self.status = ''
        self._id = 0

    # ── Opening and closing ─────────────────────────────────────────────────

    def is_open(self) -> bool:
        return self._open

    # Opens it, or keeps it open: a new conversation starts empty.
    def open(self, why: str = ''):
        with self._lock:
            if self._open:
                return
            self._open = True
            self._id += 1
            self.turns = []
            self.state, self.status = 'listening', ''
            logger.info(f"Agent chat opened{f' ({why})' if why else ''}")
            self._send({'event': 'open', **self.snapshot()})
            self._arm()

    # `why`: quiet (the silence ran out), closed (its button), or another reason for the log.
    def close(self, why: str = 'closed'):
        with self._lock:
            if not self._open:
                return
            self._open = False
            self.state = 'closed'
            self._cancel_timer()
            logger.info(f"Agent chat closed ({why})")
            self._send({'event': 'close', 'why': why, 'id': self._id})

    # ── What happened ───────────────────────────────────────────────────────

    def you(self, text: str):
        self._add('you', text)

    # Koli's turn: what it did or answered. `kind` colours it on the page.
    def koli(self, text: str, kind: str = 'reply', **extra):
        self._add('koli', text, kind, **extra)

    # Koli is working: a status line, and the silence doesn't count meanwhile.
    def working(self, text: str = 'Working…'):
        with self._lock:
            if not self._open:
                return
            self.state, self.status = 'working', text
            self._cancel_timer()
            self._send({'event': 'status', 'state': self.state, 'status': text, 'id': self._id})

    # Back to listening for you: the silence counts from now.
    def listening(self):
        with self._lock:
            if not self._open:
                return
            self.state, self.status = 'listening', ''
            self._send({'event': 'status', 'state': self.state, 'status': '', 'closes_after': self._silence(),
                        'id': self._id})
            self._arm()

    # An 'agent' event (agent/runner.py) as the conversation shows it. Only
    # while it is open: a quick command's AI action keeps to the bubble.
    def on_agent(self, payload: dict):
        if not self._open or not isinstance(payload, dict):
            return
        phase, text = payload.get('phase', ''), payload.get('text') or ''
        if phase in WORKING:
            self.working(text or 'Working…')
        elif phase in WAITING_ON_YOU:
            self._add('koli', text, phase, needs=payload.get('needs') or '', lines=payload.get('lines') or [])
            with self._lock:
                self.state = 'working'
                self._cancel_timer()
        elif phase in FINISHED:
            self._add('koli', text, phase, lines=payload.get('lines') or [])

    # ── For the page ────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        with self._lock:
            return {'id': self._id, 'open': self._open, 'state': self.state, 'status': self.status,
                    'turns': list(self.turns), 'closes_after': self._silence()}

    # ── Inside ──────────────────────────────────────────────────────────────

    def _add(self, who: str, text: str, kind: str = '', **extra):
        text = (text or '').strip()
        if not text:
            return
        with self._lock:
            if not self._open:
                return
            turn = {'who': who, 'text': text, 'kind': kind, 'at': time.strftime('%H:%M:%S'), **extra}
            self.turns = (self.turns + [turn])[-MAX_TURNS:]
            self._send({'event': 'turn', 'turn': turn, 'id': self._id})
            if self.state == 'listening':
                self._arm()

    def _silence(self) -> float:
        try:
            return max(5.0, float(self._silence_seconds() or DEFAULT_SILENCE_S))
        except (TypeError, ValueError):
            return DEFAULT_SILENCE_S

    def _arm(self):
        self._cancel_timer()
        session = self._id
        self._timer = self._timer_factory(self._silence(), lambda: self._quiet(session))
        self._timer.daemon = True
        self._timer.start()

    # The silence ran out, for the conversation it was set for.
    def _quiet(self, session: int):
        with self._lock:
            if session == self._id and self._open and self.state == 'listening':
                self.close('quiet')

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _send(self, payload: dict):
        try:
            self._publish(payload)
        except Exception as e:
            logger.debug(f"Agent chat event not sent: {e}")
