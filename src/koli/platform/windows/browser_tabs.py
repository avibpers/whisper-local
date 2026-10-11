# platform/windows/browser_tabs.py
# The tabs of every Chrome, Edge or Brave window, with their media state, read
# from the browser's tab strip through UI Automation. The strip exposes each
# tab as a TabItem whose name is "<title> - <state>" ("Audio playing",
# "Microphone in use", ...), which is how a Meet call is found behind other
# tabs. Cached for a few seconds: the detector asks once a second.
# macOS mirror: platform/macos/browser_tabs.py (stub).

import ctypes
import ctypes.wintypes as wt
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

BROWSERS = ('chrome.exe', 'msedge.exe', 'brave.exe')
CACHE_SECONDS = 3.0
UIA_CONTROL_TYPE = 30003
UIA_TAB = 50018
UIA_TABITEM = 50019
TREE_SCOPE_DESCENDANTS = 4
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_cache = (0.0, [])
_cache_lock = threading.Lock()
_client = threading.local()


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


# Visible top-level windows of the browsers, as (hwnd, exe).
def _browser_windows(exes) -> list:
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def each(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _exe_of(pid.value)
        if exe in exes:
            found.append((int(hwnd), exe))
        return True
    _user32.EnumWindows(each, 0)
    return found


# One UI Automation client per thread, the way input_watch builds its probe.
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
        logger.warning(f"Browser tabs: UI Automation unavailable ({e})")
        return None


# The accessible names of the tabs in one browser window: the TabItems under
# its tab strip (a Tab control), or under the window when no strip is found.
def _tab_names(uia, hwnd: int) -> list:
    root = uia.ElementFromHandle(hwnd)
    strip_cond = uia.CreatePropertyCondition(UIA_CONTROL_TYPE, UIA_TAB)
    item_cond = uia.CreatePropertyCondition(UIA_CONTROL_TYPE, UIA_TABITEM)
    names = []
    strips = root.FindAll(TREE_SCOPE_DESCENDANTS, strip_cond)
    scopes = [strips.GetElement(i) for i in range(strips.Length)] or [root]
    for scope in scopes:
        items = scope.FindAll(TREE_SCOPE_DESCENDANTS, item_cond)
        for i in range(items.Length):
            name = items.GetElement(i).CurrentName or ''
            if name:
                names.append(name)
    return names


def tabs(exes=BROWSERS) -> list:
    global _cache
    with _cache_lock:
        stamp, cached = _cache
        if time.monotonic() - stamp < CACHE_SECONDS:
            return list(cached)
    from ...meeting.detector import strip_media
    uia = _uia()
    out = []
    if uia is not None:
        for hwnd, exe in _browser_windows(exes):
            try:
                for name in _tab_names(uia, hwnd):
                    title, media = strip_media(name)
                    out.append({'exe': exe, 'title': title, 'media': media, 'hwnd': hwnd})
            except Exception as e:
                logger.debug(f"Tab strip of {exe} window {hwnd} unreadable: {e}")
    with _cache_lock:
        _cache = (time.monotonic(), out)
    return list(out)
