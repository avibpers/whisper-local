# services/system.py
# System actions from Settings › System and the welcome flow: start on login,
# settings backup, moving a model to an offline computer, the guided uninstall,
# Ollama reachability, GPU detection and install, and a manual update check.
# Long jobs run as background tasks and stream their progress.

import contextlib
import io
import json
import logging
import subprocess
import sys
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Start on login ───────────────────────────────────────────────────────────

def autostart_state() -> dict:
    from .. import autostart
    return {'supported': autostart.is_supported(), 'enabled': autostart.is_enabled()}


def set_autostart(enabled: bool) -> bool:
    from .. import autostart
    ok = autostart.enable() if enabled else autostart.disable()
    if not ok and enabled:
        raise RuntimeError("Couldn't turn on start on login on this computer")
    return autostart.is_enabled()


# ── Backups and offline models ───────────────────────────────────────────────

def _quiet(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kwargs)
    return code, buf.getvalue()


def export_settings(dest: str) -> str:
    from ..settings_io import export_settings as run
    code, out = _quiet(run, dest)
    if code != 0:
        raise RuntimeError(out.strip().splitlines()[-1] if out.strip() else 'Export failed')
    return dest


# Copies the files in; the settings watcher notices the change and applies it.
def import_settings(src: str, config_manager) -> str:
    from ..settings_io import import_settings as run
    config_manager.flush()
    code, out = _quiet(run, src)
    if code != 0:
        raise RuntimeError(out.strip().splitlines()[-1] if out.strip() else 'Import failed')
    return src


def export_model(dest: str, config_manager) -> str:
    from ..model_transfer import export_model as run
    config_manager.flush()
    code, out = _quiet(run, dest or None)
    if code != 0:
        raise RuntimeError(next((l for l in out.splitlines() if 'ERROR' in l), 'Export failed').replace('ERROR: ', ''))
    for line in out.splitlines():
        if line.strip().startswith('to:'):
            return line.split('to:', 1)[1].strip()
    return dest


def import_model(src: str, keep_in_place: bool, config_manager) -> str:
    from ..model_transfer import import_model as run
    config_manager.flush()
    code, out = _quiet(run, src, keep_in_place=keep_in_place)
    if code != 0:
        raise RuntimeError(next((l for l in out.splitlines() if 'ERROR' in l), 'Import failed').replace('ERROR: ', ''))
    return src


# ── Uninstall (guided, from the app) ─────────────────────────────────────────

def uninstall_plan() -> dict:
    from .. import uninstall as un
    from ..utils import get_user_app_data_path
    config_dir = Path(get_user_app_data_path())
    models = un._our_cached_models()
    try:
        from .. import autostart
        auto = autostart.is_enabled()
    except Exception:
        auto = False
    return {
        'data_dir': str(config_dir), 'data_size': un._human(un._dir_size(config_dir)) if config_dir.is_dir() else '',
        'autostart': auto,
        'models': [{'name': p.name, 'size': un._human(s)} for p, s in models],
        'models_size': un._human(sum(s for _, s in models)),
    }


def uninstall(remove_models: bool) -> dict:
    from .. import uninstall as un
    from ..utils import get_user_app_data_path
    removed = []
    try:
        from .. import autostart
        if autostart.is_enabled():
            autostart.disable()
            removed.append('start-on-login entry')
    except Exception:
        pass
    if remove_models:
        for path, _ in un._our_cached_models():
            if un._remove(path):
                removed.append(path.name)
    config_dir = Path(get_user_app_data_path())
    logging.shutdown()   # app.log lives in the folder being removed
    if config_dir.is_dir() and un._remove(config_dir):
        removed.append(str(config_dir))
    return {'removed': removed}


# ── Local AI ─────────────────────────────────────────────────────────────────

def ollama_probe(endpoint: str, timeout: float = 2.0) -> dict:
    endpoint = (endpoint or 'http://localhost:11434').rstrip('/')
    try:
        with urllib.request.urlopen(f"{endpoint}/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read())
        models = [m.get('name') for m in data.get('models', []) if m.get('name')]
        return {'reachable': True, 'models': models}
    except Exception as e:
        return {'reachable': False, 'models': [], 'error': str(e)}


# ── GPU ──────────────────────────────────────────────────────────────────────

def gpu_detect(configured_device: str = 'cpu') -> dict:
    try:
        from ..platform import gpu
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gpu_class, gpu_name, ct2_works = gpu.detect_and_print(configured_device)
        missing = []
        try:
            missing = gpu.missing_gpu_libraries()
        except Exception:
            pass
        return {'class': gpu_class, 'name': gpu_name, 'ready': bool(ct2_works) and not missing,
                'missing': missing}
    except Exception as e:
        return {'class': None, 'name': None, 'ready': False, 'error': str(e)}


# Install the GPU runtime for the detected card, streaming pip's output.
def gpu_install(gpu_class: str, reporter) -> dict:
    from .. import onboarding as ob
    if gpu_class == 'nvidia':
        commands = [[sys.executable, '-m', 'pip', 'install', '--no-cache-dir'] + ob.NVIDIA_PACKAGES]
    elif gpu_class == 'amd_rdna2+':
        url = ob.get_ct2_wheel_url(gpu_class)
        if not url:
            raise RuntimeError("No GPU build of CTranslate2 exists for this Python version yet")
        commands = [[sys.executable, '-m', 'pip', 'install', '--no-cache-dir'] + ob.ROCM_72_PACKAGES,
                    [sys.executable, '-m', 'pip', 'install', '--force-reinstall', '--no-deps', url]]
    else:
        raise RuntimeError("This graphics card needs manual setup")
    for n, cmd in enumerate(commands, start=1):
        reporter.report((n - 1) / len(commands), 'Downloading GPU libraries…')
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for line in proc.stdout:
            line = line.strip()
            if line:
                logger.info(f"[gpu install] {line}")
                reporter.report(None, line[:120])
            if reporter.cancelled:
                proc.kill()
                raise RuntimeError("GPU install cancelled")
        if proc.wait() != 0:
            raise RuntimeError("pip could not install the GPU libraries; see the log for details")
    # Make the freshly installed DLLs findable without restarting.
    from ..utils import setup_nvidia_dll_path
    setup_nvidia_dll_path()
    return {'installed': gpu_class}


# ── Updates ──────────────────────────────────────────────────────────────────

def check_update() -> dict:
    from ..update_check import _API_URL, _is_newer
    from ..utils import get_version
    current = get_version()
    req = urllib.request.Request(_API_URL, headers={'User-Agent': f'whisper-local/{current}'})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read())
    latest = (data.get('tag_name') or '').lstrip('v')
    return {'current': current, 'latest': latest, 'newer': bool(latest and _is_newer(latest, current)),
            'url': data.get('html_url', '')}
