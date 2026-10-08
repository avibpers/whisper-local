# delayed_send.py
# Auto-send with a pause to think: after the text is pasted, Koli waits a few
# seconds before pressing Enter, so you can read it and stop the send. Esc, the
# bubble's Cancel button, a new dictation, typing, or switching to another
# window stop it; "Send now" skips the wait. Each step is published on the
# event bus as 'auto_send' for the floating bubble.

import logging
import math
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

TICK_SECONDS = 0.1   # how often the countdown checks the clock and the window


# What identifies "the window the text went to": the window handle where the
# platform gives one (Windows), else the app and its process (macOS). None when
# the foreground can't be read, in which case the window check is skipped.
def window_identity(info: Optional[dict]):
    info = info or {}
    if info.get('hwnd'):
        return ('hwnd', info['hwnd'])
    if info.get('exe') or info.get('pid'):
        return ('app', info.get('exe'), info.get('pid'))
    return None


def _default_foreground() -> dict:
    try:
        from .platform import foreground
        return foreground.get_foreground_app() or {}
    except Exception:
        return {}


def _default_publish(topic: str, payload: dict) -> None:
    try:
        from .ui.events import publish
        publish(topic, payload)
    except Exception:
        pass


class DelayedSend:
    # `send_enter` presses Enter and returns True on success. The rest are
    # seams for the tests: the foreground probe, the event bus, the keyboard
    # watcher (platform input_watch by default), the clock, and whether a
    # thread drives poll() (the tests call it themselves).
    def __init__(self, send_enter: Callable[[], bool], foreground: Optional[Callable[[], dict]] = None,
                 publish: Optional[Callable[[str, dict], None]] = None, typing_watch=None,
                 clock: Callable[[], float] = time.monotonic, run_thread: bool = True):
        self._send_enter = send_enter
        self._foreground = foreground or _default_foreground
        self._publish = publish or _default_publish
        self._typing_watch = typing_watch
        self._clock = clock
        self._run_thread = run_thread
        self._lock = threading.RLock()
        self._pending: Optional[dict] = None
        self._thread: Optional[threading.Thread] = None

    def is_pending(self) -> bool:
        with self._lock:
            return self._pending is not None

    # ── Starting, finishing, cancelling ──────────────────────────────────────

    # Starts the countdown for the text that was just pasted. A send already
    # waiting is replaced quietly: only the newest message gets sent.
    def schedule(self, seconds: float) -> None:
        seconds = max(0.0, float(seconds))
        with self._lock:
            if self._pending is not None:
                self._end('cancelled', 'a newer message replaced it', announce=False)
            now = self._clock()
            self._pending = {
                'deadline': now + seconds, 'total': seconds, 'shown': None,
                'target': window_identity(self._foreground()), 'token': self._watch_typing(),
            }
            self._announce(now)
        print(f"   ⏳ Sending in {math.ceil(seconds)}s — Esc to cancel")
        logger.info(f"Auto-send waits {seconds:g}s before Enter")
        if self._run_thread and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self._run, name='delayed-send', daemon=True)
            self._thread.start()

    # Stops a waiting send. True when there was one; the text stays where it is.
    def cancel(self, reason: str) -> bool:
        with self._lock:
            if self._pending is None:
                return False
            self._end('cancelled', reason)
            return True

    # The bubble's "Send now": press Enter without waiting for the countdown.
    def send_now(self) -> bool:
        with self._lock:
            if self._pending is None:
                return False
            return self._fire() == 'sent'

    # One step of the countdown: drop the send if the window changed, press
    # Enter when the time is up, otherwise update the seconds shown. Returns
    # the outcome once the send is over ('sent', 'failed', 'not_sent').
    def poll(self) -> Optional[str]:
        with self._lock:
            pending = self._pending
            if pending is None:
                return None
            if pending['target'] is not None and window_identity(self._foreground()) != pending['target']:
                self._end('not_sent', 'you switched to another window')
                return 'not_sent'
            now = self._clock()
            if now >= pending['deadline']:
                return self._fire()
            self._announce(now)
            return None

    # ── Internals (called with the lock held) ────────────────────────────────

    def _fire(self) -> str:
        pending, self._pending = self._pending, None
        self._release_watch(pending)
        try:
            ok = bool(self._send_enter())
        except Exception as e:
            logger.error(f"Auto-send: Enter failed: {e}")
            ok = False
        state = 'sent' if ok else 'failed'
        self._publish('auto_send', {'state': state, 'reason': ''})
        logger.info(f"Auto-send: {state}")
        return state

    def _end(self, state: str, reason: str, announce: bool = True) -> None:
        pending, self._pending = self._pending, None
        self._release_watch(pending)
        if not announce:
            return
        self._publish('auto_send', {'state': state, 'reason': reason})
        print(f"   ✋ Not sent: {reason}")
        logger.info(f"Auto-send {state}: {reason}")

    # Publishes the seconds left, once per whole second.
    def _announce(self, now: float) -> None:
        pending = self._pending
        remaining = max(1, math.ceil(pending['deadline'] - now - 1e-9))
        if remaining != pending['shown']:
            pending['shown'] = remaining
            self._publish('auto_send', {'state': 'counting', 'remaining': remaining,
                                        'total': max(1, math.ceil(pending['total']))})

    # Typing during the wait (fixing a word, or pressing Enter yourself) means
    # "I'll send it myself". Koli's own paste and Enter are injected keys,
    # which the watcher ignores.
    def _watch_typing(self) -> int:
        watch = self._typing_watch
        if watch is None:
            try:
                from .platform import input_watch as watch
            except Exception:
                return 0
            self._typing_watch = watch
        try:
            return watch.start(None, self._on_typing) or 0
        except Exception as e:
            logger.debug(f"Auto-send: typing watch unavailable ({e})")
            return 0

    def _release_watch(self, pending: Optional[dict]) -> None:
        if pending and pending.get('token') and self._typing_watch is not None:
            try:
                self._typing_watch.stop(pending['token'])
            except Exception as e:
                logger.debug(f"Auto-send: typing watch stop failed ({e})")

    def _on_typing(self, key: str = '') -> None:
        self.cancel('you started typing')

    # The countdown thread: ticks until nothing is waiting.
    def _run(self) -> None:
        while True:
            time.sleep(TICK_SECONDS)
            try:
                self.poll()
            except Exception as e:
                logger.error(f"Auto-send countdown failed: {e}")
            with self._lock:
                if self._pending is None:
                    self._thread = None
                    return
