# platform/windows/idle.py
# How long nobody has touched this PC, and whether it runs on a battery, so
# the night pass (learning/night_pass.py) works only when it disturbs nobody.
# When Windows can't be asked, the answer is the safe one: "just used" and
# "on battery", so the night pass waits.

import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger(__name__)


class _LastInputInfo(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]


class _PowerStatus(ctypes.Structure):
    _fields_ = [('ACLineStatus', ctypes.c_byte), ('BatteryFlag', ctypes.c_byte), ('BatteryLifePercent', ctypes.c_byte),
                ('SystemStatusFlag', ctypes.c_byte), ('BatteryLifeTime', wintypes.DWORD), ('BatteryFullLifeTime', wintypes.DWORD)]


# Seconds since the last key press or mouse move, on any window.
def seconds_idle() -> float:
    try:
        info = _LastInputInfo(cbSize=ctypes.sizeof(_LastInputInfo))
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        # Both are 32-bit millisecond counters: the mask survives the wrap every 49 days.
        return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except (AttributeError, OSError) as e:
        logger.debug(f"Idle time unavailable: {e}")
        return 0.0


# True only when Windows says the mains are unplugged, or can't say at all.
# Status 255 means unknown, which is how many desktop PCs answer.
def on_battery() -> bool:
    try:
        status = _PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return True
        return (status.ACLineStatus & 0xFF) == 0
    except (AttributeError, OSError) as e:
        logger.debug(f"Power status unavailable: {e}")
        return True
