# platform/windows/clipboard.py
# Saves the whole clipboard before a paste and puts it back afterwards, every
# format at once, so an image, copied files or rich text survive a dictation
# (the text-only save emptied the clipboard when it held a screenshot). The
# sequence number tells whether anyone copied something meanwhile, so a copy
# the user made during the wait is never overwritten.

import ctypes
import logging
import time
from ctypes import wintypes
from typing import Optional

logger = logging.getLogger(__name__)

# Formats whose data is a GDI handle rather than memory: they can't be copied
# as bytes. Windows makes CF_BITMAP from CF_DIB on demand, so the picture
# still comes back through the DIB.
_HANDLE_FORMATS = {
    2,     # CF_BITMAP
    3,     # CF_METAFILEPICT
    9,     # CF_PALETTE
    14,    # CF_ENHMETAFILE
    0x80,  # CF_OWNERDISPLAY
    0x82,  # CF_DSPBITMAP
    0x83,  # CF_DSPMETAFILEPICT
    0x8E,  # CF_DSPENHMETAFILE
}
# Text formats Windows makes from CF_UNICODETEXT; saving them too would only
# restore copies of the same text.
_CF_UNICODETEXT = 13
_SYNTHESIZED_FROM_UNICODE = {1, 7}  # CF_TEXT, CF_OEMTEXT

# Larger clipboards are left alone: copying hundreds of megabytes twice per
# dictation would stall the paste.
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
GMEM_MOVEABLE = 0x0002
OPEN_TRIES = 10
OPEN_RETRY_S = 0.02

try:
    _user32 = ctypes.WinDLL('user32', use_last_error=True)
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _user32.OpenClipboard.argtypes = (wintypes.HWND,)
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.EnumClipboardFormats.argtypes = (wintypes.UINT,)
    _user32.EnumClipboardFormats.restype = wintypes.UINT
    _user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
    _user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    _user32.GetClipboardData.argtypes = (wintypes.UINT,)
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    _kernel32.GlobalSize.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalSize.restype = ctypes.c_size_t
    _kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalUnlock.restype = wintypes.BOOL
    _kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalFree.restype = wintypes.HGLOBAL
except (AttributeError, OSError):
    _user32 = _kernel32 = None


class Snapshot:
    """What the clipboard held: (format, bytes) pairs; empty when it was empty."""

    def __init__(self, items):
        self.items = items

    @property
    def empty(self) -> bool:
        return not self.items


# Changes whenever anything is put on the clipboard; 0 when unknown.
def sequence() -> int:
    if _user32 is None:
        return 0
    return int(_user32.GetClipboardSequenceNumber())


# Another app may hold the clipboard for a moment; try a few times.
def _open() -> bool:
    for _ in range(OPEN_TRIES):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(OPEN_RETRY_S)
    return False


def _read_handle(handle) -> Optional[bytes]:
    size = _kernel32.GlobalSize(handle)
    if not size:
        return None
    pointer = _kernel32.GlobalLock(handle)
    if not pointer:
        return None
    try:
        return ctypes.string_at(pointer, size)
    finally:
        _kernel32.GlobalUnlock(handle)


# Everything on the clipboard, or None when it can't be read or is too big
# (then the caller doesn't restore, rather than restore half of it).
def snapshot() -> Optional[Snapshot]:
    if _user32 is None or not _open():
        return None
    try:
        has_unicode = bool(_user32.IsClipboardFormatAvailable(_CF_UNICODETEXT))
        items, total, fmt = [], 0, 0
        while True:
            fmt = _user32.EnumClipboardFormats(fmt)
            if not fmt:
                break
            if fmt in _HANDLE_FORMATS or (has_unicode and fmt in _SYNTHESIZED_FROM_UNICODE):
                continue
            handle = _user32.GetClipboardData(fmt)
            if not handle:
                continue
            data = _read_handle(handle)
            if data is None:
                continue
            total += len(data)
            if total > MAX_SNAPSHOT_BYTES:
                logger.info("Clipboard too large to save around the paste; leaving it as the dictation")
                return None
            items.append((fmt, data))
        return Snapshot(items)
    except OSError as e:
        logger.warning(f"Could not read the clipboard: {e}")
        return None
    finally:
        _user32.CloseClipboard()


def _set(fmt: int, data: bytes) -> bool:
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False
    pointer = _kernel32.GlobalLock(handle)
    if not pointer:
        _kernel32.GlobalFree(handle)
        return False
    ctypes.memmove(pointer, data, len(data))
    _kernel32.GlobalUnlock(handle)
    # On success the clipboard owns the memory; on failure it is still ours.
    if not _user32.SetClipboardData(fmt, handle):
        _kernel32.GlobalFree(handle)
        return False
    return True


# Puts a snapshot back; an empty snapshot leaves the clipboard empty, as it was.
def restore(snap: Snapshot) -> bool:
    if _user32 is None or snap is None or not _open():
        return False
    try:
        _user32.EmptyClipboard()
        restored = sum(1 for fmt, data in snap.items if _set(fmt, data))
        return restored == len(snap.items)
    finally:
        _user32.CloseClipboard()
