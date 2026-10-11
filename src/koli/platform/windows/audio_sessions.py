# platform/windows/audio_sessions.py
# Which programs have an audio session open right now (the entries the volume
# mixer shows) and whether each is active, through pycaw. A call keeps its
# session active for its whole length, even in silence, which is what meeting
# detection keys on. macOS mirror: platform/macos/audio_sessions.py (stub).

import logging
import threading

logger = logging.getLogger(__name__)

_com_ready = threading.local()


# COM has to be set up once per thread; the detector thread is not the one
# that imported comtypes.
def _init_com():
    if getattr(_com_ready, 'done', False):
        return
    try:
        import comtypes
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass
    except Exception:
        pass
    _com_ready.done = True


# [{pid, exe, state}] for every session; state 1 is active (see
# AudioSessionState: 0 inactive, 1 active, 2 expired).
def active() -> list:
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception as e:
        logger.debug(f"pycaw unavailable: {e}")
        return []
    _init_com()
    out = []
    try:
        for session in AudioUtilities.GetAllSessions():
            try:
                process = session.Process
                exe = process.name().lower() if process else ''
                pid = process.pid if process else 0
                out.append({'pid': int(pid), 'exe': exe, 'state': int(session.State)})
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"Audio session probe failed: {e}")
    return out
