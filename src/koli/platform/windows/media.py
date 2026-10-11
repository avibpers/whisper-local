# platform/windows/media.py
# Other sound on Windows: pause what is playing through the system media
# sessions (the same ones the volume flyout shows: Spotify, browsers, players),
# play those again afterwards, and read or set the master volume. Falls back to
# the play/pause media key when the media-session packages aren't installed.

import asyncio
import logging

logger = logging.getLogger(__name__)

# GlobalSystemMediaTransportControlsSessionPlaybackStatus
_PLAYING = 4
_PAUSED = 5

MEDIA_KEY = 'media key'   # what pause_playing() reports when it pressed the key


def _session_manager():
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
        return Manager
    except ImportError:
        return None


def _press_play_pause():
    from . import keyboard
    keyboard.send_key('media_play_pause')


# Pause every session that is playing. Returns the apps that were paused.
def pause_playing() -> list:
    Manager = _session_manager()
    if Manager is None:
        _press_play_pause()
        return [MEDIA_KEY]

    async def pause_all():
        manager = await Manager.request_async()
        paused = []
        for session in manager.get_sessions():
            if int(session.get_playback_info().playback_status) == _PLAYING:
                if await session.try_pause_async():
                    paused.append(session.source_app_user_model_id)
        return paused
    return asyncio.run(pause_all())


# Play again the apps pause_playing() paused. One the user already started
# again, or stopped for good (closed), is left alone. Returns the apps resumed.
def resume_playing(apps: list) -> list:
    if not apps:
        return []
    if MEDIA_KEY in apps:
        _press_play_pause()
        return [MEDIA_KEY]
    Manager = _session_manager()
    if Manager is None:
        return []

    async def resume_all():
        manager = await Manager.request_async()
        resumed = []
        for session in manager.get_sessions():
            app = session.source_app_user_model_id
            if app in apps and int(session.get_playback_info().playback_status) == _PAUSED:
                if await session.try_play_async():
                    resumed.append(app)
        return resumed
    return asyncio.run(resume_all())


# The default speakers' endpoint volume. COM has to be set up on each thread
# that uses it, and the hotkey thread is not the one that imported comtypes.
def _endpoint():
    import comtypes
    from pycaw.pycaw import AudioUtilities
    comtypes.CoInitialize()
    return AudioUtilities.GetSpeakers().EndpointVolume


# Master volume 0..1, or None when it can't be read (no speakers, no pycaw).
def get_master_volume():
    try:
        return float(_endpoint().GetMasterVolumeLevelScalar())
    except ImportError:
        return None
    except Exception as e:
        logger.debug(f"Volume not readable: {e}")
        return None


def set_master_volume(value: float):
    _endpoint().SetMasterVolumeLevelScalar(max(0.0, min(1.0, float(value))), None)
