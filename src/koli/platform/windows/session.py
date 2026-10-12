# platform/windows/session.py
# Whether the PC is locked, so the wake word stops listening on the lock
# screen. While the secure desktop (lock screen, sign-in, a UAC prompt) has
# the input, the user's desktop can't be opened or switched to.

import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger(__name__)
_warned = False

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


# True on the lock screen. When it can't be asked, the PC counts as locked:
# the wake word must never run commands on a locked computer, so it stops
# listening rather than guess (and says why once, in the log).
def is_locked() -> bool:
    global _warned
    try:
        if _user32 is None:
            raise OSError('user32 is not available')
        desktop = _user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if not desktop:
            return True
        try:
            return not _user32.SwitchDesktop(desktop)
        finally:
            _user32.CloseDesktop(desktop)
    except Exception as e:
        if not _warned:
            _warned = True
            logger.warning(f"Can't tell whether the PC is locked ({e}); the wake word won't listen")
        return True
