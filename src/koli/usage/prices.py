# usage/prices.py
# What a model call costs. The shipped list (prices.yaml beside this file) with
# the user's own prices.yaml from the settings folder laid over it, entry by
# entry. cost() turns one call's audio seconds and tokens into dollars; local
# models and subscriptions cost nothing, a cloud model missing from the list
# has no price (None). Design: docs/design/online-models.md.

import datetime
import logging
import threading
from pathlib import Path
from typing import Optional

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

SHIPPED = Path(__file__).with_name('prices.yaml')
# Providers whose calls are already paid for (a subscription) or run here.
FREE_PROVIDERS = ('local', 'ollama', 'lmstudio', 'codex', 'grok')
PRICE_FIELDS = ('per_minute', 'audio_in', 'text_in', 'text_out')

_lock = threading.Lock()
_cache = {'mtime': None, 'data': None}


def user_file() -> Path:
    from ..utils import get_user_app_data_path
    return Path(get_user_app_data_path()) / 'prices.yaml'


def _read(path: Path) -> dict:
    try:
        with open(path, encoding='utf-8') as f:
            return YAML(typ='safe').load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Prices in {path} not read: {e}")
        return {}


# The merged list, re-read when the user's file changes.
def _data() -> dict:
    path = user_file()
    mtime = path.stat().st_mtime if path.exists() else 0
    with _lock:
        if _cache['data'] is None or _cache['mtime'] != mtime:
            shipped, own = _read(SHIPPED), _read(path)
            models = {k: dict(v or {}) for k, v in (shipped.get('models') or {}).items()}
            # Your price wins over the shipped one and its later changes.
            for spec, entry in (own.get('models') or {}).items():
                shipped_entry = {k: v for k, v in models.get(spec, {}).items() if k != 'changes'}
                models[spec] = {**shipped_entry, **(entry or {}), 'edited': True}
            _cache['data'] = {'models': models,
                              'gemini_tps': float(shipped.get('gemini_audio_tokens_per_second') or 25)}
            _cache['mtime'] = mtime
        return _cache['data']


# One model's prices on `day` (today by default): a dated change applies from
# its date on.
def entry(spec: str, day: Optional[datetime.date] = None) -> Optional[dict]:
    raw = _data()['models'].get(spec)
    if raw is None:
        return None
    day = (day or datetime.date.today()).isoformat()
    current = {k: v for k, v in raw.items() if k != 'changes'}
    for change in sorted(raw.get('changes') or [], key=lambda c: str(c.get('from'))):
        if str(change.get('from')) <= day:
            current.update({k: v for k, v in change.items() if k != 'from'})
    return current


def is_free(spec: str) -> bool:
    return str(spec).partition('/')[0] in FREE_PROVIDERS


# Dollars for one call, or None when the model has no price. Tokens win when
# the answer gave them and the model is priced by the token; else minutes.
def cost(spec: str, seconds: float = 0.0, audio_tokens: int = 0, text_in: int = 0, text_out: int = 0) -> Optional[float]:
    if is_free(spec):
        return 0.0
    p = entry(spec)
    if p is None:
        return None
    by_token = any(p.get(k) is not None for k in ('audio_in', 'text_in', 'text_out'))
    if by_token and (audio_tokens or text_in or text_out):
        audio_rate = p.get('audio_in', p.get('text_in')) or 0
        return (audio_tokens * audio_rate + text_in * (p.get('text_in') or 0)
                + text_out * (p.get('text_out') or 0)) / 1e6
    if p.get('per_minute') is not None:
        return seconds / 60 * float(p['per_minute'])
    if by_token and seconds:
        # No tokens in the answer: estimate the audio from its length.
        return seconds * _data()['gemini_tps'] * (p.get('audio_in') or 0) / 1e6
    return None


# Dollars for an hour of speech, for comparing models: the per-minute price,
# or the audio tokens of an hour plus about 175 words-tokens a minute written.
def per_hour(p: dict) -> Optional[float]:
    if p.get('per_minute') is not None:
        return round(float(p['per_minute']) * 60, 4)
    if p.get('audio_in') is not None:
        audio = 3600 * _data()['gemini_tps'] * float(p['audio_in'])
        out = 60 * 175 * float(p.get('text_out') or 0)
        return round((audio + out) / 1e6, 4)
    return None


# Every model with today's prices, for the pickers and the Usage screen.
def catalog(transcribe_only: bool = False) -> list:
    out = []
    for spec in _data()['models']:
        p = entry(spec)
        if transcribe_only and not p.get('transcribe'):
            continue
        provider, _, model = spec.partition('/')
        upcoming = [c for c in (_data()['models'][spec].get('changes') or [])
                    if str(c.get('from')) > datetime.date.today().isoformat()]
        out.append({'spec': spec, 'provider': provider, 'model': model,
                    'transcribe': p.get('transcribe') or '', 'note': p.get('note') or '',
                    'edited': bool(p.get('edited')), 'per_hour': per_hour(p) if p.get('transcribe') else None,
                    **{k: p.get(k) for k in PRICE_FIELDS},
                    'changes': [{'from': str(c.get('from')), **{k: c.get(k) for k in PRICE_FIELDS if k in c}}
                                for c in upcoming]})
    return out


# Saves your own prices for one model (empty values drop back to the
# shipped price); `prices` None removes your entry altogether.
def save_own(spec: str, prices: Optional[dict]) -> None:
    if '/' not in str(spec):
        raise ValueError('A model is written provider/model')
    path = user_file()
    yaml = YAML()
    data = _read(path) or {}
    models = dict(data.get('models') or {})
    if prices is None:
        models.pop(spec, None)
    else:
        clean = {}
        for k in PRICE_FIELDS:
            v = prices.get(k)
            if v not in (None, ''):
                clean[k] = float(v)
        if prices.get('transcribe') in ('upload', 'live', 'chat'):
            clean['transcribe'] = prices['transcribe']
        models[spec] = clean
    data['models'] = models
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        yaml.dump(data, f)
    tmp.replace(path)
    with _lock:
        _cache['data'] = None
