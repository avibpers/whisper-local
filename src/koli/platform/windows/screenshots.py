# platform/windows/screenshots.py
# Screenshots for meetings: open the Windows snip overlay (the same one as
# Win+Shift+S), tell when the clipboard changed, and read the snip off it.
# Only an image the Snipping Tool put there counts, so an image copied from
# a browser during a meeting is never taken for a screenshot.

import ctypes
import logging
import os
from ctypes import wintypes
from typing import Optional

logger = logging.getLogger(__name__)

# The processes behind the overlay: the modern one, and the classic tool.
SNIP_PROCESSES = ('screenclippinghost.exe', 'snippingtool.exe')
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

try:
    _user32 = ctypes.WinDLL('user32', use_last_error=True)
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    _user32.GetClipboardOwner.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD))
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = (_EnumWindowsProc, wintypes.LPARAM)
except (AttributeError, OSError):
    _user32 = _kernel32 = None


def available() -> bool:
    return _user32 is not None


# The Windows snip overlay, as Win+Shift+S opens it. The snip lands on the
# clipboard; nothing comes back from here.
def open_overlay() -> None:
    os.startfile('ms-screenclip:')


# Changes whenever anything is put on the clipboard; cheap to poll.
def clipboard_sequence() -> int:
    if _user32 is None:
        return 0
    return int(_user32.GetClipboardSequenceNumber())


# The image name of a window's process, lower-cased, or None when it can't
# be read (no window, a protected process).
def _process_name(hwnd) -> Optional[str]:
    if not hwnd:
        return None
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return os.path.basename(buf.value).lower()
    finally:
        _kernel32.CloseHandle(handle)


def _is_snip_process(name: Optional[str]) -> bool:
    return bool(name) and name in SNIP_PROCESSES


# Whether the snip overlay is on screen: a visible top-level window of the
# Snipping Tool. Gone with no new image means the user pressed Esc.
def overlay_open() -> bool:
    if _user32 is None:
        return False
    found = []

    def visit(hwnd, _lparam):
        if _user32.IsWindowVisible(hwnd) and _is_snip_process(_process_name(hwnd)):
            found.append(hwnd)
            return False
        return True

    _user32.EnumWindows(_EnumWindowsProc(visit), 0)
    return bool(found)


# True when the Snipping Tool owns the clipboard's contents, False when some
# other app does, None when there is no owner window or it can't be read.
def clipboard_owner_is_snip() -> Optional[bool]:
    if _user32 is None:
        return None
    name = _process_name(_user32.GetClipboardOwner())
    if name is None:
        return None
    return _is_snip_process(name)


# The snip on the clipboard as a Pillow image, or None when the clipboard
# holds no image or an image that isn't a snip. An owner that can't be read
# is accepted: better one stray image than a missed snip.
def grab_snip():
    try:
        from PIL import ImageGrab
        image = ImageGrab.grabclipboard()
    except Exception as e:
        logger.debug(f"Clipboard image not readable: {e}")
        return None
    if image is None or isinstance(image, list):       # nothing, or file paths
        return None
    if clipboard_owner_is_snip() is False:
        return None
    return image
