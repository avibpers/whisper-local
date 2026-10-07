# platform/windows/media.py
# Other sound on Windows: pause what is playing through the system media
# sessions (the same ones the volume flyout shows: Spotify, browsers, players)
# and read or set the master volume. Falls back to the play/pause media key
# when the media-session packages aren't installed.

import asyncio
import logging

logger = logging.getLogger(__name__)

_PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING


# Pause every session that is playing. Returns the apps that were paused.
def pause_playing() -> list:
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
    except ImportError:
        from . import keyboard
        keyboard.send_key('media_play_pause')
        return ['media key']

    async def pause_all():
        manager = await Manager.request_async()
        paused = []
        for session in manager.get_sessions():
            if int(session.get_playback_info().playback_status) == _PLAYING:
                if await session.try_pause_async():
                    paused.append(session.source_app_user_model_id)
        return paused
    return asyncio.run(pause_all())


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
