# llm/credentials.py
# One encrypted file per provider in %APPDATA%\koli\credentials\: the Codex and
# Grok tokens, the Gemini / OpenRouter / OpenAI keys, the optional LM Studio
# token. Nothing here is ever written to user_settings.yaml, so settings
# exports and bug-report bundles never carry a key. Writes go to a temporary
# file and are renamed into place, so a crash can't leave half a token.
# The lock file lets the app and a `--doctor` run share a rotating token.

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_STALE_SECONDS = 45
LOCK_MAX_WAIT_SECONDS = 40


def folder() -> Path:
    from ..utils import get_user_app_data_path
    path = Path(get_user_app_data_path()) / 'credentials'
    path.mkdir(parents=True, exist_ok=True)
    return path


# DPAPI on Windows, pass-through elsewhere (and on Linux CI, where the
# platform package has no modules).
def _cipher():
    try:
        from ..platform import secrets
        return secrets.protect, secrets.unprotect
    except (ImportError, AttributeError):
        return (lambda b: b), (lambda b: b)


def load(name: str) -> dict:
    path = folder() / f"{name}.bin"
    if not path.exists():
        return {}
    try:
        _, unprotect = _cipher()
        return json.loads(unprotect(path.read_bytes()).decode('utf-8'))
    except Exception as e:
        logger.warning(f"Couldn't read the saved {name} login ({e}); treating it as not connected")
        return {}


def save(name: str, data: dict):
    protect, _ = _cipher()
    path = folder() / f"{name}.bin"
    tmp = path.with_suffix('.tmp')
    tmp.write_bytes(protect(json.dumps(data).encode('utf-8')))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def delete(name: str):
    for suffix in ('.bin', '.lock'):
        try:
            (folder() / f"{name}{suffix}").unlink()
        except FileNotFoundError:
            pass


# Holds <name>.lock while refreshing, so two processes never spend the same
# single-use refresh token. A lock older than 45 s belongs to a crashed run and
# is taken over; after 40 s of waiting we go ahead anyway.
@contextmanager
def file_lock(name: str):
    path = folder() / f"{name}.lock"
    deadline = time.time() + LOCK_MAX_WAIT_SECONDS
    held = False
    while not held:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            held = True
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.time() > deadline:
                logger.warning(f"{name}: refresh lock still held after {LOCK_MAX_WAIT_SECONDS}s; going ahead")
                break
            time.sleep(0.5)
    try:
        yield
    finally:
        if held:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
