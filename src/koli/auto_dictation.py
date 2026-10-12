# auto_dictation.py
# Starts a recording when a text field gets keyboard focus, so you can dictate
# without a hotkey. The platform's input_watch reports focus changes and real
# typing; this coordinator holds the policy (which fields, which apps, when
# not) and asks the state manager to begin or cancel. No OS calls live here,
# so the policy is tested with a fake watcher. Design: docs/design/auto-dictation.md

import logging
import os
import threading
from typing import Optional


class AutoDictation:
    # A later focus report within this window cancels the pending start, so
    # tabbing through a form does not start a recording per field.
    DEBOUNCE_SECONDS = 0.3

    def __init__(self, state_manager, config_manager, app_rules, watcher=None, own_pid: Optional[int] = None):
        self.sm = state_manager
        self.cm = config_manager
        self.app_rules = app_rules
        self._watcher = watcher
        self._watch_token = 0   # our listener on the shared watcher
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._running = False
        self._last_focus: Optional[dict] = None
        self._pending: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self.logger = logging.getLogger(__name__)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    # The Settings switch: on everywhere unless an app rule turns it off.
    def enabled(self) -> bool:
        cfg = self.cm.config.get('auto_dictation') or {}
        return bool(cfg.get('enabled', False))

    # An app rule set to On turns it on in that app even with the switch off,
    # so the watcher has to run for those apps too.
    def _wanted(self) -> bool:
        if self.enabled():
            return True
        try:
            return bool(self.app_rules and self.app_rules.any_rule_sets('auto_dictation'))
        except Exception:
            return False

    def is_running(self) -> bool:
        return self._running

    # Hooks the platform watcher when the setting or an app rule wants it.
    # Idempotent, and a platform without a watcher (macOS for now) stays off.
    def start(self) -> bool:
        if self._running:
            return True
        if not self._wanted():
            return False
        watcher = self._get_watcher()
        if watcher is None:
            self.logger.info("Auto-dictation: not available on this platform")
            return False
        try:
            self._watch_token = watcher.start(self.on_focus, self.on_typing)
            ok = bool(self._watch_token)
        except Exception as e:
            self.logger.warning(f"Auto-dictation: could not hook focus and typing ({e})")
            return False
        self._running = ok
        if ok:
            self.logger.info("Auto-dictation on: a focused text field starts the recording")
        return ok

    def stop(self) -> None:
        self._cancel_pending()
        if not self._running:
            return
        self._running = False
        self._last_focus = None
        watcher = self._get_watcher()
        if watcher is not None:
            try:
                watcher.stop(self._watch_token)
                self._watch_token = 0
            except Exception as e:
                self.logger.debug(f"Auto-dictation: watcher stop failed: {e}")
        self.logger.info("Auto-dictation off")

    # Called by the settings applier, after a profile switch and after an app
    # rule changes: starts or stops to match the setting and the rules.
    def apply_settings(self) -> None:
        if self._wanted():
            self.start()
        else:
            self.stop()

    def _get_watcher(self):
        if self._watcher is None:
            try:
                from .platform import input_watch
                self._watcher = input_watch
            except Exception:
                return None
        return self._watcher

    # ── Watcher callbacks (arrive on the watcher's thread) ───────────────────

    # One focus report per focus change: {'id', 'editable', 'password', 'hwnd',
    # 'root_pid', 'exe'}. Only an editable, non-password field in another
    # app's window that was not the last thing focused may start a recording.
    def on_focus(self, info: dict) -> None:
        if not self._running:
            return
        info = info or {}
        if self._same_field(info, self._last_focus):
            return  # the same field again: it only re-arms after focus moved elsewhere
        previous, self._last_focus = self._last_focus, info
        self._cancel_pending()  # a newer focus wins over a pending start

        # Focus left the field we were listening to: the words have nowhere
        # safe to go (a paste would land wherever you clicked), so drop them,
        # the same as typing. A new field may then start its own recording.
        try:
            if self.sm.is_auto_recording():
                self.logger.info(f"Auto-dictation: focus moved from {self._describe(previous)} to {self._describe(info)}")
                self.sm.cancel_auto_recording('the text field lost focus')
        except Exception as e:
            self.logger.debug(f"Auto-dictation: blur cancel failed: {e}")

        reason = self._decline_reason(info)
        if reason:
            self.logger.debug(f"Auto-dictation: not starting ({reason}) in {info.get('exe') or '?'}")
            return
        with self._lock:
            timer = threading.Timer(self.DEBOUNCE_SECONDS, self._start_after_debounce)
            timer.daemon = True
            self._pending = timer
            timer.start()

    # Typing during an auto recording means "I'll write it myself": cancel and
    # drop the audio. Typing during a hotkey recording is none of our business.
    def on_typing(self, key: str = '') -> None:
        if not self._running:
            return
        self._cancel_pending()
        try:
            if self.sm.is_auto_recording():
                self.sm.cancel_auto_recording('you started typing' + (f' ({key})' if key else ''))
        except Exception as e:
            self.logger.debug(f"Auto-dictation: typing cancel failed: {e}")

    # ── Field identity ───────────────────────────────────────────────────────

    # The same field can come back with a new runtime id (Chromium apps
    # re-create a box's DOM node when they re-render) or as an inner or outer
    # node of the same box. Same window and one rectangle inside the other
    # means it is still the field we are listening to.
    @staticmethod
    def _same_field(a: Optional[dict], b: Optional[dict]) -> bool:
        if not a or not b:
            return False
        if a.get('id') is not None and a.get('id') == b.get('id'):
            return True
        if a.get('hwnd') != b.get('hwnd') or not a.get('rect') or not b.get('rect'):
            return False
        return AutoDictation._contains(a['rect'], b['rect']) or AutoDictation._contains(b['rect'], a['rect'])

    @staticmethod
    def _contains(outer, inner, slack: int = 2) -> bool:
        return (outer[0] - slack <= inner[0] and outer[1] - slack <= inner[1]
                and outer[2] + slack >= inner[2] and outer[3] + slack >= inner[3])

    @staticmethod
    def _describe(info: Optional[dict]) -> str:
        if not info:
            return 'nothing'
        kind = 'text field' if info.get('editable') else 'non-text element'
        return f"{kind} in {info.get('exe') or '?'} at {info.get('rect')} (id {info.get('id')})"

    # ── Policy ───────────────────────────────────────────────────────────────

    def _decline_reason(self, info: dict) -> str:
        if not info.get('editable'):
            return 'not a text field'
        if info.get('password'):
            return 'password field'
        if info.get('root_pid') == self._own_pid:
            return "Koli's own window"
        exe = (info.get('exe') or '').lower()
        if exe and exe in getattr(self.sm, 'NON_TEXT_EXES', ()):
            return 'not an app window'
        try:
            rule = self.app_rules.match_for_foreground() if self.app_rules else None
        except Exception:
            rule = None
        if rule and rule.get('auto_dictation') is False:
            return f"app rule {rule.get('id') or rule.get('match')} turns it off"
        if not self.enabled() and not (rule and rule.get('auto_dictation') is True):
            return 'off here (on only in apps whose rule turns it on)'
        if getattr(self.sm, 'is_paused', False):
            return 'hotkeys paused'
        # During a meeting the microphone hears the call: every click into a
        # text field recorded it, and pieces of the call were typed into
        # Discord and Cursor. The record hotkey still works when you mean it.
        meetings = getattr(self.sm, 'meetings', None)
        if meetings is not None and meetings.is_running():
            return 'a meeting is on'
        if not self.sm.can_start_recording():
            return 'busy'
        return ''

    def _start_after_debounce(self) -> None:
        with self._lock:
            self._pending = None
        if not self._running:
            return
        try:
            self.sm.begin_auto_recording()
        except Exception as e:
            self.logger.warning(f"Auto-dictation: could not start recording: {e}")

    def _cancel_pending(self) -> None:
        with self._lock:
            timer, self._pending = self._pending, None
        if timer is not None:
            timer.cancel()

    # Tests: run the pending start now instead of waiting for the debounce.
    def flush_timer(self) -> None:
        with self._lock:
            timer = self._pending
        if timer is not None:
            timer.cancel()
            timer.function()
