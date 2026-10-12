# settings/service.py
# The one way settings change while the app runs: coerce and validate the value,
# save it (debounced), apply it to the running components, remember it for
# Undo, and tell every open window. Also watches user_settings.yaml so edits made
# by the CLI or by hand are applied the same way.

import collections
import copy
import logging
import threading
import time
from typing import Any, Optional

from ..ui.events import publish
from . import schema

logger = logging.getLogger(__name__)

HOTKEY_PATHS = ('hotkey.recording_hotkey', 'hotkey.stop_key', 'hotkey.auto_send_key',
                'hotkey.cancel_combination', 'hotkey.command_hotkey', 'hotkey.rephrase_hotkey',
                'hotkey.pause_hotkey', 'hotkey.paste_last_hotkey', 'hotkey.meeting_hotkey',
                'hotkey.meeting_mute_hotkey')

# Chords Windows uses to switch keyboard language when a second layout is
# installed (Hebrew, Arabic, Russian...). Assigning them breaks typing.
LAYOUT_SWITCH_CHORDS = ({'alt', 'shift'}, {'ctrl', 'shift'})


class SettingsError(ValueError):
    pass


# Keys in a shortcut the OS hotkey backend can't register. Without a backend
# (the lean test env) nothing is refused here; the listener still rolls back.
def _unsupported_keys(value) -> list:
    try:
        from ..platform import hotkeys
    except Exception:
        return []
    return list(hotkeys.unsupported_keys(value))


def _norm_hotkey(value) -> frozenset:
    if not value:
        return frozenset()
    return frozenset(p.strip().lower() for p in str(value).split('+') if p.strip())


class SettingsService:
    UNDO_DEPTH = 50

    def __init__(self, config_manager, appliers=None):
        self.cm = config_manager
        self.appliers = appliers
        self._undo = collections.deque(maxlen=self.UNDO_DEPTH)
        self._lock = threading.RLock()
        self._watch_stop = threading.Event()
        self._watch_thread = None

    # ── Reading ──────────────────────────────────────────────────────────────

    def get(self, path: str, default: Any = None) -> Any:
        return self.cm.get_path(path, default)

    def has(self, path: str) -> bool:
        sentinel = object()
        return self.cm.get_path(path, sentinel) is not sentinel

    # The whole config as plain data for the page.
    def snapshot(self) -> dict:
        from ..config_manager import _to_plain
        with self._lock:
            return copy.deepcopy(_to_plain(self.cm.config))

    def default_of(self, path: str) -> Any:
        node = self.cm._defaults_baseline
        for key in path.split('.'):
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        from ..config_manager import _to_plain
        return copy.deepcopy(_to_plain(node))

    # ── Writing ──────────────────────────────────────────────────────────────

    # Change one setting. Returns {ok, path, value, old, applied, task_id,
    # warnings} or raises SettingsError with a message for the user.
    def set(self, path: str, value: Any, source: str = 'ui', record_undo: bool = True) -> dict:
        setting = schema.get(path)
        if setting is None:
            raise SettingsError(f"Unknown setting: {path}")
        if setting.apply == schema.INTERNAL and source == 'ui':
            raise SettingsError(f"{setting.label} is managed by the app")
        try:
            value = schema.coerce(setting, value)
        except ValueError as e:
            raise SettingsError(str(e))
        warnings = self._validate(path, value)

        with self._lock:
            old = copy.deepcopy(self.cm.get_path(path))
            if old == value:
                return {'ok': True, 'path': path, 'value': value, 'old': old, 'applied': 'unchanged',
                        'task_id': None, 'warnings': warnings}
            self.cm.set_path(path, value)
            if record_undo:
                self._undo.append({'changes': [(path, old)], 't': time.time(), 'label': setting.label})

        result = {'applied': setting.apply, 'task_id': None}
        if self.appliers is not None:
            try:
                result = self.appliers.apply(path, value, old) or result
            except Exception as e:
                logger.error(f"Applying {path} failed: {e}", exc_info=True)
                # A shortcut that can't be registered is useless saved, and the
                # listener has already gone back to the old one: match it.
                if path in HOTKEY_PATHS:
                    with self._lock:
                        self.cm.set_path(path, old)
                        if record_undo and self._undo:
                            self._undo.pop()
                    raise SettingsError(f"That shortcut couldn't be registered, so your previous "
                                        f"one is kept. ({e})")
                warnings.append(f"Saved, but could not apply it right now: {e}")
        out = {'ok': True, 'path': path, 'value': value, 'old': old, 'warnings': warnings,
               'label': setting.label, **result}
        publish('settings', {'path': path, 'old': old, 'new': value, 'source': source,
                             'applied': out.get('applied'), 'task_id': out.get('task_id'),
                             'label': setting.label, 'undoable': record_undo})
        return out

    # Several settings as one change (Simple mode answers, profile activation).
    # One Undo restores all of them.
    def set_many(self, patch: dict, source: str = 'ui', label: str = '') -> dict:
        results, olds = [], []
        for path, value in patch.items():
            before = copy.deepcopy(self.cm.get_path(path))
            res = self.set(path, value, source=source, record_undo=False)
            if res.get('applied') != 'unchanged':
                olds.append((path, before))
            results.append(res)
        if olds:
            with self._lock:
                self._undo.append({'changes': olds, 't': time.time(), 'label': label or 'Several settings'})
        return {'ok': True, 'results': results,
                'task_ids': [r['task_id'] for r in results if r.get('task_id')]}

    # Put back the values from the most recent change. Returns what was undone.
    def undo(self) -> Optional[dict]:
        with self._lock:
            if not self._undo:
                return None
            entry = self._undo.pop()
        for path, old in reversed(entry['changes']):
            try:
                self.set(path, old, source='undo', record_undo=False)
            except SettingsError as e:
                logger.warning(f"Undo of {path} failed: {e}")
        return {'label': entry['label'], 'paths': [p for p, _ in entry['changes']]}

    def can_undo(self) -> bool:
        return bool(self._undo)

    def reset_section(self, section: str) -> dict:
        patch = {s.path: self.default_of(s.path) for s in schema.SCHEMA
                 if s.section == section and s.apply != schema.INTERNAL and self.has(s.path)}
        return self.set_many(patch, source='ui', label=f"Reset {section}")

    # Back to defaults everywhere, keeping the user's favored words.
    def reset_all(self) -> dict:
        patch = {s.path: self.default_of(s.path) for s in schema.SCHEMA
                 if s.apply != schema.INTERNAL and s.path != 'whisper.hotwords' and self.has(s.path)}
        return self.set_many(patch, source='ui', label='Reset all settings')

    # Called by the settings service's own revert path when a background
    # change fails: restores the old value without applying it again.
    def revert_silently(self, path: str, old: Any):
        with self._lock:
            self.cm.set_path(path, old)
        publish('settings', {'path': path, 'old': None, 'new': old, 'source': 'revert',
                             'applied': 'reverted', 'undoable': False})

    # ── Validation across settings ───────────────────────────────────────────

    def _validate(self, path: str, value: Any) -> list:
        warnings = []
        if path in HOTKEY_PATHS:
            keys = _norm_hotkey(value)
            if keys:
                bad = _unsupported_keys(value)
                if bad:
                    shown = ', '.join(f"“{k}”" for k in bad)
                    raise SettingsError(f"{shown} can't be used in a shortcut on this computer. "
                                        "Your previous shortcut is kept.")
                for other in HOTKEY_PATHS:
                    if other == path:
                        continue
                    if _norm_hotkey(self.cm.get_path(other)) == keys:
                        other_label = schema.get(other).label
                        if {path, other} == {'hotkey.stop_key', 'hotkey.auto_send_key'}:
                            raise SettingsError(f"Already used by “{other_label}”. Stop and send-with-Enter need different keys.")
                        raise SettingsError(f"Already used by “{other_label}”.")
                if any(chord <= keys for chord in LAYOUT_SWITCH_CHORDS):
                    warnings.append("This chord also switches the keyboard language on Windows "
                                    "when you have a second layout (Hebrew, Arabic, Russian…).")
            elif path == 'hotkey.recording_hotkey':
                raise SettingsError("The record shortcut can't be empty.")
        if path == 'vad.vad_offset_threshold' and value > (self.cm.get_path('vad.vad_onset_threshold') or 1):
            warnings.append("“Speech ends at” is higher than “Speech starts at”, so detection may flicker.")
        if path == 'vad.vad_onset_threshold' and value < (self.cm.get_path('vad.vad_offset_threshold') or 0):
            warnings.append("“Speech starts at” is lower than “Speech ends at”, so detection may flicker.")
        if path == 'audio.noise_suppression.enabled' and value:
            from ..services import system
            if not system.extra_installed('noise'):
                warnings.append("Noise suppression needs an extra package that isn't installed yet. "
                                "Install it from Settings → Microphone & audio.")
        if path == 'server.host' and str(value).strip() not in ('127.0.0.1', 'localhost', '::1'):
            warnings.append(schema.get(path).warn)
        return warnings

    # ── Watching for edits made outside the app ──────────────────────────────

    def start_watching(self, interval: float = 1.5):
        if self._watch_thread:
            return

        def loop():
            last_error = ''
            while not self._watch_stop.wait(interval):
                try:
                    changed = self.cm.reload_if_changed_externally()
                except Exception as e:
                    logger.debug(f"Settings watch failed: {e}")
                    continue
                for path in changed:
                    self._apply_external(path)
                error = getattr(self.cm, 'reload_error', '')
                if error and error != last_error:
                    publish('notify', {'kind': 'settings_file', 'level': 'warn',
                                       'message': "user_settings.yaml was edited but can't be read, so the app "
                                                  f"keeps your current settings. {error}"})
                last_error = error

        self._watch_thread = threading.Thread(target=loop, daemon=True, name='settings-watch')
        self._watch_thread.start()

    def stop_watching(self):
        self._watch_stop.set()

    def _apply_external(self, path: str):
        # A changed map entry (e.g. whisper.models.foo) applies as its map.
        setting = schema.get(path)
        probe = path
        while setting is None and '.' in probe:
            probe = probe.rsplit('.', 1)[0]
            setting = schema.get(probe)
        if setting is None:
            return
        value = self.cm.get_path(setting.path)
        if self.appliers is not None:
            try:
                self.appliers.apply(setting.path, value, None)
            except Exception as e:
                logger.warning(f"Could not apply edited setting {setting.path}: {e}")
        publish('settings', {'path': setting.path, 'old': None, 'new': value, 'source': 'file',
                             'applied': setting.apply, 'label': setting.label, 'undoable': False})
