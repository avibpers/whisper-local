# ui/log_stream.py
# Feeds the app's log records into the Live console. A logging.Handler that
# keeps the last 2,000 records in memory (so a window opened later still shows
# recent history) and publishes each new record on the event bus.

import collections
import logging
import threading
import time

from .events import publish

MAX_RECORDS = 2000

_LEVEL_CLASS = {
    'DEBUG': 'debug', 'INFO': 'info', 'WARNING': 'warn', 'ERROR': 'error', 'CRITICAL': 'error',
}


class LogStreamHandler(logging.Handler):
    def __init__(self, level=logging.DEBUG):
        super().__init__(level)
        self._records = collections.deque(maxlen=MAX_RECORDS)
        self._records_lock = threading.Lock()
        self._seq = 0

    def emit(self, record: logging.LogRecord):
        try:
            message = record.getMessage()
            if record.exc_info:
                message += '\n' + logging.Formatter().formatException(record.exc_info)
            with self._records_lock:
                self._seq += 1
                item = {
                    'seq': self._seq,
                    't': time.strftime('%H:%M:%S', time.localtime(record.created)),
                    'ts': record.created,
                    'lv': record.levelname,
                    'cls': _LEVEL_CLASS.get(record.levelname, 'info'),
                    'mod': record.name.replace('koli.', ''),
                    'msg': message,
                }
                self._records.append(item)
            publish('log', item)
        except Exception:
            self.handleError(record)

    # The most recent `n` records, oldest first.
    def tail(self, n: int = 200) -> list:
        with self._records_lock:
            items = list(self._records)
        return items[-n:] if n else items

    def clear(self):
        with self._records_lock:
            self._records.clear()


_handler = None


# Attach the stream handler to the root logger exactly once. setup_logging()
# clears root handlers, so main.py calls this again after any logging reset.
def install(level=logging.DEBUG) -> LogStreamHandler:
    global _handler
    root = logging.getLogger()
    if _handler is None:
        _handler = LogStreamHandler(level)
    if _handler not in root.handlers:
        root.addHandler(_handler)
    return _handler


def get_handler() -> LogStreamHandler:
    return _handler or install()
