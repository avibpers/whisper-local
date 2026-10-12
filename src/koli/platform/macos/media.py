# platform/macos/media.py
# Other sound on macOS: pause and play again with the play/pause media key.
# Reading and setting the master volume isn't implemented yet, so "lower the
# volume while talking" does nothing on macOS (the functions report no volume).

from . import keyboard

MEDIA_KEY = 'media key'


def pause_playing() -> list:
    keyboard.send_key('media_play_pause')
    return [MEDIA_KEY]


# The key can't tell what is playing, so it is pressed again only when
# pause_playing() pressed it.
def resume_playing(apps: list) -> list:
    if MEDIA_KEY not in (apps or []):
        return []
    keyboard.send_key('media_play_pause')
    return [MEDIA_KEY]


def get_master_volume():
    return None


def set_master_volume(value: float):
    pass


# Microphone mute isn't implemented on macOS yet: unknown, and never set.
# Windows mirror mutes the device for every app.
def mic_muted(name: str):
    return None


def set_mic_muted(name: str, muted: bool) -> bool:
    return False
