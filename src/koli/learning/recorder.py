# learning/recorder.py
# Watches each dictation and keeps it as a clip when "Learn from my
# dictations" is on. The state manager hands over the raw microphone audio
# once (begin); everything else comes from the pipeline events it already
# publishes: what Whisper heard, why a clip was dropped, what was typed. The
# file is written on a worker thread, so a dictation never waits for the disk.
# Design: docs/design/self-learning.md.

import collections
import difflib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from . import retention
from .clips import ClipStore, new_id, slim_info

logger = logging.getLogger(__name__)

MIN_SECONDS = 0.3            # shorter is a tap on the keys, not a dictation
REPEAT_WINDOW_S = 30         # "you said it again" only counts this soon after
AFTER_DROP_WINDOW_S = 15     # a dictation this soon after a dropped one in the same window
REPEAT_SIMILARITY = 0.6      # how alike two takes must be to be one sentence said twice
CLEANUP_EVERY_S = 6 * 3600   # age rules run at least this often

# Pipeline stages that end a dictation.
TERMINAL = ('delivered', 'empty', 'error', 'command', 'rephrased')


def _norm(text: str) -> str:
    from ..utils import normalize_spoken
    return normalize_spoken(text or '')


class ClipRecorder:
    def __init__(self, config_manager, app_rules=None, store: Optional[ClipStore] = None,
                 get_window: Optional[Callable[[], dict]] = None, run: Optional[Callable] = None,
                 clock: Callable[[], float] = time.time):
        self.cm = config_manager
        self.app_rules = app_rules
        self.store = store or ClipStore()
        self._get_window = get_window or self._foreground
        self._clock = clock
        self._executor = None if run else ThreadPoolExecutor(max_workers=1, thread_name_prefix='clips')
        self._run = run or self._executor.submit
        self._lock = threading.Lock()
        self._pending: Optional[dict] = None
        self._recent = collections.deque(maxlen=8)   # finished clips, for "said it again"
        self._last_cleanup = 0.0
        self._unsubscribe = None

    # ── Wiring ──────────────────────────────────────────────────────────────

    def start(self):
        if self._unsubscribe is None:
            from ..ui.events import get_bus
            self._unsubscribe = get_bus().subscribe('pipeline', self.on_event)
            self._run(self.cleanup)   # the age rules, once at startup

    def stop(self):
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._executor is not None:
            self._executor.shutdown(wait=True)

    @staticmethod
    def _foreground() -> dict:
        try:
            from ..platform import foreground
            return foreground.get_foreground_app() or {}
        except Exception:
            return {}

    def _setting(self, key: str, default=None):
        cfg = (getattr(self.cm, 'config', None) or {}).get('learning') or {}
        value = cfg.get(key, default)
        return default if value is None else value

    def enabled(self) -> bool:
        return self._setting('enabled', False) is True

    def cap_gb(self) -> float:
        try:
            return float(self._setting('disk_cap_gb', retention.DEFAULT_CAP_GB))
        except (TypeError, ValueError):
            return retention.DEFAULT_CAP_GB

    # Why this dictation is not kept, or '' to keep it.
    def _skip_reason(self, audio, mode: str, window: dict) -> str:
        if not self.enabled():
            return 'off'
        if mode != 'dictate':
            return 'voice commands and rephrase are never kept'
        if audio is None or len(audio) < MIN_SECONDS * 16000:
            return 'too short'
        try:
            rule = self.app_rules.match_for_foreground() if self.app_rules else None
        except Exception:
            rule = None
        if rule and rule.get('keep_audio') is False:
            return f"app rule for {window.get('exe') or 'this app'} says never keep"
        return ''

    # ── From the state manager ──────────────────────────────────────────────

    # A dictation's audio was captured. `kind`: hotkey, auto or continuous.
    # Returns the clip id it will be kept under, or None when it isn't kept.
    def begin(self, audio, kind: str = 'hotkey', mode: str = 'dictate', mic: str = '', phrase: bool = False):
        window = self._get_window() or {}
        reason = self._skip_reason(audio, mode, window)
        if reason:
            if reason != 'off':
                logger.debug(f"Learning: clip not kept ({reason})")
            return None
        clip = {
            'id': new_id(), 'kind': kind, 'phrase': bool(phrase), 'mic': mic,
            'app': window.get('exe', ''), 'title': window.get('title', ''),
            'window': {'hwnd': window.get('hwnd'), 'pid': window.get('pid')},
            'speaker': 'you' if kind == 'hotkey' else 'unverified',
            'seconds': round(len(audio) / 16000, 2), 'started': self._clock(),
        }
        with self._lock:
            stale, self._pending = self._pending, {'clip': clip, 'audio': audio.copy()}
        if stale is not None:   # a dictation that never finished: keep what we know
            self._finish(stale, 'error', 'no end event', '')
        return clip['id']

    # ── Pipeline events ─────────────────────────────────────────────────────

    def on_event(self, topic: str, payload: dict):
        if topic != 'pipeline' or not isinstance(payload, dict):
            return
        with self._lock:
            pending = self._pending
            if pending is None:
                return
            stage = payload.get('stage')
            if stage == 'transcribed':
                pending['raw'] = payload.get('raw') or ''
                pending['info'] = payload.get('info')
                return
            # 'transcribed' already carries Whisper's whole output; later stages
            # carry it trimmed by the guards, and the trimmed pieces are what
            # later phases need to see, so the first one is kept.
            if stage not in TERMINAL:
                return
            self._pending = None
        if stage in ('command', 'rephrased'):
            return   # never kept, even if the mode turned out to be one late
        if stage == 'error':
            self._finish(pending, 'error', payload.get('error') or '', '')
        elif stage == 'empty':
            pending.setdefault('info', payload.get('info'))
            self._finish(pending, 'dropped', payload.get('why') or 'unknown', '')
        else:
            delivery = payload.get('outcome') or ''
            text = payload.get('text') or ''
            pending['ai'] = payload.get('ai')
            if delivery == 'empty' or not text:
                self._finish(pending, 'dropped', 'post-processing left nothing', '', delivery)
            elif delivery == 'failed':
                self._finish(pending, 'failed', '', text, delivery)
            else:
                self._finish(pending, 'typed', '', text, delivery)

    # ── Finishing a clip ────────────────────────────────────────────────────

    def _finish(self, pending: dict, outcome: str, why: str, text: str, delivery: str = ''):
        clip = dict(pending['clip'])
        info = slim_info(pending.get('info'))
        clip.update({
            'outcome': outcome if outcome != 'dropped' else f'dropped: {why}',
            'why': why, 'delivery': delivery,
            'language': info.get('language') or '',
            'live': {'text': pending.get('raw', ''), 'info': info},
            'final_text': text, 'signals': self._signals(clip, outcome, text),
            'trust': 'none', 'distilled_at': None,
        })
        # `live.text` is what you said. `final_text` may be the AI's: learning must
        # never take its words for yours, so this says exactly what it changed.
        if pending.get('ai'):
            clip['ai'] = pending['ai']
        started = clip.pop('started', None)
        self._recent.appendleft({'id': clip['id'], 't': started or self._clock(), 'window': self._window_key(clip),
                                 'outcome': outcome, 'norm': _norm(text)})
        self._run(lambda: self._write(clip, pending['audio']))

    @staticmethod
    def _window_key(clip: dict) -> tuple:
        w = clip.get('window') or {}
        return (clip.get('app'), w.get('hwnd'), w.get('pid'))

    # Free signals: you said the same thing again soon after in the same
    # window (the second take is likely what you meant), or you dictated
    # again right after Koli dropped one (that one was probably real speech).
    def _signals(self, clip: dict, outcome: str, text: str) -> dict:
        signals = {}
        if outcome != 'typed' or not text:
            return signals
        now, where, norm = self._clock(), self._window_key(clip), _norm(text)
        for earlier in self._recent:
            age = now - earlier['t']
            if age > REPEAT_WINDOW_S:
                break
            if earlier['window'] != where:
                continue
            if earlier['outcome'] == 'typed' and earlier['norm']:
                similarity = difflib.SequenceMatcher(a=earlier['norm'], b=norm, autojunk=False).ratio()
                if similarity >= REPEAT_SIMILARITY:
                    signals['repeats'] = earlier['id']
                    signals['similarity'] = round(similarity, 2)
                    break
            elif earlier['outcome'] == 'dropped' and age <= AFTER_DROP_WINDOW_S:
                signals['after_drop'] = earlier['id']
                break
        return signals

    def _write(self, clip: dict, audio):
        try:
            self.store.add(clip, audio)
        except Exception as e:
            logger.warning(f"Learning: couldn't keep the clip ({e})")
            return
        if self.store.used_bytes() > self.cap_gb() * 1e9 \
                or self._clock() - self._last_cleanup > CLEANUP_EVERY_S:
            self.cleanup()

    def cleanup(self) -> dict:
        self._last_cleanup = self._clock()
        try:
            return retention.enforce(self.store, self.cap_gb())
        except Exception as e:
            logger.warning(f"Learning: cleanup failed ({e})")
            return {}

    # Tests and shutdown: wait until every clip handed over is on disk.
    def flush(self):
        if self._executor is not None:
            self._executor.submit(lambda: None).result(timeout=10)
