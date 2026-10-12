# platform/macos/session.py
# Whether the Mac is locked (see the Windows mirror: platform/windows/session.py).
# Not built for macOS yet: it always reads as unlocked.


def is_locked() -> bool:
    return False
