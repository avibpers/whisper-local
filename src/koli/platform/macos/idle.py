# platform/macos/idle.py
# Mirror of platform/windows/idle.py. Not built for macOS yet: the Mac always
# counts as just used and on battery, so the night pass never runs there.


def seconds_idle() -> float:
    return 0.0


def on_battery() -> bool:
    return True
