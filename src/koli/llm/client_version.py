# llm/client_version.py
# The CLI version Koli presents to the Codex and Grok subscription backends.
# They fingerprint the official command-line tools and refuse an old one with
# HTTP 426, which no login fixes. So the version sent is the highest of: the
# floor shipped here, the latest release on npm (checked once a day), and any
# minimum a 426 named. Kept in %APPDATA%\koli\ai-client-versions.json.
# Ported from BaileysWhatsAPP services/ai/client-version.ts.

import json
import logging
import re
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# id → (npm package, floor)
TRACKS = {
    'codex': ('@openai/codex', '0.162.1'),
    'grok': ('@xai-official/grok', '1.0.52'),
}
STALE_SECONDS = 24 * 3600
ERROR_RETRY_SECONDS = 3600

_lock = threading.Lock()
_refreshing = set()


def parse(version) -> tuple:
    m = re.match(r'^\s*v?(\d+)\.(\d+)\.(\d+)', str(version or ''))
    return tuple(int(x) for x in m.groups()) if m else None


def highest(*versions) -> str:
    valid = [str(v).strip() for v in versions if parse(v)]
    return max(valid, key=parse) if valid else ''


# "update to version 1.0.13 or later" → "1.0.13".
def minimum_from_426(body: str) -> str:
    m = re.search(r'version\s+v?(\d+\.\d+\.\d+)\s+or\s+later', body or '', re.I)
    return m.group(1) if m else ''


def _file() -> Path:
    from ..utils import get_user_app_data_path
    return Path(get_user_app_data_path()) / 'ai-client-versions.json'


def _read() -> dict:
    try:
        return json.loads(_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _write(state: dict):
    try:
        _file().write_text(json.dumps(state, indent=2), encoding='utf-8')
    except OSError as e:
        logger.debug(f"Couldn't save AI client versions: {e}")


# The version to send now. A stale record starts a background npm check; this
# call never waits for the network.
def current(track: str) -> str:
    package, floor = TRACKS[track]
    entry = _read().get(track) or {}
    age = time.time() - float(entry.get('checked_at') or 0)
    if age > (ERROR_RETRY_SECONDS if entry.get('error') else STALE_SECONDS):
        refresh_in_background(track)
    return highest(floor, entry.get('version')) or floor


def refresh(track: str, minimum: str = '', reason: str = 'scheduled') -> str:
    from . import http
    package, floor = TRACKS[track]
    latest, error = '', ''
    try:
        data = http.get_json(f"https://registry.npmjs.org/{package.replace('/', '%2F')}/latest", timeout=15)
        latest = str(data.get('version') or '') if parse(data.get('version')) else ''
        if not latest:
            error = 'npm answered no usable version'
    except Exception as e:
        error = str(e)
    with _lock:
        state = _read()
        previous = (state.get(track) or {}).get('version')
        # Never step backwards: a registry hiccup must not drop below a floor
        # a 426 already taught us.
        version = highest(latest, minimum, previous, floor)
        state[track] = {'version': version, 'latest': latest, 'checked_at': time.time(), 'reason': reason,
                        **({'error': error} if error else {}), **({'min_required': minimum} if minimum else {})}
        _write(state)
    if previous and version != previous:
        logger.info(f"{track}: client version {previous} → {version} ({reason})")
    return version


def refresh_in_background(track: str, minimum: str = '', reason: str = 'scheduled'):
    with _lock:
        if track in _refreshing:
            return
        _refreshing.add(track)

    def run():
        try:
            refresh(track, minimum, reason)
        finally:
            with _lock:
                _refreshing.discard(track)

    threading.Thread(target=run, daemon=True, name=f'ai-version-{track}').start()


# After a 426: learn the minimum it names, synchronously, so the one retry
# goes out with an accepted version.
def note_refusal(track: str, body: str) -> str:
    return refresh(track, minimum_from_426(body), reason='426')
