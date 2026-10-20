# platform/macos/screenshots.py
# Screenshots for meetings are not wired on macOS yet (see the Windows
# mirror: platform/windows/screenshots.py). available() says so, and the
# settings that depend on it stay hidden; everything else is a no-op.

from typing import Optional


def available() -> bool:
    return False


def open_overlay() -> None:
    pass


def clipboard_sequence() -> int:
    return 0


def overlay_open() -> bool:
    return False


def clipboard_owner_is_snip() -> Optional[bool]:
    return None


def grab_snip():
    return None
