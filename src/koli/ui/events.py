# ui/events.py
# A small thread-safe publish/subscribe bus. The engine publishes what it is
# doing (state, levels, pipeline stages, notifications, tasks, settings); the UI
# bridge subscribes and forwards to the web pages. Publishing never raises and
# never blocks on a slow subscriber, so it is safe from the audio thread.

import itertools
import logging
import threading
import time
from collections import defaultdict
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Topics in use. Listed here so publishers and the web side agree on names.
TOPICS = (
    'state',          # {'state': idle|recording|processing|model_loading, 'mode': dictate|command|rephrase, 'paused': bool}
    'level',          # {'level': 0..1}
    'vad',            # {'probability', 'speaking', 'silence_s', 'onset', 'offset', 'timeout_s'}
    'streaming_text', # {'text', 'final'}
    'pipeline',       # {'stage', ...} one dictation's progress, see StateManager
    'notify',         # {'kind', 'message'}
    'log',            # one log record dict
    'task',           # {'id', 'label', 'progress', 'status', 'error', 'detail'}
    'settings',       # {'path', 'old', 'new', 'applied', 'source'}
    'foreground',     # {'exe', 'title', 'rule'}
    'devices_changed',
    'window',         # {'action': 'show'|'hide', 'page'}
    'problem',        # {'id', 'message', 'time'} something you must see, see report_problem
    'meeting',        # the meeting panel's whole state, see SessionManager.state_payload
    'meeting_turn',   # {'id', 'turn'} a transcribed turn; {'id', 'pending'} one in progress; {'id', 'reload': True}
    'wake',           # {'phase': awake|follow_up|sleeping} the wake word, see wake_word.py
    'agent',          # {'phase', 'text', ...} an AI action, see agent/runner.py
)

Subscriber = Callable[[str, Any], None]


class EventBus:
    def __init__(self):
        self._subscribers = defaultdict(list)
        self._lock = threading.Lock()

    # Subscribe to one topic, or '*' for every topic. Returns an unsubscribe
    # function so callers never need to keep the callable around themselves.
    def subscribe(self, topic: str, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers[topic].append(fn)

        def unsubscribe():
            with self._lock:
                try:
                    self._subscribers[topic].remove(fn)
                except ValueError:
                    pass
        return unsubscribe

    def publish(self, topic: str, payload: Any = None):
        with self._lock:
            targets = list(self._subscribers.get(topic, ())) + list(self._subscribers.get('*', ()))
        for fn in targets:
            try:
                fn(topic, payload)
            except Exception as e:  # a broken subscriber must never break the engine
                logger.debug(f"Event subscriber failed on '{topic}': {e}")


# One bus per process. Modules publish through publish() below so the engine
# needs no wiring changes, and a process without a UI pays almost nothing.
_bus = EventBus()


def get_bus() -> EventBus:
    return _bus


def publish(topic: str, payload: Any = None):
    _bus.publish(topic, payload)


# ── Problems ─────────────────────────────────────────────────────────────────
# Something that went wrong and that you must see: a model that didn't load, a
# background job that failed, a transcription that broke. Logged, published
# as 'problem' (the bubble flashes it, the app shows a toast that stays), and
# kept until dismissed, so an app window opened later still shows it.

MAX_PROBLEMS = 10
_problems: list = []
_problem_ids = itertools.count(1)
_problems_lock = threading.Lock()


def report_problem(message: str) -> dict:
    problem = {'id': next(_problem_ids), 'message': str(message), 'time': time.time()}
    with _problems_lock:
        _problems.append(problem)
        del _problems[:-MAX_PROBLEMS]
    logger.warning(f"Problem: {message}")
    publish('problem', problem)
    return problem


def open_problems() -> list:
    with _problems_lock:
        return list(_problems)


def dismiss_problem(problem_id) -> None:
    with _problems_lock:
        _problems[:] = [p for p in _problems if p['id'] != problem_id]
