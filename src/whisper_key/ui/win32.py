# ui/win32.py
# Win32 helpers for the two app windows: find a window, keep the floating bar
# from ever taking focus or showing a taskbar button, move it without
# activating it, theme the main window's title bar, and remember/restore the
# window the user was typing into. Every function is a no-op off Windows.

import ctypes
import logging
import os
import sys

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == 'win32'

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOPMOST = 0x00000008
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1

if IS_WINDOWS:
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_uint]


# The first of `windows` ((hwnd, title, pid) tuples) with this exact title
# that belongs to process `pid`; 0 when there is none.
def pick_window(windows, title: str, pid: int) -> int:
    for hwnd, window_title, window_pid_ in windows:
        if window_title == title and window_pid_ == pid:
            return hwnd
    return 0


# This process's top-level window with this title. Another copy of the app
# (say the --test one) has windows with the same titles, so FindWindow alone
# could return one of those.
def find_window(title: str) -> int:
    if not IS_WINDOWS:
        return 0
    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def visit(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            found.append((hwnd, buf.value, window_pid(hwnd)))
        return True
    user32.EnumWindows(proc(visit), 0)
    return pick_window(found, title, os.getpid())


def foreground() -> int:
    return user32.GetForegroundWindow() if IS_WINDOWS else 0


def window_pid(hwnd: int) -> int:
    if not IS_WINDOWS or not hwnd:
        return 0
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def is_own_window(hwnd: int) -> bool:
    return bool(hwnd) and window_pid(hwnd) == os.getpid()


def restore_foreground(hwnd: int) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False


# The bar: no taskbar button, never activated by clicks or showing.
def make_tool_window(hwnd: int):
    if not IS_WINDOWS or not hwnd:
        return
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    style = (style & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED)


# Move/resize in physical pixels without activating.
def place(hwnd: int, x: int, y: int, w: int, h: int):
    if not IS_WINDOWS or not hwnd:
        return
    user32.SetWindowPos(hwnd, HWND_TOPMOST, int(x), int(y), int(w), int(h), SWP_NOACTIVATE)


def show_no_activate(hwnd: int):
    if IS_WINDOWS and hwnd:
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def hide(hwnd: int):
    if IS_WINDOWS and hwnd:
        user32.ShowWindow(hwnd, SW_HIDE)


def dpi_scale(hwnd: int = 0) -> float:
    if not IS_WINDOWS:
        return 1.0
    try:
        dpi = user32.GetDpiForWindow(hwnd) if hwnd else user32.GetDpiForSystem()
        return (dpi or 96) / 96.0
    except Exception:
        return 1.0


# Work area (screen minus taskbar) of the primary monitor, physical pixels.
def work_area() -> tuple:
    if not IS_WINDOWS:
        return (0, 0, 1920, 1080)
    rect = ctypes.wintypes.RECT()
    SPI_GETWORKAREA = 0x0030
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
    return rect.left, rect.top, rect.right, rect.bottom


# Native title bar in dark or light, to match the app's theme (Windows 11).
def set_dark_title_bar(hwnd: int, dark: bool):
    if not IS_WINDOWS or not hwnd:
        return
    try:
        value = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (and its pre-20H1 id)
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception as e:
        logger.debug(f"Title bar theme not applied: {e}")


# Rounded corners for the bar on Windows 11 (no-op elsewhere).
def round_corners(hwnd: int):
    if not IS_WINDOWS or not hwnd:
        return
    try:
        pref = ctypes.c_int(2)   # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


# WebView2 runtime version from the registry, or None when it is missing.
def webview2_version():
    if not IS_WINDOWS:
        return 'n/a'
    import winreg
    guid = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive, path in ((winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
                       (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
                       (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}")):
        try:
            with winreg.OpenKey(hive, path) as key:
                value = winreg.QueryValueEx(key, 'pv')[0]
                if value and value != '0.0.0.0':
                    return value
        except OSError:
            continue
    return None


# Ctrl+C / console close → callback, from any thread (the main thread is busy
# running the window loop, where Python signal handlers can't run).
def on_console_close(callback):
    if not IS_WINDOWS:
        return
    try:
        import win32api

        def handler(_ctrl_type):
            callback()
            return True
        win32api.SetConsoleCtrlHandler(handler, True)
        on_console_close._handler = handler   # keep a reference
    except Exception as e:
        logger.debug(f"Console close handler not installed: {e}")
