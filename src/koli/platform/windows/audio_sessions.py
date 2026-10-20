# platform/windows/audio_sessions.py
# Which programs have an audio session open right now (the entries the volume
# mixer shows), on which device, and whether each is active, through pycaw.
# Output sessions say who plays sound; microphone (capture) sessions say who
# holds the mic, which is what a call is: Discord opens one the second you
# join, before anyone speaks, and keeps it for the whole call. Every active
# device is read, not only the Windows default: a call on the headset must be
# seen when the monitor is the default. macOS mirror:
# platform/macos/audio_sessions.py (stub).

import logging
import threading

logger = logging.getLogger(__name__)

_com_ready = threading.local()
SESSION_ACTIVE = 1


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


# (name, id, sessions) for every active device of one direction: output
# (`capture=False`) or microphone (`capture=True`).
def _devices(capture: bool = False):
    from pycaw.pycaw import DEVICE_STATE, AudioSession, AudioUtilities, EDataFlow, IAudioSessionControl2
    flow = EDataFlow.eCapture.value if capture else EDataFlow.eRender.value
    for device in AudioUtilities.GetAllDevices(flow, DEVICE_STATE.ACTIVE.value):
        sessions = []
        try:
            enumerator = device.AudioSessionManager.GetSessionEnumerator()
            for i in range(enumerator.GetCount()):
                control = enumerator.GetSession(i)
                if control is not None:
                    sessions.append(AudioSession(control.QueryInterface(IAudioSessionControl2)))
        except Exception as e:
            logger.debug(f"Sessions of {getattr(device, 'FriendlyName', '?')} unreadable: {e}")
        yield getattr(device, 'FriendlyName', ''), getattr(device, 'id', ''), sessions


# [{pid, exe, state, device, device_id, flow}] for every session that belongs
# to a program; state 1 is active (AudioSessionState: 0 inactive, 1 active,
# 2 expired), flow is 'render' (output) or 'capture' (microphone).
# `devices_fn` / `capture_devices_fn` are for tests; a test that passes only
# the first reads no microphones.
def active(devices_fn=None, capture_devices_fn=None) -> list:
    if devices_fn is None:
        try:
            import pycaw.pycaw  # noqa: F401
        except Exception as e:
            logger.debug(f"pycaw unavailable: {e}")
            return []
        _init_com()
        devices_fn = _devices
        capture_devices_fn = capture_devices_fn or (lambda: _devices(capture=True))
    out = _sessions_of(devices_fn, 'render')
    if capture_devices_fn is not None:
        out += _sessions_of(capture_devices_fn, 'capture')
    return out


# The program sessions of one direction's devices; a device or session that
# can't be read is skipped, so one broken driver doesn't hide the rest.
def _sessions_of(devices_fn, flow: str) -> list:
    out = []
    try:
        for name, device_id, sessions in devices_fn():
            for session in sessions:
                try:
                    process = session.Process
                    if process is None:
                        continue
                    out.append({'pid': int(process.pid), 'exe': process.name().lower(), 'state': int(session.State),
                                'device': str(name), 'device_id': str(device_id), 'flow': flow})
                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"Audio session probe ({flow}) failed: {e}")
    return out
