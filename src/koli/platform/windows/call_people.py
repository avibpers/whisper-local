# platform/windows/call_people.py
# Who is in a call, read from the call app's own window through UI Automation.
# Only Discord for now: its window exposes the call ("Becky (direct message),
# Ongoing Call", "In a call (Becky)") and, while the call's conversation is
# open, one tile per person with their mute state. It does not expose who is
# speaking (tested in a live call, 2026-10-20), so voices stay the job of the
# meeting's voiceprints. A read takes one to five seconds: never call it from
# the detector's tick. macOS mirror: platform/macos/call_people.py (stub).

import ctypes
import ctypes.wintypes as wt
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

DISCORD = 'discord.exe'
UIA_NAME = 30005
UIA_CLASS_NAME = 30012
TREE_SCOPE_DESCENDANTS = 4
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# The call in the side list ("unread, Becky (direct message), Ongoing Call,
# Online"), or the label shown under it ("In a call (Becky)").
DM_CALL = re.compile(r'^(?:unread, )?(?P<name>.+?) \(direct message\), Ongoing Call')
CALL_LABEL = re.compile(r'^In a call \((?P<name>[^)]+)\)')
TILE_CLASS = 'focusTarget__'
MUTED = ', Muted'

_client = threading.local()


# ── Pure ─────────────────────────────────────────────────────────────────────

# {'with': name, 'people': [{'name', 'muted'}]} from the (class name, name)
# rows of Discord's window, or {} when no call shows.
def parse_discord(rows: list) -> dict:
    with_name = ''
    people = []
    for cls, name in rows:
        cls, name = cls or '', (name or '').strip()
        if not with_name:
            found = DM_CALL.match(name) or CALL_LABEL.match(name)
            if found:
                with_name = found.group('name').strip()
        if TILE_CLASS in cls and name:
            muted = name.endswith(MUTED)
            person = name[:-len(MUTED)] if muted else name
            if all(p['name'] != person for p in people):
                people.append({'name': person, 'muted': muted})
    if not with_name and not people:
        return {}
    return {'with': with_name, 'people': people}


# ── Windows ──────────────────────────────────────────────────────────────────

def _exe_of(pid: int) -> str:
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ''
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ''
    finally:
        _kernel32.CloseHandle(handle)


# Visible top-level windows of one program that have a title.
def _windows_of(exe: str) -> list:
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def each(hwnd, _):
        if not _user32.IsWindowVisible(hwnd) or not _user32.GetWindowTextLengthW(hwnd):
            return True
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _exe_of(pid.value) == exe:
            found.append(int(hwnd))
        return True
    _user32.EnumWindows(each, 0)
    return found


# One UI Automation client per thread, the way browser_tabs builds its own.
def _uia():
    if getattr(_client, 'uia', None) is not None:
        return _client.uia
    if getattr(_client, 'failed', False):
        return None
    try:
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass
        comtypes.client.GetModule('UIAutomationCore.dll')
        from comtypes.gen import UIAutomationClient as uiac
        _client.uia = comtypes.client.CreateObject(uiac.CUIAutomation, interface=uiac.IUIAutomation)
        return _client.uia
    except Exception as e:
        _client.failed = True
        logger.warning(f"Call people: UI Automation unavailable ({e})")
        return None


# (class name, name) of every element in a window, fetched in one cached call.
def _rows(uia, hwnd: int) -> list:
    request = uia.CreateCacheRequest()
    request.AddProperty(UIA_NAME)
    request.AddProperty(UIA_CLASS_NAME)
    root = uia.ElementFromHandle(hwnd)
    found = root.FindAllBuildCache(TREE_SCOPE_DESCENDANTS, uia.CreateTrueCondition(), request)
    rows = []
    for i in range(found.Length):
        element = found.GetElement(i)
        rows.append((element.CachedClassName or '', element.CachedName or ''))
    return rows


# Who is in the call `exe` is on, or {} (another app, no window, no call,
# or UI Automation failing).
def read(exe: str) -> dict:
    if (exe or '').lower() != DISCORD:
        return {}
    uia = _uia()
    if uia is None:
        return {}
    try:
        rows = []
        for hwnd in _windows_of(DISCORD):
            rows += _rows(uia, hwnd)
        return parse_discord(rows)
    except Exception as e:
        logger.debug(f"Call people: Discord window unreadable ({e})")
        return {}
