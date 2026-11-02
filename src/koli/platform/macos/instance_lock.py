# platform/macos/instance_lock.py
# Single-instance lock via an fcntl flock on a file in the config directory. The
# file object must stay referenced for the lifetime of the process.
# Windows mirror: platform/windows/instance_lock.py (named mutex).
import fcntl
import os
from pathlib import Path

_lock_file = None

def acquire_lock(app_name: str):
    global _lock_file
    lock_path = Path.home() / f".{app_name}.lock"

    try:
        _lock_file = open(lock_path, 'w')
        fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file.write(str(os.getpid()))
        _lock_file.flush()
        return _lock_file
    except (IOError, OSError):
        if _lock_file:
            _lock_file.close()
            _lock_file = None
        return None

def release_lock(handle):
    global _lock_file
    if handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        except:
            pass
    _lock_file = None

# Whether another process holds `app_name`'s lock, without taking it: a
# --test copy asks this to stay hands-off while your Koli runs.
def is_held(app_name: str) -> bool:
    lock_path = Path.home() / f".{app_name}.lock"
    if not lock_path.exists():
        return False
    try:
        with open(lock_path, 'a') as probe:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return False
    except (IOError, OSError):
        return True
