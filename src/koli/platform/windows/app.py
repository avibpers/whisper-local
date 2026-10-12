# platform/windows/app.py
# Windows app-lifecycle bits: single-keypress input (msvcrt) and the thread
# requirements the main loop must respect. Windows needs no special main-thread
# handling, so these are mostly trivial.
# macOS mirror: platform/macos/app.py (NSApplication run loop, which DOES
# require the main thread).
import contextlib
import ctypes
import msvcrt

COINIT_MULTITHREADED = 0x0

# Tk windows each run their own root on a worker thread, which Windows allows.
# macOS mirror is True: Tk there must live on the main thread.
TK_MAIN_THREAD_ONLY = False


def setup():
    pass

def run_event_loop(shutdown_event):
    while not shutdown_event.wait(timeout=0.1):
        pass

def getch():
    return msvcrt.getwch()


# Windows has no main-thread requirement for tray/UI objects, so this just
# calls through. It exists so callers never have to branch on platform.
# macOS mirror marshals onto the main thread — see the note there.
def run_on_ui_thread(fn):
    fn()


# Wrap the whole life of a thread that opens audio streams. WASAPI is COM
# underneath, and a callback stream started from a thread that never joined
# COM fails with "Unanticipated host error [-9999]"; PortAudio doesn't join
# for us. The recorder then fell back to the Windows default microphone, so
# every session quietly recorded from the wrong one. Joins the multithreaded
# apartment; a thread already in COM (S_FALSE, or a different apartment) is
# left as it is. macOS mirror is a no-op.
@contextlib.contextmanager
def audio_thread():
    result = ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    try:
        yield
    finally:
        if result >= 0:  # S_OK or S_FALSE: each success needs its own uninitialize
            ctypes.windll.ole32.CoUninitialize()
