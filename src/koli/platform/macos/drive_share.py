# platform/macos/drive_share.py
# Drive for desktop's Share window is Windows-only here for now (see the
# Windows mirror: platform/windows/drive_share.py); Share opens the doc in the
# browser instead.

from typing import Optional


def available() -> bool:
    return False


def local_root(email: str) -> Optional[object]:
    return None


def show_share(path) -> bool:
    return False
