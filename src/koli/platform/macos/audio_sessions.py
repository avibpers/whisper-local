# platform/macos/audio_sessions.py
# Not implemented on macOS yet: meeting detection reports no sources, so
# meetings start by hand. Mirrors platform/windows/audio_sessions.py.


def active(devices_fn=None) -> list:
    return []
