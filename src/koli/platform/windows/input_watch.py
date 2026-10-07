# platform/windows/input_watch.py
# Reports two things to auto_dictation.py: "a text field got focus" and "the
# user is typing". A hook thread pumps Windows messages for a focus WinEvent
# hook and a low-level keyboard hook; a worker thread asks UI Automation what
# the focused element is (never from inside the hook callback, which Windows
# forbids) and calls back. Nothing here knows about Koli state.
# macOS mirror: platform/macos/input_watch.py (no-op for now).
import ctypes
import ctypes.wintypes as wt
import logging
import queue
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
EVENT_OBJECT_FOCUS = 0x8005
WINEVENT_OUTOFCONTEXT = 0x0000
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_QUIT = 0x0012
LLKHF_INJECTED = 0x10
LLKHF_ALTDOWN = 0x20
GA_ROOT = 2
VK_CONTROL, VK_LWIN, VK_RWIN = 0x11, 0x5B, 0x5C

# UI Automation ids (UIAutomationClient.h)
UIA_COMBOBOX = 50003
UIA_EDIT = 50004
UIA_DOCUMENT = 50030
UIA_VALUE_IS_READONLY = 30046

FOCUS_SETTLE_SECONDS = 0.12   # focus storms (a window opening) collapse to the last element
TYPING_REPEAT_SECONDS = 0.25  # one typing report per burst, not per key

# Keys that are never "typing": modifiers, navigation, function and lock keys,
# media keys, Escape (the cancel hotkey handles it) and the menu key.
_MODIFIER_VKS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, VK_LWIN, VK_RWIN}
_NOT_TYPING_VKS = _MODIFIER_VKS | {
    0x13, 0x14, 0x1B, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2F,
    0x5D, 0x90, 0x91,
} | set(range(0x70, 0x88)) | set(range(0xA6, 0xB8))


# Pure decision, importable on any OS for the tests. `flags` is the hook's
# KBDLLHOOKSTRUCT.flags; Ctrl and Win are read separately since the struct
# only carries Alt.
def classify_key(vk: int, flags: int, ctrl_down: bool = False, win_down: bool = False) -> bool:
    if flags & LLKHF_INJECTED:      # Koli's own paste, Enter and live typing
        return False
    if flags & LLKHF_ALTDOWN or ctrl_down or win_down:
        return False                # shortcuts and hotkeys
    if vk in _NOT_TYPING_VKS:
        return False
    return True


# ── Win32 bindings (resolved lazily so importing never touches user32) ───────
_user32 = None
_kernel32 = None
_WinEventProc = None
_KeyboardProc = None


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode', wt.DWORD), ('scanCode', wt.DWORD), ('flags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ctypes.c_void_p)]


def _bind():
    global _user32, _kernel32, _WinEventProc, _KeyboardProc
    if _user32 is not None:
        return
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _WinEventProc = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)
    _KeyboardProc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
    _user32.SetWinEventHook.restype = wt.HANDLE
    _user32.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE, _WinEventProc, wt.DWORD, wt.DWORD, wt.DWORD]
    _user32.UnhookWinEvent.argtypes = [wt.HANDLE]
    _user32.SetWindowsHookExW.restype = wt.HHOOK
    _user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _KeyboardProc, wt.HINSTANCE, wt.DWORD]
    _user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
    _user32.CallNextHookEx.restype = ctypes.c_ssize_t
    _user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
    _user32.GetAncestor.restype = wt.HWND
    _user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    _user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
    _kernel32.GetModuleHandleW.restype = wt.HMODULE
    _kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]


def _root_pid(hwnd: int) -> int:
    try:
        root = _user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


# ── UI Automation probe ──────────────────────────────────────────────────────
class _Probe:
    # One COM client per worker thread. comtypes generates the UIAutomation
    # wrapper module on first use; if that fails (read-only install) every
    # probe reports "not editable" with the reason in the log.
    def __init__(self):
        self.uia = None
        self.error = ''
        try:
            import comtypes
            import comtypes.client
            # comtypes initialises COM on whichever thread first imports it
            # (single-threaded apartment). On any other thread this call is
            # needed; where it already happened it fails harmlessly.
            try:
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            except OSError:
                pass
            comtypes.client.GetModule('UIAutomationCore.dll')
            from comtypes.gen import UIAutomationClient as uiac
            self.uia = comtypes.client.CreateObject(uiac.CUIAutomation, interface=uiac.IUIAutomation)
        except Exception as e:
            self.error = str(e)
            logger.warning(f"Auto-dictation: UI Automation unavailable, fields will not be detected ({e})")

    def focused(self, hwnd: int) -> dict:
        info = {'id': (hwnd,), 'editable': False, 'password': False, 'hwnd': hwnd,
                'root_pid': _root_pid(hwnd), 'exe': _foreground_exe(), 'why': ''}
        if self.uia is None:
            info['why'] = f'UI Automation unavailable: {self.error}'
            return info
        try:
            el = self.uia.GetFocusedElement()
            if el is None:
                info['why'] = 'no focused element'
                return info
            try:
                runtime_id = tuple(int(x) for x in (el.GetRuntimeId() or ()))
                if runtime_id:
                    info['id'] = runtime_id
            except Exception:
                pass
            native = int(el.CurrentNativeWindowHandle or 0)
            if native:
                info['hwnd'] = native
                info['root_pid'] = _root_pid(native)
            control_type = int(el.CurrentControlType)
            info['password'] = bool(el.CurrentIsPassword)
            if not el.CurrentIsEnabled:
                info['why'] = 'disabled'
                return info
            read_only = _variant_bool(el, UIA_VALUE_IS_READONLY)
            if control_type in (UIA_EDIT, UIA_DOCUMENT):
                info['editable'] = read_only is not True
            elif control_type == UIA_COMBOBOX:
                info['editable'] = read_only is False
            if not info['editable']:
                info['why'] = f'control type {control_type}' + (', read-only' if read_only else '')
        except Exception as e:
            info['why'] = f'probe failed: {e}'
        return info


def _variant_bool(element, property_id: int):
    try:
        value = element.GetCurrentPropertyValue(property_id)
    except Exception:
        return None
    if value is None or value == '':
        return None
    return bool(value)


def _foreground_exe() -> str:
    try:
        from .foreground import get_foreground_app
        return (get_foreground_app() or {}).get('exe', '') or ''
    except Exception:
        return ''


# ── The watcher ──────────────────────────────────────────────────────────────
class _Watcher:
    def __init__(self, on_focus: Callable[[dict], None], on_typing: Callable[[], None]):
        self.on_focus = on_focus
        self.on_typing = on_typing
        self._events: "queue.Queue" = queue.Queue()
        self._hook_thread: Optional[threading.Thread] = None
        self._worker: Optional[threading.Thread] = None
        self._hook_thread_id = 0
        self._ready = threading.Event()
        self._failed = ''
        self._stopping = False
        self._last_typing = 0.0
        # The callbacks must outlive the hooks; Windows holds raw pointers.
        self._focus_proc = None
        self._key_proc = None

    def start(self) -> bool:
        _bind()
        self._hook_thread = threading.Thread(target=self._pump, name='auto-dictation-hooks', daemon=True)
        self._hook_thread.start()
        self._worker = threading.Thread(target=self._work, name='auto-dictation-probe', daemon=True)
        self._worker.start()
        self._ready.wait(3.0)
        if self._failed:
            self.stop()
            raise RuntimeError(self._failed)
        return self._ready.is_set()

    def stop(self):
        self._stopping = True
        if self._hook_thread_id:
            _user32.PostThreadMessageW(self._hook_thread_id, WM_QUIT, 0, 0)
        self._events.put(('quit',))
        for t in (self._hook_thread, self._worker):
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(2.0)

    # Hook thread: install both hooks, pump until WM_QUIT.
    def _pump(self):
        self._hook_thread_id = _kernel32.GetCurrentThreadId()
        focus_hook = key_hook = None
        try:
            self._focus_proc = _WinEventProc(self._on_win_event)
            focus_hook = _user32.SetWinEventHook(EVENT_OBJECT_FOCUS, EVENT_OBJECT_FOCUS, None,
                                                 self._focus_proc, 0, 0, WINEVENT_OUTOFCONTEXT)
            if not focus_hook:
                raise OSError('SetWinEventHook failed')
            self._key_proc = _KeyboardProc(self._on_key)
            key_hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._key_proc,
                                                 _kernel32.GetModuleHandleW(None), 0)
            if not key_hook:
                raise OSError(f'keyboard hook failed ({ctypes.get_last_error()})')
            self._ready.set()
            msg = wt.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            self._failed = str(e)
            self._ready.set()
        finally:
            if key_hook:
                _user32.UnhookWindowsHookEx(key_hook)
            if focus_hook:
                _user32.UnhookWinEvent(focus_hook)

    def _on_win_event(self, hook, event, hwnd, id_object, id_child, thread_id, event_time):
        if event == EVENT_OBJECT_FOCUS and hwnd:
            self._events.put(('focus', int(hwnd)))

    def _on_key(self, code, wparam, lparam):
        try:
            if code >= 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kb = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                ctrl = bool(_user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
                win = bool((_user32.GetAsyncKeyState(VK_LWIN) | _user32.GetAsyncKeyState(VK_RWIN)) & 0x8000)
                if classify_key(kb.vkCode, kb.flags, ctrl, win):
                    now = time.monotonic()
                    if now - self._last_typing >= TYPING_REPEAT_SECONDS:
                        self._last_typing = now
                        self._events.put(('typing',))
        except Exception:
            pass
        return _user32.CallNextHookEx(None, code, wparam, lparam)

    # Worker thread: let a focus storm settle, probe the last element, report.
    def _work(self):
        probe = _Probe()
        while not self._stopping:
            item = self._events.get()
            if item[0] == 'quit':
                break
            if item[0] == 'typing':
                self._call(self.on_typing)
                continue
            hwnd = item[1]
            deadline = time.monotonic() + FOCUS_SETTLE_SECONDS
            quit_seen = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    later = self._events.get(timeout=remaining)
                except queue.Empty:
                    break
                if later[0] == 'quit':
                    quit_seen = True
                    break
                if later[0] == 'typing':
                    self._call(self.on_typing)
                elif later[0] == 'focus':
                    hwnd = later[1]
            if quit_seen:
                break
            info = probe.focused(hwnd)
            if not info['editable']:
                logger.debug(f"Auto-dictation: focus in {info['exe'] or '?'} is not a text field ({info['why']})")
            self._call(self.on_focus, info)

    def _call(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:
            logger.debug(f"Auto-dictation callback failed: {e}")


_watcher: Optional[_Watcher] = None
_lock = threading.Lock()


# Platform contract: start(on_focus, on_typing) -> bool, stop(), is_running().
def start(on_focus: Callable[[dict], None], on_typing: Callable[[], None]) -> bool:
    global _watcher
    with _lock:
        if _watcher is not None:
            return True
        watcher = _Watcher(on_focus, on_typing)
        ok = watcher.start()
        if ok:
            _watcher = watcher
        return ok


def stop() -> None:
    global _watcher
    with _lock:
        watcher, _watcher = _watcher, None
    if watcher is not None:
        watcher.stop()


def is_running() -> bool:
    return _watcher is not None
