# platform/macos/input_watch.py
# No-op stub: auto-dictation (start listening when a text field gets focus)
# and the delayed send's "typing cancels it" are Windows-only for now. The
# macOS version will use an AXObserver on the frontmost app's
# kAXFocusedUIElementChanged notification for focus and the existing
# CGEventTap in hotkeys.py for typing.
# Windows mirror: platform/windows/input_watch.py (the real implementation).


def classify_key(vk: int, flags: int, ctrl_down: bool = False, win_down: bool = False) -> bool:
    return False


# Returns a listener token; 0 means "not available".
def start(on_focus, on_typing) -> int:
    return 0


def stop(token: int = 0) -> None:
    pass


def is_running() -> bool:
    return False
