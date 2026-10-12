# platform/windows/window_focus.py
# Sends a voice command's keys somewhere other than the app in front: to a
# chosen app's window, or to "windows", an invisible Koli window that ignores
# keys, so only Windows' own shortcuts and other programs' global shortcuts
# react. Focus goes back to where the user was. Also lists the open apps for
# the command editor's picker. macOS mirror: platform/macos/window_focus.py.
#
# The desktop is not used as the "no app" target on purpose: it acts on
# Ctrl+Shift+1..4 (icon size) and Delete, F2, Ctrl+Shift+N...

import ctypes
import ctypes.wintypes as wt
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# Own DLL handles, so the argtypes set here can't change how other modules'
# calls through ctypes.windll behave.
_user32 = ctypes.WinDLL('user32', use_last_error=True)
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
_dwmapi = ctypes.WinDLL('dwmapi')

SEND_TO_WINDOWS = 'windows'
FOCUS_SETTLE_SECONDS = 0.08    # the new window takes the keyboard
KEYS_SETTLE_SECONDS = 0.15     # the target reads the keys before focus leaves
FOREGROUND_WAIT_SECONDS = 0.3
# How long after a round trip its focus reports may still arrive (they come
# through the focus watcher's queue, a little late).
FOCUS_REPORT_GRACE_SECONDS = 0.6

GWL_EXSTYLE = -20
GW_OWNER = 4
SW_RESTORE = 9
SW_SHOWNOACTIVATE = 4
WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
LWA_ALPHA = 0x2
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x1
WM_CLOSE = 0x0010
WM_SYSCOMMAND = 0x0112
SC_CLOSE = 0xF060
SC_KEYMENU = 0xF100

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

_user32.GetForegroundWindow.restype = wt.HWND
_user32.SetForegroundWindow.argtypes = [wt.HWND]
_user32.BringWindowToTop.argtypes = [wt.HWND]
_user32.IsIconic.argtypes = [wt.HWND]
_user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_user32.IsWindowVisible.argtypes = [wt.HWND]
_user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
_user32.GetWindow.restype = wt.HWND
_user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.GetWindowTextLengthW.argtypes = [wt.HWND]
_user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_user32.GetWindowThreadProcessId.restype = wt.DWORD
_user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
_user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.DefWindowProcW.restype = ctypes.c_ssize_t
_user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
_user32.CreateWindowExW.restype = wt.HWND
_user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.COLORREF, wt.BYTE, wt.DWORD]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
_kernel32.GetModuleHandleW.restype = wt.HMODULE
_kernel32.OpenProcess.restype = wt.HANDLE
_kernel32.CloseHandle.argtypes = [wt.HANDLE]
_kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
_dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, wt.LPVOID, wt.DWORD]


# ── Open apps ────────────────────────────────────────────────────────────────

def _exe_of(hwnd) -> str:
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value or pid.value == os.getpid():
        return ''
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ''
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wt.DWORD(520)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ''
    finally:
        _kernel32.CloseHandle(handle)


# A window the user would call an app: visible, titled, not a tool or owned
# popup, and not one of the hidden "cloaked" windows modern apps keep around.
def _is_app_window(hwnd) -> bool:
    if not _user32.IsWindowVisible(hwnd) or not _user32.GetWindowTextLengthW(hwnd):
        return False
    if _user32.GetWindow(hwnd, GW_OWNER) or _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
        return False
    cloaked = wt.DWORD()
    _dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return not cloaked.value


# Every app window as (hwnd, exe, title), the most recently used first.
def _app_windows() -> list:
    found = []

    def visit(hwnd, _):
        if _is_app_window(hwnd):
            exe = _exe_of(hwnd)
            if exe:
                length = _user32.GetWindowTextLengthW(hwnd)
                title = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(hwnd, title, length + 1)
                found.append((hwnd, exe, title.value))
        return True
    _user32.EnumWindows(ENUMPROC(visit), 0)
    return found


# The open apps for the picker: [{'exe': 'chrome.exe', 'title': '...'}], one
# per app, the most recently used first. Koli's own windows are left out.
def open_apps() -> list:
    apps = {}
    for _, exe, title in _app_windows():
        apps.setdefault(exe, {'exe': exe, 'title': title})
    return list(apps.values())


def _find_app(exe: str):
    wanted = exe if exe.endswith('.exe') else exe + '.exe'
    return next((hwnd for hwnd, name, _ in _app_windows() if name == wanted), None)


# ── The "no app" window ──────────────────────────────────────────────────────
# A 1x1 fully transparent popup on its own thread with a message loop. Keys
# reaching it do nothing: there is no TranslateMessage (so no characters and
# no beep), and Alt+F4 or the Alt menu key are swallowed.

_sink = {'hwnd': None}
_sink_lock = threading.Lock()


def _sink_proc(hwnd, msg, wparam, lparam):
    if msg == WM_CLOSE:
        return 0
    if msg == WM_SYSCOMMAND and (wparam & 0xFFF0) in (SC_CLOSE, SC_KEYMENU):
        return 0
    return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_sink_proc_ref = WNDPROC(_sink_proc)   # kept alive for as long as the window


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [('style', wt.UINT), ('lpfnWndProc', WNDPROC), ('cbClsExtra', ctypes.c_int),
                ('cbWndExtra', ctypes.c_int), ('hInstance', wt.HINSTANCE), ('hIcon', wt.HICON),
                ('hCursor', wt.HANDLE), ('hbrBackground', wt.HBRUSH), ('lpszMenuName', wt.LPCWSTR),
                ('lpszClassName', wt.LPCWSTR)]


def _run_sink(ready: threading.Event):
    instance = _kernel32.GetModuleHandleW(None)
    window_class = _WNDCLASSW(lpfnWndProc=_sink_proc_ref, hInstance=instance, lpszClassName='KoliKeySink')
    _user32.RegisterClassW(ctypes.byref(window_class))
    # No title: ui/win32.find_window looks Koli's windows up by title. Created
    # hidden and shown without activating: a visible window made from scratch
    # takes the foreground, and the user's typing with it.
    hwnd = _user32.CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_TRANSPARENT, 'KoliKeySink', '',
                                   WS_POPUP, 0, 0, 1, 1, None, None, instance, None)
    if hwnd:
        _user32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA)
        _user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    _sink['hwnd'] = hwnd
    ready.set()
    msg = wt.MSG()
    while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        _user32.DispatchMessageW(ctypes.byref(msg))


def _sink_window():
    with _sink_lock:
        if not _sink['hwnd']:
            ready = threading.Event()
            threading.Thread(target=_run_sink, args=(ready,), name='koli-key-sink', daemon=True).start()
            ready.wait(2.0)
        return _sink['hwnd']


# ── Switching windows ────────────────────────────────────────────────────────

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wt.LONG), ('dy', wt.LONG), ('mouseData', wt.DWORD), ('dwFlags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    _fields_ = [('type', wt.DWORD), ('mi', _MOUSEINPUT)]


# Windows only lets the process that made the last input change the window
# in front. A mouse move of zero makes Koli that process without doing
# anything the user could see.
def _claim_last_input():
    nudge = _INPUT(type=INPUT_MOUSE, mi=_MOUSEINPUT(dwFlags=MOUSEEVENTF_MOVE))
    _user32.SendInput(1, ctypes.byref(nudge), ctypes.sizeof(nudge))


def _wait_foreground(hwnd) -> bool:
    deadline = time.monotonic() + FOREGROUND_WAIT_SECONDS
    while time.monotonic() < deadline:
        if _user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.01)
    return False


def _bring_to_front(hwnd) -> bool:
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
    if _user32.SetForegroundWindow(hwnd) and _wait_foreground(hwnd):
        return True
    _claim_last_input()
    if _user32.SetForegroundWindow(hwnd) and _wait_foreground(hwnd):
        return True
    # Last try: share the keyboard state of the window in front for a moment.
    front_thread = _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), None)
    own_thread = _kernel32.GetCurrentThreadId()
    attached = bool(front_thread) and _user32.AttachThreadInput(own_thread, front_thread, True)
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(own_thread, front_thread, False)
    return _wait_foreground(hwnd)


# ── Koli's own focus moves ───────────────────────────────────────────────────
# Auto-dictation watches focus to start listening in a text field. The hops
# of a round trip are Koli's, not the user's, so it asks here and ignores them.

_moving = {'depth': 0, 'until': 0.0}
_moving_lock = threading.Lock()


def koli_moving_focus() -> bool:
    with _moving_lock:
        return _moving['depth'] > 0 or time.monotonic() < _moving['until']


def _begin_move():
    with _moving_lock:
        _moving['depth'] += 1


def _end_move():
    with _moving_lock:
        _moving['depth'] -= 1
        _moving['until'] = time.monotonic() + FOCUS_REPORT_GRACE_SECONDS


# Run `press` (the key presses) with `target` in front: "windows" or an app's
# .exe ("chrome.exe", or "chrome"). Afterwards the window that was in front
# gets focus back. Raises LookupError, with a message for the user, when the
# app isn't open or Windows refuses the switch; nothing is pressed then.
def run_focused(target: str, press):
    target = str(target or '').strip().lower()
    # Read first: nothing below may change it before it is known.
    previous = _user32.GetForegroundWindow()
    if target == SEND_TO_WINDOWS:
        hwnd = _sink_window()
        if not hwnd:
            raise LookupError("Koli couldn't make its key window")
    else:
        hwnd = _find_app(target)
        if not hwnd:
            raise LookupError(f"{target} isn't open")
    if hwnd == previous:
        time.sleep(FOCUS_SETTLE_SECONDS)
        press()
        return
    _begin_move()
    try:
        if not _bring_to_front(hwnd):
            raise LookupError(f"Windows didn't let Koli switch to {target}")
        time.sleep(FOCUS_SETTLE_SECONDS)
        try:
            press()
        finally:
            if previous:
                time.sleep(KEYS_SETTLE_SECONDS)
                if not _bring_to_front(previous):
                    logger.debug("Couldn't return focus to the previous window")
    finally:
        _end_move()
