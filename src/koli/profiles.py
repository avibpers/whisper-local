# profiles.py
# Named sets of settings on top of your global settings, which they never overwrite.
# One applies per dictation: the one whose key you hold, else the one set on the app's
# rule, else the active one (the default for the whole system). Whatever a profile
# doesn't set is "use global". Design: docs/design/profiles.md.
import logging
from pathlib import Path
from typing import Dict, List, Optional

from ruamel.yaml import YAML

from .defaults_merge import load_layered, shipped_value
from .utils import get_user_app_data_path

logger = logging.getLogger(__name__)

PROFILES_FILE = "profiles.yaml"
PROFILES_DEFAULTS = "profiles.defaults.yaml"

USER_FILE_HEADER = """\
# Koli — your profiles
#
# The profiles that ship with the app are applied underneath this file, so they
# keep improving with each update. Here you only write what's yours:
#
#   profiles:
#     # one of your own: hold its key to dictate with it
#     hebrew:
#       description: Always Hebrew
#       hotkey: ctrl+win+h
#       overrides:
#         whisper:
#           language: he
#
#     # change a shipped one, naming it and only what you want different.
#     # `overrides` is taken whole, so list everything you want that profile
#     # to set, not just what you're changing. The rest uses your global settings.
#     code:
#       overrides:
#         clipboard:
#           auto_paste: false
#
#     # turn a shipped one off
#     translate:
#       disabled: true
#
# Shipped profiles: dictation, chat, code, notes, translate.
# `active` below is the default for the whole system, set from the tray or the app.

"""


class ProfileManager:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.profiles: Dict[str, dict] = {}
        self.active: Optional[str] = None
        self._load()

    def _load(self):
        user_path = Path(get_user_app_data_path()) / PROFILES_FILE
        self.profiles, data = load_layered(
            PROFILES_DEFAULTS, user_path, "profiles", USER_FILE_HEADER,
            mapping=True)
        # A fresh user file holds no `active`, so fall back to the shipped one.
        self.active = data.get("active") or shipped_value(PROFILES_DEFAULTS, "active")

    def list_profiles(self) -> List[str]:
        return list(self.profiles.keys())

    def get_active(self) -> Optional[str]:
        return self.active

    # Files changed (the Apps screen saved one): read them again.
    def reload(self):
        self._load()

    # The default for the whole system. Only says which profile; changes no setting.
    def activate(self, name: str) -> bool:
        if name not in self.profiles:
            return False
        self.active = name
        self._persist_active()
        return True

    # What a profile sets, as dotted paths, leaving out what can't change per dictation.
    def overrides_of(self, name: Optional[str]) -> Dict[str, object]:
        from .settings import schema
        flat: Dict[str, object] = {}

        def walk(prefix: str, value):
            if isinstance(value, dict) and prefix not in schema.PROFILE_PATHS:
                for key, child in value.items():
                    walk(f"{prefix}.{key}" if prefix else str(key), child)
            elif prefix in schema.PROFILE_PATHS:
                flat[prefix] = value
        walk('', (self.profiles.get(name) or {}).get('overrides') or {})
        return flat

    # (profile, key combination) for each profile with a key of its own.
    def hotkeys(self) -> List[tuple]:
        from .config_manager import _resolve_platform_values
        keys = []
        for name, entry in self.profiles.items():
            hotkey = (entry or {}).get('hotkey')
            if hotkey:
                keys.append((name, _resolve_platform_values({'hotkey': str(hotkey)})['hotkey']))
        return keys

    def _persist_active(self):
        path = Path(get_user_app_data_path()) / PROFILES_FILE
        if not path.exists():
            return
        try:
            yaml = YAML()
            with open(path, encoding="utf-8") as f:
                data = yaml.load(f) or {}
            data["active"] = self.active
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(data, f)
        except Exception:
            pass


# Puts one profile on top of the global settings. Settings that are read when used
# just follow; for those a part of Koli holds in memory (the paste setting, the
# engine's language) the appliers set the new value, and set it back afterwards.
class ProfileLayer:
    def __init__(self, config_manager, manager: ProfileManager):
        self.config_manager = config_manager
        self.manager = manager
        self.appliers = None      # set once the app window's settings exist
        self.current: Optional[str] = None
        self.use()

    # The appliers arrived after the layer went on: the profile takes effect now.
    def attach(self, appliers):
        self.appliers = appliers
        for path, value in self.config_manager.overlay.items():
            self._apply(path, value, self.config_manager.get_global(path))

    # `name` for one dictation; none is the active profile, the system's default.
    def use(self, name: Optional[str] = None):
        name = name if name in self.manager.profiles else self.manager.get_active()
        wanted = self.manager.overrides_of(name)
        cm = self.config_manager
        before = cm.overlay
        self.current = name
        if wanted == before:
            return
        touched = set(before) | set(wanted)
        old = {path: cm.get_path(path) for path in touched}
        cm.set_overlay(wanted)
        for path in touched:
            new = cm.get_path(path)
            if new != old[path]:
                self._apply(path, new, old[path])

    def _apply(self, path: str, value, old):
        if self.appliers is None:
            return
        try:
            self.appliers.apply_for_profile(path, value, old)
        except Exception as e:
            logger.warning(f"Profile: couldn't apply {path}: {e}")
