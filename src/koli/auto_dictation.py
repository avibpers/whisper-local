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
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._running = False
        self._last_focus_id = None
        self._pending: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self.logger = logging.getLogger(__name__)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def enabled(self) -> bool:
        cfg = self.cm.config.get('auto_dictation') or {}
        return bool(cfg.get('enabled', False))

    def is_running(self) -> bool:
        return self._running

    # Hooks the platform watcher when the setting is on. Idempotent, and a
    # platform without a watcher (macOS for now) just stays off.
    def start(self) -> bool:
        if self._running:
            return True
        if not self.enabled():
            return False
        watcher = self._get_watcher()
        if watcher is None:
            self.logger.info("Auto-dictation: not available on this platform")
            return False
        try:
            ok = bool(watcher.start(self.on_focus, self.on_typing))
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
        self._last_focus_id = None
        watcher = self._get_watcher()
        if watcher is not None:
            try:
                watcher.stop()
            except Exception as e:
                self.logger.debug(f"Auto-dictation: watcher stop failed: {e}")
        self.logger.info("Auto-dictation off")

    # Called by the settings applier and after a profile switch: starts or
    # stops to match the current setting.
    def apply_settings(self) -> None:
        if self.enabled():
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
        focus_id = info.get('id')
        if focus_id is not None and focus_id == self._last_focus_id:
            return  # the same field again: it only re-arms after focus moved elsewhere
        self._last_focus_id = focus_id
        self._cancel_pending()  # a newer focus wins over a pending start

        # Focus left the field we were listening to: the words have nowhere
        # safe to go (a paste would land wherever you clicked), so drop them,
        # the same as typing. A new field may then start its own recording.
        try:
            if self.sm.is_auto_recording():
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
    def on_typing(self) -> None:
        if not self._running:
            return
        self._cancel_pending()
        try:
            if self.sm.is_auto_recording():
                self.sm.cancel_auto_recording('you started typing')
        except Exception as e:
            self.logger.debug(f"Auto-dictation: typing cancel failed: {e}")

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
        if getattr(self.sm, 'is_paused', False):
            return 'hotkeys paused'
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
