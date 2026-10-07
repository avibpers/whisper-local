# services/models.py
# The models table: which Whisper models exist, which are on disk and how big,
# downloading one with real progress, deleting one from the shared Hugging Face
# cache (only our own model folders), and adding your own model. Downloads poll
# the cache folder's size against the repo's file sizes, which works whether
# Hugging Face uses plain HTTP or Xet underneath.

import fnmatch
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The files faster-whisper fetches for a model (mirrors faster_whisper.utils).
ALLOW_PATTERNS = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]

GROUP_ORDER = ['multilingual', 'hebrew', 'english', 'yours']
GROUP_TITLES = {'multilingual': 'Multilingual', 'hebrew': 'Hebrew (ivrit.ai)',
                'english': 'English only', 'yours': 'Your models'}


def repo_for(registry, key: str) -> Optional[str]:
    source = registry.get_source(key)
    model = registry.get_model(key)
    if model is not None and model.is_local_path:
        return None
    if '/' in source:
        return source
    try:
        from faster_whisper.utils import _MODELS
        return _MODELS.get(source)
    except Exception:
        return None


def cache_path(registry, key: str) -> Optional[Path]:
    model = registry.get_model(key)
    if model is not None and model.is_local_path:
        return Path(model.source)
    folder = registry.get_cache_folder(key)
    if not folder:
        return None
    return Path(registry.get_hf_cache_path()) / folder


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    if not os.path.islink(fp):
                        total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _group_of(key: str, model) -> str:
    source = (getattr(model, 'source', '') or '').lower()
    if key.startswith('hebrew') or 'ivrit' in source:
        return 'hebrew'
    # The distil-whisper models are English-only too.
    if key.endswith('.en') or source.endswith('.en') or 'distil' in key:
        return 'english'
    if getattr(model, 'group', 'custom') == 'official':
        return 'multilingual'
    return 'yours'


def _size_hint(key: str) -> str:
    try:
        from ..whisper_engine import _model_size_hint
        return _model_size_hint(key)
    except Exception:
        return ''


# Every configured model with its group, label, disk state and size.
def list_models(registry, current_key: str, models_cfg: dict) -> list:
    out = []
    for key, model in registry.whisper_models.items():
        cfg = models_cfg.get(key) or {}
        cached = registry.is_model_cached(key)
        path = cache_path(registry, key)
        size = _dir_size(path) if cached and path else 0
        out.append({
            'key': key,
            'label': model.label,
            'group': _group_of(key, model),
            'source': model.source,
            'enabled': bool(cfg.get('enabled', True)),
            'cached': bool(cached),
            'local': bool(model.is_local_path),
            'size_bytes': size,
            'size_hint': _size_hint(key),
            'in_use': key == current_key,
            'deletable': bool(cached and not model.is_local_path and key != current_key),
        })
    out.sort(key=lambda m: (GROUP_ORDER.index(m['group']) if m['group'] in GROUP_ORDER else 9))
    return out


def _expected_bytes(repo_id: str) -> Optional[int]:
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(repo_id, files_metadata=True)
        total = 0
        for sib in info.siblings or []:
            name = sib.rfilename
            if any(fnmatch.fnmatch(name, p) for p in ALLOW_PATTERNS):
                total += int(sib.size or 0)
        return total or None
    except Exception as e:
        logger.debug(f"Could not read model size for {repo_id}: {e}")
        return None


# Download a model into the Hugging Face cache, reporting progress through a
# TaskReporter. Returns the snapshot path. No-op for models already on disk or
# stored in a local folder.
def ensure_downloaded(registry, key: str, reporter=None) -> Optional[str]:
    if registry.is_model_cached(key):
        return None
    repo_id = repo_for(registry, key)
    if not repo_id:
        raise RuntimeError(f"Don't know where to download “{key}” from")
    if reporter:
        reporter.report(None, 'Checking the download size…')
    expected = _expected_bytes(repo_id)
    target = cache_path(registry, key)
    result, error = {}, {}

    def worker():
        try:
            from huggingface_hub import snapshot_download
            result['path'] = snapshot_download(repo_id, allow_patterns=ALLOW_PATTERNS)
        except Exception as e:
            error['e'] = e

    th = threading.Thread(target=worker, daemon=True, name=f'download-{key}')
    th.start()
    started = time.time()
    while th.is_alive():
        th.join(0.5)
        if reporter:
            done = _dir_size(target) if target else 0
            if expected:
                mb, total_mb = done / 1e6, expected / 1e6
                reporter.report(min(0.99, done / expected), f"Downloading {mb:,.0f} of {total_mb:,.0f} MB")
            else:
                reporter.report(None, f"Downloading… {done / 1e6:,.0f} MB")
            # Stops waiting; the finished part stays in the cache for next time.
            reporter.check_cancelled()
    if 'e' in error:
        raise RuntimeError(f"Download failed: {error['e']}")
    logger.info(f"Downloaded model {key} ({repo_id}) in {time.time() - started:.0f}s")
    return result.get('path')


# Remove a model's folder from the cache. Refuses anything outside our own
# model folders (the cache is shared with other tools) and the model in use.
def delete_cached(registry, key: str, current_key: str) -> bool:
    if key == current_key:
        raise RuntimeError("That model is in use. Switch to another model first.")
    model = registry.get_model(key)
    if model is not None and model.is_local_path:
        raise RuntimeError("This model lives in your own folder; remove it from the list instead.")
    path = cache_path(registry, key)
    if not path or not path.exists():
        return False
    hub = Path(registry.get_hf_cache_path()).resolve()
    resolved = path.resolve()
    if hub not in resolved.parents or not resolved.name.startswith('models--'):
        raise RuntimeError("Refusing to delete a folder outside the model cache")
    shutil.rmtree(resolved)
    logger.info(f"Deleted cached model {key} at {resolved}")
    return True


def model_entry_for_repo(repo_id: str, label: str = '') -> tuple:
    key = repo_id.split('/')[-1].lower().replace('_', '-')
    return key, {'source': repo_id, 'label': label or repo_id.split('/')[-1], 'group': 'custom', 'enabled': True}


def model_entry_for_folder(folder: str, label: str = '') -> tuple:
    path = Path(folder)
    if not (path / 'model.bin').exists():
        raise RuntimeError("That folder has no model.bin, so it isn't a CTranslate2 Whisper model.")
    key = 'local-' + path.name.lower().replace(' ', '-')
    return key, {'source': str(path), 'label': label or f"{path.name} (folder)", 'group': 'custom', 'enabled': True}
