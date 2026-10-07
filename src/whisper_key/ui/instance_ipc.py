# ui/instance_ipc.py
# "Launch it again" opens the running app's window instead of starting a second
# copy. The running instance listens on a local named pipe (Windows) / socket
# file (macOS) protected by a random key stored in the user's data folder;
# a new launch sends {"cmd": "open", "page": ...} and exits.

import logging
import os
import secrets
import threading
from multiprocessing.connection import Client, Listener
from pathlib import Path

from ..utils import get_user_app_data_path

logger = logging.getLogger(__name__)


def _address(instance_name: str) -> str:
    user = (os.environ.get('USERNAME') or os.environ.get('USER') or 'user').replace(' ', '_')
    if os.name == 'nt':
        return rf'\\.\pipe\whisper-local-{instance_name}-{user}'
    return str(Path(get_user_app_data_path()) / f'.{instance_name}.sock')


def _key_path(instance_name: str) -> Path:
    return Path(get_user_app_data_path()) / f'.{instance_name}.ipc'


# Ask a running instance to open its window. True when one answered.
def send_open(instance_name: str, page: str = None, timeout: float = 2.0) -> bool:
    key_file = _key_path(instance_name)
    if not key_file.exists():
        return False
    try:
        key = key_file.read_bytes()
    except OSError:
        return False
    result = {}

    def attempt():
        try:
            with Client(_address(instance_name), authkey=key) as conn:
                conn.send({'cmd': 'open', 'page': page})
                result['ok'] = conn.recv() == 'ok'
        except Exception as e:
            result['error'] = e

    th = threading.Thread(target=attempt, daemon=True)
    th.start()
    th.join(timeout)
    return bool(result.get('ok'))


class InstanceListener:
    def __init__(self, instance_name: str, on_open):
        self.instance_name = instance_name
        self.on_open = on_open
        self._listener = None
        self._stop = threading.Event()

    def start(self):
        key = secrets.token_bytes(32)
        key_file = _key_path(self.instance_name)
        try:
            key_file.write_bytes(key)
        except OSError as e:
            logger.debug(f"IPC key not written: {e}")
            return
        address = _address(self.instance_name)
        if os.name != 'nt' and os.path.exists(address):
            os.unlink(address)
        try:
            self._listener = Listener(address, authkey=key)
        except Exception as e:
            logger.debug(f"IPC listener not started: {e}")
            return
        threading.Thread(target=self._serve, daemon=True, name='instance-ipc').start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn = self._listener.accept()
            except Exception:
                if self._stop.is_set():
                    return
                continue
            try:
                msg = conn.recv()
                if isinstance(msg, dict) and msg.get('cmd') == 'open':
                    self.on_open(msg.get('page'))
                conn.send('ok')
            except Exception as e:
                logger.debug(f"IPC message failed: {e}")
            finally:
                conn.close()

    def stop(self):
        self._stop.set()
        try:
            if self._listener:
                self._listener.close()
        except Exception:
            pass
        try:
            _key_path(self.instance_name).unlink()
        except OSError:
            pass
