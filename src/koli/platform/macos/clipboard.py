# platform/macos/clipboard.py
# Saves the clipboard before a paste and puts it back afterwards. For now
# text only, through pyperclip; a clipboard with no text is not restored, so
# the save can never empty it. The full pasteboard (images, files) and its
# change count are for the macOS port.

import logging
from typing import Optional

import pyperclip

logger = logging.getLogger(__name__)


class Snapshot:
    """The clipboard's text; mirrors the Windows snapshot's shape."""

    def __init__(self, items):
        self.items = items

    @property
    def empty(self) -> bool:
        return not self.items


# 0 means unknown: callers then restore without the "copied meanwhile" check.
def sequence() -> int:
    return 0


def snapshot() -> Optional[Snapshot]:
    try:
        text = pyperclip.paste()
    except Exception as e:
        logger.warning(f"Could not read the clipboard: {e}")
        return None
    # No text may still mean an image we can't read here: don't restore.
    return Snapshot([('text', text)]) if text else None


def restore(snap: Snapshot) -> bool:
    if snap is None or snap.empty:
        return False
    try:
        pyperclip.copy(snap.items[0][1])
        return True
    except Exception as e:
        logger.warning(f"Could not restore the clipboard: {e}")
        return False
