# platform/windows/session.py
# Whether the PC is locked, so the wake word stops listening on the lock
# screen. While the secure desktop (lock screen, sign-in, a UAC prompt) has
# the input, the user's desktop can't be opened or switched to.

import ctypes
from ctypes import wintypes

DESKTOP_SWITCHDESKTOP = 0x0100

try:
    _user32 = ctypes.WinDLL('user32', use_last_error=True)
    _user32.OpenInputDesktop.restype = wintypes.HANDLE
    _user32.OpenInputDesktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _user32.SwitchDesktop.restype = wintypes.BOOL
    _user32.SwitchDesktop.argtypes = (wintypes.HANDLE,)
    _user32.CloseDesktop.restype = wintypes.BOOL
    _user32.CloseDesktop.argtypes = (wintypes.HANDLE,)
except (AttributeError, OSError):
    _user32 = None


# True on the lock screen. Any failure to ask reads as unlocked: the wake
# word listening a little too long is better than never listening.
def is_locked() -> bool:
    if _user32 is None:
        return False
    try:
        desktop = _user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if not desktop:
            return True
        try:
            return not _user32.SwitchDesktop(desktop)
        finally:
            _user32.CloseDesktop(desktop)
    except Exception:
        return False
