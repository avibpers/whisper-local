# platform/windows/instance_lock.py
# Named-mutex single-instance lock. The handle must stay referenced for the
# process's lifetime — releasing it frees the lock.
# macOS mirror: platform/macos/instance_lock.py (fcntl flock).
import win32api
import win32event

def acquire_lock(app_name: str):
    mutex_name = f"{app_name}_SingleInstance"
    mutex_handle = win32event.CreateMutex(None, True, mutex_name)

    if win32api.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None

    return mutex_handle

def release_lock(handle):
    pass  # Mutex is released automatically when process exits

# Whether another process holds `app_name`'s lock, without taking it: a
# --test copy asks this to stay hands-off while your Koli runs.
def is_held(app_name: str) -> bool:
    import pywintypes
    SYNCHRONIZE = 0x00100000
    try:
        handle = win32event.OpenMutex(SYNCHRONIZE, False, f"{app_name}_SingleInstance")
    except pywintypes.error:
        return False
    win32api.CloseHandle(handle)
    return True
