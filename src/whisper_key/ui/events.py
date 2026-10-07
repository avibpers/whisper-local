# ui/events.py
# A small thread-safe publish/subscribe bus. The engine publishes what it is
# doing (state, levels, pipeline stages, notifications, tasks, settings); the UI
# bridge subscribes and forwards to the web pages. Publishing never raises and
# never blocks on a slow subscriber, so it is safe from the audio thread.

import logging
import threading
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
