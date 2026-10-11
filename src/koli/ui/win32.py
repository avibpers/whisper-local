# ui/win32.py
# Win32 helpers for the two app windows: find a window, keep the floating bar
# from ever taking focus or showing a taskbar button, move it without
# activating it, swap the main window's title bar for the page's own, give the
# app its own taskbar identity, and remember/restore the window the user was
# typing into. Every function is a no-op off Windows.

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

# Window messages and hit-test answers used by the custom title bar.
GWLP_WNDPROC = -4
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
SM_CYFRAME = 33
SM_CXPADDEDBORDER = 92

if IS_WINDOWS:
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_uint]
    user32.CallWindowProcW.restype = ctypes.c_ssize_t
    user32.CallWindowProcW.argtypes = [ctypes.c_ssize_t, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM)

    class NCCALCSIZE_PARAMS(ctypes.Structure):
        _fields_ = [('rgrc', wt.RECT * 3), ('lppos', ctypes.c_void_p)]


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


# Work area (screen minus taskbar) in physical pixels: of the primary monitor,
# or with `point` given, of the monitor nearest to that (x, y).
def work_area(point=None) -> tuple:
    if not IS_WINDOWS:
        return (0, 0, 1920, 1080)
    rect = ctypes.wintypes.RECT()
    if point is not None:
        class MONITORINFO(ctypes.Structure):
            _fields_ = [('cbSize', ctypes.c_ulong), ('rcMonitor', wt.RECT), ('rcWork', wt.RECT),
                        ('dwFlags', ctypes.c_ulong)]
        MONITOR_DEFAULTTONEAREST = 2
        monitor = user32.MonitorFromPoint(wt.POINT(int(point[0]), int(point[1])), MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcWork
            return r.left, r.top, r.right, r.bottom
    SPI_GETWORKAREA = 0x0030
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
    return rect.left, rect.top, rect.right, rect.bottom


# A window's rectangle on screen (left, top, right, bottom), physical pixels.
def window_rect(hwnd: int) -> tuple:
    if not IS_WINDOWS or not hwnd:
        return (0, 0, 0, 0)
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
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


# Rounded corners for the bar on Windows 11 (no-op elsewhere). The thin
# resting tab gets the small rounding and no border: the normal rounding is
# never drawn smaller than about 16 px, so on a 6 px window it leaves a ghost
# outline around the tab.
def round_corners(hwnd: int, thin: bool = False):
    if not IS_WINDOWS or not hwnd:
        return
    try:
        dwm = ctypes.windll.dwmapi
        pref = ctypes.c_int(3 if thin else 2)   # DWMWCP_ROUNDSMALL / DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        border = ctypes.c_uint(0xFFFFFFFE if thin else 0xFFFFFFFF)   # DWMWA_COLOR_NONE / _DEFAULT
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
    except Exception:
        pass


# See-through window: 1.0 is solid. The window becomes a layered one the first
# time it is made see-through, and stays layered (WebView2 keeps painting).
def set_opacity(hwnd: int, opacity: float):
    if not IS_WINDOWS or not hwnd:
        return
    WS_EX_LAYERED, LWA_ALPHA = 0x00080000, 0x2
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if not style & WS_EX_LAYERED:
        if opacity >= 1.0:
            return
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
    alpha = max(0, min(255, int(round(opacity * 255))))
    user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)


# Keep a window out of screen sharing and recordings (Windows 10 2004+). It
# stays visible on the monitor. Older capture tools show a black box instead.
def exclude_from_capture(hwnd: int, on: bool) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
    try:
        return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if on else WDA_NONE))
    except Exception:
        return False


# ── Custom title bar ─────────────────────────────────────────────────────────
# The page draws the title bar (tabs plus minimize / maximize / close). Windows
# keeps the real frame: the invisible borders on the left, right and bottom
# still resize, the shadow stays, and snapping works. Only the caption goes.

# Resize answer for a point near the top edge, or None when the point isn't
# there (or the window is maximized, where there is nothing to resize).
def top_edge_hit(x: int, y: int, window_rect: tuple, border: int, maximized: bool):
    left, top, right, _bottom = window_rect
    if maximized or not (top <= y < top + border):
        return None
    corner = border * 2
    if x < left + corner:
        return HTTOPLEFT
    if x >= right - corner:
        return HTTOPRIGHT
    return HTTOP


# Where the page starts once the caption is gone. A maximized window hangs over
# the screen edge by its frame width, so its content starts that much lower.
def caption_free_client_top(window_top: int, maximized: bool, frame: int) -> int:
    return window_top + frame if maximized else window_top


# Resize border thickness for this window's DPI, in physical pixels.
def frame_thickness(hwnd: int) -> int:
    if not IS_WINDOWS:
        return 8
    try:
        dpi = user32.GetDpiForWindow(hwnd) or 96
        return (user32.GetSystemMetricsForDpi(SM_CYFRAME, dpi)
                + user32.GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi))
    except Exception:
        return 8


_title_bar_hooks = {}   # hwnd -> (window procedure, previous procedure); keeps them alive


# Removes the main window's caption and lets the top edge resize. True when the
# page should draw its own title bar; False leaves the normal frame in place.
def remove_title_bar(hwnd: int) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    if hwnd in _title_bar_hooks:
        return True
    previous = user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)
    if not previous:
        return False

    def window_proc(h, msg, wparam, lparam):
        if msg == WM_NCCALCSIZE and wparam:
            params = NCCALCSIZE_PARAMS.from_address(lparam)
            window_top = params.rgrc[0].top
            result = user32.CallWindowProcW(previous, h, msg, wparam, lparam)
            try:
                params.rgrc[0].top = caption_free_client_top(
                    window_top, bool(user32.IsZoomed(h)), frame_thickness(h))
            except Exception:
                pass
            return result
        result = user32.CallWindowProcW(previous, h, msg, wparam, lparam)
        if msg == WM_NCHITTEST:
            try:
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                rect = ctypes.wintypes.RECT()
                user32.GetWindowRect(h, ctypes.byref(rect))
                hit = top_edge_hit(x, y, (rect.left, rect.top, rect.right, rect.bottom),
                                   frame_thickness(h), bool(user32.IsZoomed(h)))
                if hit is not None:
                    return hit
            except Exception:
                pass
        return result

    proc = WNDPROC(window_proc)
    _title_bar_hooks[hwnd] = (proc, previous)
    try:
        user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(proc, ctypes.c_void_p).value)
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        return True
    except Exception as e:
        logger.warning(f"Custom title bar not applied: {e}")
        return False


# Koli's own taskbar identity, so its button shows the Koli icon instead of
# being grouped under Python. Must run before the first window is created.
def set_app_id(app_id: str):
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
    except Exception as e:
        logger.debug(f"App id not set: {e}")


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
