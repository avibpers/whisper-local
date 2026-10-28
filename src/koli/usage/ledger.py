# usage/ledger.py
# What every transcription and AI call used and cost: one record per call
# (feature, model, audio seconds, tokens, dollars, and the meeting it belongs
# to), summed for this session, per day and model, and per meeting and model.
# The sums live in usage.json in the data folder, written a few seconds after
# a change; each record is published on the 'usage' topic so the screens
# update live. Design: docs/design/online-models.md.

import contextlib
import datetime
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from . import prices

logger = logging.getLogger(__name__)

SAVE_DELAY_S = 5
KEEP_DAYS = 400
_FIELDS = ('calls', 'seconds', 'tokens_in', 'tokens_out', 'audio_tokens', 'cost')

_lock = threading.RLock()
_state = {'loaded': False, 'days': {}, 'meetings': {}, 'session': {}, 'session_started': time.time(),
          'unpriced': set()}
_save_timer: Optional[threading.Timer] = None
_context = threading.local()


def path() -> Path:
    from ..utils import get_user_app_data_path
    return Path(get_user_app_data_path()) / 'usage.json'


def _load() -> None:
    if _state['loaded']:
        return
    _state['loaded'] = True
    try:
        data = json.loads(path().read_text(encoding='utf-8'))
        _state['days'] = data.get('days') or {}
        _state['meetings'] = data.get('meetings') or {}
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"usage.json not read, starting fresh: {e}")


def _save_now() -> None:
    with _lock:
        cutoff = (datetime.date.today() - datetime.timedelta(days=KEEP_DAYS)).isoformat()
        days = {d: v for d, v in _state['days'].items() if d >= cutoff}
        _state['days'] = days
        text = json.dumps({'days': days, 'meetings': _state['meetings']}, ensure_ascii=False)
    target = path()
    tmp = target.with_suffix('.tmp')
    try:
        tmp.write_text(text, encoding='utf-8')
        tmp.replace(target)
    except OSError as e:
        logger.warning(f"usage.json not saved: {e}")


def _save_soon() -> None:
    global _save_timer
    if _save_timer is not None:
        return
    def run():
        global _save_timer
        _save_timer = None
        _save_now()
    _save_timer = threading.Timer(SAVE_DELAY_S, run)
    _save_timer.daemon = True
    _save_timer.start()


# Writes now (at quit), so the last few seconds of calls aren't lost.
def flush() -> None:
    global _save_timer
    if _save_timer is not None:
        _save_timer.cancel()
        _save_timer = None
        _save_now()


def _add(bucket: dict, rec: dict) -> None:
    row = bucket.setdefault(rec['model'], {k: 0 for k in _FIELDS})
    row['calls'] += 1
    for k in _FIELDS[1:]:
        if k == 'cost' and rec['cost'] is None:
            row['unpriced'] = True
            continue
        row[k] = round(row.get(k, 0) + (rec.get(k) or 0), 6 if k == 'cost' else 2)


# ── Which meeting a call belongs to ──────────────────────────────────────────

# Calls made inside this block (a chunk's transcription, a summary, the
# meeting chat) are counted on the meeting; `feature` names the AI calls in it
# ("summary", "chat") instead of the AI feature's own name ("meetings"). Per
# thread, so a dictation running alongside isn't counted.
@contextlib.contextmanager
def for_meeting(meeting_id: Optional[str], feature: str = ''):
    previous = getattr(_context, 'meeting_id', None), getattr(_context, 'feature', '')
    _context.meeting_id, _context.feature = meeting_id, feature
    try:
        yield
    finally:
        _context.meeting_id, _context.feature = previous


def current_meeting() -> Optional[str]:
    return getattr(_context, 'meeting_id', None)


# The name an AI call is counted under: the block's, else its own.
def ai_feature(feature: str) -> str:
    return getattr(_context, 'feature', '') or feature


# ── Recording ────────────────────────────────────────────────────────────────

# One call. `feature`: dictation, meeting, summary, chat, polish, …; `spec`:
# "provider/model" ("local/large-v3" for local Whisper). `cost` is worked out
# from the price list unless the caller already knows it. Never raises.
def record(feature: str, spec: str, seconds: float = 0.0, tokens_in: int = 0, tokens_out: int = 0,
           audio_tokens: int = 0, meeting_id: Optional[str] = None, cost: Optional[float] = -1) -> dict:
    try:
        if cost == -1:
            cost = prices.cost(spec, seconds=seconds, audio_tokens=audio_tokens,
                               text_in=tokens_in, text_out=tokens_out)
        meeting_id = meeting_id or current_meeting()
        rec = {'feature': feature, 'model': spec, 'seconds': round(float(seconds or 0), 2),
               'tokens_in': int(tokens_in or 0), 'tokens_out': int(tokens_out or 0),
               'audio_tokens': int(audio_tokens or 0), 'cost': None if cost is None else round(cost, 6),
               'meeting_id': meeting_id, 'time': time.time()}
        with _lock:
            _load()
            day = datetime.date.today().isoformat()
            _add(_state['days'].setdefault(day, {}).setdefault(feature, {}), rec)
            _add(_state['session'].setdefault(feature, {}), rec)
            if meeting_id:
                _add(_state['meetings'].setdefault(meeting_id, {}).setdefault(feature, {}), rec)
            if cost is None and spec not in _state['unpriced']:
                _state['unpriced'].add(spec)
                logger.info(f"No price for {spec}; add one on the Usage screen")
            _save_soon()
            totals = {'session_cost': _total(_state['session']),
                      'meeting_cost': _total(_state['meetings'].get(meeting_id) or {}) if meeting_id else None}
        _publish({**rec, **totals})
        return rec
    except Exception as e:
        logger.warning(f"Usage not recorded: {e}")
        return {}


def _publish(payload: dict) -> None:
    try:
        from ..ui.events import publish
        publish('usage', payload)
    except Exception:
        pass


def _total(by_feature: dict) -> float:
    return round(sum((row.get('cost') or 0) for rows in by_feature.values() for row in rows.values()), 6)


# ── Reading ──────────────────────────────────────────────────────────────────

def session() -> dict:
    with _lock:
        return {'started': _state['session_started'], 'cost': _total(_state['session']),
                'by_feature': json.loads(json.dumps(_state['session']))}


# One meeting's use: {feature: {model: {calls, seconds, tokens…, cost}}}, the
# total, and the models that transcribed it.
def meeting(meeting_id: str) -> dict:
    with _lock:
        _load()
        by_feature = json.loads(json.dumps(_state['meetings'].get(meeting_id) or {}))
    return {'by_feature': by_feature, 'cost': _total(by_feature),
            'transcribed_by': sorted((by_feature.get('meeting') or {}).keys()),
            'unpriced': any(r.get('unpriced') for rows in by_feature.values() for r in rows.values())}


# Totals for every meeting at once, for the list: {id: {cost, models}}.
def meetings_brief() -> dict:
    with _lock:
        _load()
        return {mid: {'cost': _total(v), 'transcribed_by': sorted((v.get('meeting') or {}).keys())}
                for mid, v in _state['meetings'].items()}


def forget_meeting(meeting_id: str) -> None:
    with _lock:
        _load()
        if _state['meetings'].pop(meeting_id, None) is not None:
            _save_soon()


# Day by day for the last `days` days: [{day, cost, by_feature}], newest first.
def days(days: int = 30) -> list:
    with _lock:
        _load()
        cutoff = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
        rows = [{'day': d, 'cost': _total(v), 'by_feature': json.loads(json.dumps(v))}
                for d, v in _state['days'].items() if d >= cutoff]
    return sorted(rows, key=lambda r: r['day'], reverse=True)
