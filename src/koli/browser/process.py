# browser/process.py
# Starts and stops Koli's browser: its own profile folder, a control port only
# this PC can reach, no window. It can never outlive Koli: the process joins a
# job that Windows ends with her, and a browser left by a crashed run is found
# by its profile folder and ended before the next start.

import json
import logging
import socket
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

DEFAULT_SIZE = (1100, 760)   # the page size the Meet join was measured at; smaller changes Meet's layout
STOP_WAIT_S = 2.0


# ── The command line ─────────────────────────────────────────────────────────

# Everything after the program's path. Each flag is one the Meet join was
# measured with (docs/design/meet-participant.md); change them only with a
# new measurement.
def launch_args(profile_dir: Path, port: int, headless: bool = True, size: tuple = DEFAULT_SIZE,
                extra_args: Iterable[str] = ()) -> list:
    if not port:
        # Port 0 lets Chrome choose, and is said to mark the browser as automated.
        raise ValueError("The browser needs a control port chosen by Koli")
    args = [
        f'--user-data-dir={profile_dir}',
        f'--remote-debugging-port={port}',
        '--no-first-run',
        '--no-default-browser-check',
        '--mute-audio',                          # the meeting's loopback capture must not hear the call twice
        '--lang=en-US',                          # Meet's labels are read in English
        '--use-fake-device-for-media-stream',    # the real camera and microphone can never be opened
        f'--window-size={int(size[0])},{int(size[1])}',
    ]
    if headless:
        args.append('--headless=new')
    args += list(extra_args)
    args.append('about:blank')
    return args


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


# ── The process ──────────────────────────────────────────────────────────────

class BrowserProcess:
    """One running browser. `start` returns the port to connect to."""

    def __init__(self, popen: Callable = subprocess.Popen, attach_job: Optional[Callable[[int], bool]] = None):
        self._popen = popen
        self._attach_job = attach_job
        self._process = None
        self.port = 0

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self._process is not None else None

    def start(self, exe: Path, profile_dir: Path, headless: bool = True, size: tuple = DEFAULT_SIZE,
              extra_args: Iterable[str] = ()) -> int:
        profile_dir.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        self._process = self._popen([str(exe), *launch_args(profile_dir, self.port, headless, size, extra_args)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=flags)
        attach = self._attach_job or _attach_job
        if not attach(self._process.pid):
            logger.info("Koli's browser is not tied to the app (no job object here); it is closed on shutdown only")
        logger.info(f"Koli's browser started (pid {self._process.pid}, control port {self.port})")
        return self.port

    # Asks nicely through `close_browser` (a Browser.close call), then ends
    # the process and its children.
    def stop(self, close_browser: Optional[Callable[[], None]] = None):
        process, self._process = self._process, None
        if process is None:
            return
        if close_browser is not None and process.poll() is None:
            try:
                close_browser()
                process.wait(timeout=STOP_WAIT_S)
            except Exception:
                pass
        if process.poll() is None:
            _kill_tree(process.pid)
            try:
                process.wait(timeout=STOP_WAIT_S)
            except Exception:
                pass
        logger.info("Koli's browser stopped")


def _attach_job(pid: int) -> bool:
    try:
        from ..platform import child_jobs
        return child_jobs.attach(pid)
    except Exception as e:
        logger.debug(f"Job object unavailable: {e}")
        return False


# ── Leftovers ────────────────────────────────────────────────────────────────

def _no_window() -> int:
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _kill_tree(pid: int):
    if sys.platform == 'win32':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True,
                       creationflags=_no_window(), stdin=subprocess.DEVNULL, timeout=10)
    else:
        subprocess.run(['kill', '-9', str(pid)], capture_output=True, timeout=10)


# Every chrome.exe with its command line: [{'pid', 'command'}].
def _list_chrome() -> list:
    if sys.platform != 'win32':
        return []
    script = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
              "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    done = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True,
                          creationflags=_no_window(), stdin=subprocess.DEVNULL, timeout=20)
    text = done.stdout.decode('utf-8', 'replace').strip()
    if not text:
        return []
    rows = json.loads(text)
    rows = rows if isinstance(rows, list) else [rows]
    return [{'pid': int(r.get('ProcessId') or 0), 'command': str(r.get('CommandLine') or '')} for r in rows]


def _same_path(text: str) -> str:
    return text.replace('\\', '/').lower()


# Ends browsers still running on Koli's profile from a run that crashed, so
# nobody stays in a call with no Koli behind them. Only main processes are
# named: their children go with them. Returns how many were ended.
def kill_leftovers(profile_dir: Path, list_processes: Optional[Callable[[], list]] = None,
                   kill: Optional[Callable[[int], None]] = None) -> int:
    try:
        running = (list_processes or _list_chrome)()
    except Exception as e:
        logger.debug(f"Could not list browsers: {e}")
        return 0
    needle = f'--user-data-dir={_same_path(str(profile_dir))}'
    count = 0
    for row in running:
        command = _same_path(row.get('command') or '')
        if needle in command and '--type=' not in command and row.get('pid'):
            try:
                (kill or _kill_tree)(row['pid'])
                count += 1
            except Exception as e:
                logger.debug(f"Could not end leftover browser {row['pid']}: {e}")
    if count:
        logger.info(f"Ended {count} browser(s) left from an earlier run of Koli")
    return count


__all__ = ['BrowserProcess', 'DEFAULT_SIZE', 'free_port', 'kill_leftovers', 'launch_args']
