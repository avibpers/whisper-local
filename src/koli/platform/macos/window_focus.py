# platform/macos/window_focus.py
# Sending a voice command's keys to another app or to "no app" (see the
# Windows mirror: platform/windows/window_focus.py). Not built for macOS yet:
# the picker shows no apps and a targeted command says why it didn't run.

SEND_TO_WINDOWS = 'windows'


def open_apps() -> list:
    return []


# Koli never moves focus itself on macOS, so auto-dictation has nothing to ignore.
def koli_moving_focus() -> bool:
    return False


def find_window(name: str):
    return None


def open_window(name: str) -> None:
    raise LookupError("opening an app by name works on Windows only for now")


def run_focused(target: str, press):
    raise LookupError("sending keys to another app works on Windows only for now")
