# platform/windows/browser_tabs.py
# The tabs of every Chrome, Edge or Brave window, with their media state, read
# from the browser's tab strip through UI Automation. The strip exposes each
# tab as a TabItem whose name is "<title> - <state>" ("Audio playing",
# "Microphone recording", ...), which is how a Meet call is found behind other
# tabs. The caller passes the function that splits a name into title and
# state (meeting/detector.strip_media), so this module needs nothing from the
# feature. Cached for a few seconds, and between full scans only the windows
# that held a tab in a call are probed again: a dozen Chrome windows cost
# about a second to walk. macOS mirror: platform/macos/browser_tabs.py (stub).

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
CACHE_SECONDS = 3.0          # the detector asks once a second
FULL_SCAN_SECONDS = 15.0     # every window again, to catch a call in a new one
UIA_CONTROL_TYPE = 30003
UIA_TAB = 50018
UIA_TABITEM = 50019
TREE_SCOPE_DESCENDANTS = 4
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_monotonic = time.monotonic   # swapped in tests
_cache_lock = threading.Lock()
_cache = {'stamp': 0.0, 'full': 0.0, 'tabs': [], 'hot': set()}
_client = threading.local()


def _reset_cache():
    with _cache_lock:
        _cache.update(stamp=0.0, full=0.0, tabs=[], hot=set())


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
# its tab strips (Tab controls), or under the window when no strip is found.
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


# [{exe, title, media, hwnd}] for the browsers' tabs. `strip(name)` returns
# (title, media state); without it every tab reads as silent.
def tabs(exes=BROWSERS, strip=None) -> list:
    strip = strip or (lambda name: (name, ''))
    now = _monotonic()
    with _cache_lock:
        if now - _cache['stamp'] < CACHE_SECONDS:
            return list(_cache['tabs'])
        full = (now - _cache['full'] >= FULL_SCAN_SECONDS) or not _cache['hot']
        hot = set(_cache['hot'])
    uia = _uia()
    out = []
    if uia is not None:
        windows = _browser_windows(exes)
        targets = windows if full else [(h, e) for h, e in windows if h in hot]
        for hwnd, exe in targets:
            seen = set()
            try:
                for name in _tab_names(uia, hwnd):
                    if name in seen:
                        continue     # the same tab under several strips
                    seen.add(name)
                    title, media = strip(name)
                    out.append({'exe': exe, 'title': title, 'media': media, 'hwnd': hwnd})
            except Exception as e:
                logger.debug(f"Tab strip of {exe} window {hwnd} unreadable: {e}")
    with _cache_lock:
        _cache['tabs'] = out
        _cache['stamp'] = now
        if full:
            _cache['full'] = now
        _cache['hot'] = {t['hwnd'] for t in out if t['media']}
    return list(out)
