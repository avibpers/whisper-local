# platform/windows/hotkeys.py
# Global hotkey registration via the `global-hotkeys` library, including the key
# name mapping from this app's config syntax to the library's expected format.
# macOS mirror: platform/macos/hotkeys.py (NSEvent taps).
import time

from global_hotkeys import (clear_hotkeys, register_hotkeys, start_checking_hotkeys,
                             stop_checking_hotkeys)
from global_hotkeys.keycodes import vk_key_names

# global-hotkeys library expects: 'control + window + shift' format
KEY_MAP = {
    'ctrl': 'control',
    'win': 'window',
    'windows': 'window',
    'cmd': 'window',
    'super': 'window',
    'esc': 'escape',
    'pageup': 'page_up',
    'pagedown': 'page_down',
}

def _normalize_hotkey(hotkey_str: str) -> str:
    keys = hotkey_str.lower().split('+')
    converted = [KEY_MAP.get(k.strip(), k.strip()) for k in keys]
    return ' + '.join(converted)

# The keys in a hotkey string the backend can't register, as typed. Checked
# before saving: register() clears every binding first, so one bad key there
# would leave all hotkeys dead. ',' is the library's chord separator, so it
# can never be a key even though it has a key code.
def unsupported_keys(hotkey_str: str) -> list:
    bad = []
    for key in (k.strip().lower() for k in str(hotkey_str or '').split('+')):
        name = KEY_MAP.get(key, key)
        if not key or key == ',' or name not in vk_key_names:
            bad.append(key)
    return bad

# ── Shortcuts Windows keeps for itself ───────────────────────────────────────
# Windows grabs its input-language shortcuts before any app sees them: with
# "Switch to English" on Alt+Shift+1, a shortcut recorder only ever gets Alt
# and Shift, never the 1. The settings live in the registry (Settings → Time &
# language → Typing → Advanced keyboard settings → Input language hot keys).

_TOGGLE_KEY = r'Keyboard Layout\Toggle'
_IME_HOTKEYS_KEY = r'Control Panel\Input Method\Hot Keys'
# Toggle values: 1 Alt+Shift, 2 Ctrl+Shift, 3 not assigned, 4 the grave key.
_TOGGLE_CHORDS = {'1': 'alt+shift', '2': 'ctrl+shift', '4': '`'}
# "Switch to <language>" shortcuts are the Hot Keys subkeys 0x100-0x11F; the
# others belong to Chinese/Japanese input methods and only act inside them.
_DIRECT_SWITCH_IDS = range(0x100, 0x120)
_IME_MODIFIERS = ((0x2, 'ctrl'), (0x1, 'alt'), (0x4, 'shift'))
# Virtual-key codes for the keys a language shortcut can use, named the way
# the shortcut recorder names them.
_VK_NAMES = {0x20: 'space', 0xBA: ';', 0xBB: '=', 0xBC: ',', 0xBD: '-', 0xBE: '.', 0xBF: '/',
             0xC0: '`', 0xDB: '[', 0xDC: '\\', 0xDD: ']', 0xDE: "'"}


def _vk_name(vk: int):
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    if 0x70 <= vk <= 0x87:
        return f'f{vk - 0x6F}'
    return _VK_NAMES.get(vk)


# "English (United States)" for the language in a keyboard-layout handle.
def _language_name(hkl: int) -> str:
    import ctypes
    buffer = ctypes.create_unicode_buffer(128)
    LOCALE_SLOCALIZEDDISPLAYNAME = 0x2
    if ctypes.windll.kernel32.GetLocaleInfoW(hkl & 0xFFFF, LOCALE_SLOCALIZEDDISPLAYNAME, buffer, 128):
        return buffer.value
    return 'another language'


def _toggle_shortcuts(winreg) -> list:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _TOGGLE_KEY) as key:
            def value(name):
                try:
                    return str(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    return None
            language = value('Language Hotkey') or value('Hotkey')
            layout = value('Layout Hotkey')
    except OSError:
        return []
    found = []
    if language in _TOGGLE_CHORDS:
        found.append({'hotkey': _TOGGLE_CHORDS[language], 'label': 'switch the keyboard language'})
    if layout in _TOGGLE_CHORDS and layout != language:
        found.append({'hotkey': _TOGGLE_CHORDS[layout], 'label': 'switch the keyboard layout'})
    return found


def _direct_switch_shortcuts(winreg) -> list:
    found = []
    for hotkey_id in _DIRECT_SWITCH_IDS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf'{_IME_HOTKEYS_KEY}\{hotkey_id:08X}') as key:
                modifiers = int.from_bytes(winreg.QueryValueEx(key, 'Key Modifiers')[0][:4], 'little')
                vk = int.from_bytes(winreg.QueryValueEx(key, 'Virtual Key')[0][:4], 'little')
                hkl = int.from_bytes(winreg.QueryValueEx(key, 'Target IME')[0][:4], 'little')
        except (OSError, TypeError):
            continue
        name = _vk_name(vk)
        if not name or not hkl:
            continue  # unassigned, or a key the recorder can't produce anyway
        mods = [mod for bit, mod in _IME_MODIFIERS if modifiers & bit]
        found.append({'hotkey': '+'.join(mods + [name]), 'label': f'switch to {_language_name(hkl)}'})
    return found


# Every shortcut Windows keeps for itself, as [{'hotkey': 'alt+shift+1',
# 'label': 'switch to English (United States)'}]. Never raises: a missing or
# odd registry entry just leaves that shortcut out.
def os_reserved_hotkeys() -> list:
    import winreg
    return _toggle_shortcuts(winreg) + _direct_switch_shortcuts(winreg)

# REPLACES the current bindings — it does not add to them. The macOS mirror
# rebinds its whole list on every call, and callers rely on that: the pause
# hotkey re-registers a reduced set and then the full set again on resume.
#
# global-hotkeys keeps its registrations across stop_checking_hotkeys(), so
# without clear_hotkeys() the second register() raises "already registered".
# That exception aborted the pause path before start() could run, leaving the
# listener stopped with every hotkey dead — including pause itself, so there
# was no way back without restarting the app (issue #6).
def register(bindings: list):
    clear_hotkeys()
    normalized = []
    for binding in bindings:
        hotkey_str = binding[0]
        normalized_binding = [_normalize_hotkey(hotkey_str)] + binding[1:]
        normalized.append(normalized_binding)
    register_hotkeys(normalized)

def start():
    start_checking_hotkeys()

# global-hotkeys' stop only flips a flag — it never joins the checker thread,
# which polls on a 20 ms cycle. Returning immediately leaves a window where a
# caller that re-registers next (settings change, transforms refresh) clears
# the bindings dict while that thread is still iterating it, raising
# RuntimeError and killing it (issue #7). Give it 10 poll cycles to notice.
# The library's own restart path waits 0.7 s for the same reason.
_STOP_SETTLE_SECONDS = 0.2


def stop():
    stop_checking_hotkeys()
    time.sleep(_STOP_SETTLE_SECONDS)
