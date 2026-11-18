# browser/install.py
# Gets Koli's browser onto this PC: one fixed version of Chrome for Testing
# (Google's own downloadable build), fetched once into <data>/browser/ with
# progress, checked, unpacked, and older versions removed. The version is
# fixed because the Meet join was measured with it; a newer one is a new
# measurement, not an update.

import logging
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from ..utils import get_user_app_data_path

logger = logging.getLogger(__name__)

VERSION = '158.0.8127.36'
URL = f'https://storage.googleapis.com/chrome-for-testing-public/{VERSION}/win64/chrome-win64.zip'
INNER = Path('chrome-win64') / 'chrome.exe'     # where the program sits inside the zip
MARKER = 'INSTALLED'                            # written last: a folder without it is half an install
BLOCK = 256 * 1024
TIMEOUT_S = 30


class InstallError(Exception):
    """The browser could not be put on this PC; the message is for the user."""


class InstallCancelled(Exception):
    """The user stopped the download."""


# ── Where things are ─────────────────────────────────────────────────────────

# Chrome for Testing is downloaded for Windows only so far.
def supported() -> bool:
    return sys.platform == 'win32'


def home() -> Path:
    return Path(get_user_app_data_path()) / 'browser'


# Koli's own browser profile: cookies and settings that are hers, not yours.
def profile_dir(folder: Optional[Path] = None) -> Path:
    return (folder or home()) / 'profile'


def _version_dir(folder: Path) -> Path:
    return folder / f'chrome-{VERSION}'


# The installed program of this version, or None.
def executable(folder: Optional[Path] = None) -> Optional[Path]:
    root = _version_dir(folder or home())
    exe = root / INNER
    return exe if (root / MARKER).is_file() and exe.is_file() else None


# ── Installing ───────────────────────────────────────────────────────────────

# Downloads and unpacks the browser unless it is already there. `reporter`
# is a task reporter (ui/tasks.py): report(fraction, detail) and
# check_cancelled(). Returns the program's path.
def ensure(reporter=None, url: Optional[str] = None, folder: Optional[Path] = None, block: int = BLOCK) -> Path:
    folder = folder or home()
    ready = executable(folder)
    if ready is not None:
        return ready
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / f'chrome-{VERSION}.zip.part'
    unpacked = folder / f'chrome-{VERSION}.unpacking'
    target = _version_dir(folder)
    try:
        _download(url or URL, part, reporter, block)
        _report(reporter, None, "Unpacking Koli's browser…")
        _unpack(part, unpacked)
        shutil.rmtree(target, ignore_errors=True)
        unpacked.rename(target)
        (target / MARKER).write_text(VERSION, encoding='utf-8')
    except InstallError:
        raise
    except (urllib.error.URLError, OSError) as e:
        raise InstallError(f"Koli's browser could not be downloaded: {getattr(e, 'reason', e)}") from e
    finally:
        part.unlink(missing_ok=True)
        shutil.rmtree(unpacked, ignore_errors=True)
    _remove_other_versions(folder)
    logger.info(f"Koli's browser {VERSION} installed in {target}")
    _report(reporter, 1.0, "Koli's browser is ready")
    return target / INNER


def _report(reporter, fraction, detail):
    if reporter is not None:
        reporter.report(fraction, detail)


# Streams the zip to `part`, reporting megabytes and stopping when cancelled.
def _download(url: str, part: Path, reporter, block: int):
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response, open(part, 'wb') as out:
        total = int(response.headers.get('Content-Length') or 0)
        done = 0
        while True:
            if reporter is not None:
                reporter.check_cancelled()
            data = response.read(block)
            if not data:
                break
            out.write(data)
            done += len(data)
            mb, total_mb = done / 1e6, total / 1e6
            # The last few percent are the unpacking.
            _report(reporter, min(0.95, done / total * 0.95) if total else None,
                    f"Downloading Koli's browser: {mb:,.0f} of {total_mb:,.0f} MB" if total
                    else f"Downloading Koli's browser: {mb:,.0f} MB")
    if total and done != total:
        raise InstallError(f"Koli's browser download stopped early ({done:,} of {total:,} bytes). Try again.")


# Checks every file in the zip, then unpacks it; the program must be inside.
def _unpack(part: Path, into: Path):
    shutil.rmtree(into, ignore_errors=True)
    try:
        with zipfile.ZipFile(part) as z:
            damaged = z.testzip()
            if damaged:
                raise InstallError(f"Koli's browser download is damaged ({damaged}). Try again.")
            z.extractall(into)
    except zipfile.BadZipFile as e:
        raise InstallError("Koli's browser download is damaged. Try again.") from e
    if not (into / INNER).is_file():
        raise InstallError("Koli's browser download does not hold the browser. Try again later.")


# Older versions go once the new one is in; her profile folder stays.
def _remove_other_versions(folder: Path):
    keep = _version_dir(folder).name
    for child in folder.glob('chrome-*'):
        if child.is_dir() and child.name != keep:
            shutil.rmtree(child, ignore_errors=True)


__all__ = ['InstallCancelled', 'InstallError', 'URL', 'VERSION', 'ensure', 'executable', 'home', 'profile_dir', 'supported']
