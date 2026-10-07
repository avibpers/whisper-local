# platform/macos/media.py
# Other sound on macOS: pause with the play/pause media key. Reading and
# setting the master volume isn't implemented yet, so "lower the volume while
# talking" does nothing on macOS (the functions report no volume).

from . import keyboard


def pause_playing() -> list:
    keyboard.send_key('media_play_pause')
    return ['media key']


def get_master_volume():
    return None


def set_master_volume(value: float):
    pass
