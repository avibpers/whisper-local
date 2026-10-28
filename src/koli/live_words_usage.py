# live_words_usage.py
# Counts what each "words while you speak" engine was used for, per day:
# recordings, seconds of audio, words written, words fixed in the bubble, and
# cloud tokens. With the price table in live_words.prices it says what each
# engine cost, so a week with one provider can be compared with a week with
# another (the usage card in History & insights).

import json
import os
import threading
from datetime import date
from pathlib import Path
from typing import Optional

from .utils import get_user_app_data_path

FILE_NAME = 'live_words_usage.json'
_lock = threading.Lock()
_LABELS = {'local': 'Local words', 'phrases': 'Phrases (Whisper)', 'openai': 'OpenAI', 'gemini': 'Gemini'}


def _default_path() -> Path:
    return Path(get_user_app_data_path()) / FILE_NAME


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data.get('days'), dict) else {'days': {}}
    except (OSError, ValueError, AttributeError):
        return {'days': {}}


# Adds one recording to today's counters for `key` (live_words.usage_key).
def record(key: str, seconds: float, words: int, fixed: bool, tokens_in: int = 0, tokens_out: int = 0,
           path: Optional[Path] = None, day: Optional[str] = None) -> None:
    path = path or _default_path()
    day = day or date.today().isoformat()
    with _lock:
        data = _read(path)
        row = data['days'].setdefault(day, {}).setdefault(key, {
            'recordings': 0, 'seconds': 0.0, 'words': 0, 'fixes': 0, 'tokens_in': 0, 'tokens_out': 0})
        row['recordings'] += 1
        row['seconds'] = round(row['seconds'] + max(0.0, float(seconds)), 2)
        row['words'] += max(0, int(words))
        row['fixes'] += 1 if fixed else 0
        row['tokens_in'] += max(0, int(tokens_in))
        row['tokens_out'] += max(0, int(tokens_out))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=1), encoding='utf-8')
        os.replace(tmp, path)


# What `key` cost for that much audio and those tokens: OpenAI bills per
# minute of audio, Gemini per million tokens. Local engines and models not in
# the table cost nothing.
def cost(key: str, seconds: float, tokens_in: int, tokens_out: int, prices: dict) -> float:
    if not key.startswith('cloud/'):
        return 0.0
    price = (prices or {}).get(key[len('cloud/'):]) or {}
    total = float(price.get('per_minute') or 0) * seconds / 60.0
    total += float(price.get('input_per_million') or 0) * tokens_in / 1_000_000
    total += float(price.get('output_per_million') or 0) * tokens_out / 1_000_000
    return total


def _label(key: str) -> str:
    parts = key.split('/')
    if parts[0] == 'cloud' and len(parts) >= 3:
        return f"{_LABELS.get(parts[1], parts[1])} · {'/'.join(parts[2:])}"
    return _LABELS.get(key, key)


# One row per engine over every recorded day, most recently used first.
def summary(prices: dict, path: Optional[Path] = None) -> list:
    path = path or _default_path()
    with _lock:
        data = _read(path)
    totals = {}
    for day in sorted(data['days']):
        for key, row in (data['days'][day] or {}).items():
            t = totals.setdefault(key, {'key': key, 'label': _label(key), 'first_day': day, 'last_day': day,
                                        'days': 0, 'recordings': 0, 'seconds': 0.0, 'words': 0, 'fixes': 0,
                                        'tokens_in': 0, 'tokens_out': 0})
            t['last_day'] = day
            t['days'] += 1
            for field in ('recordings', 'seconds', 'words', 'fixes', 'tokens_in', 'tokens_out'):
                t[field] += row.get(field, 0) or 0
    rows = []
    for t in totals.values():
        minutes = t['seconds'] / 60.0
        rows.append({**t, 'minutes': round(minutes, 2),
                     'words_per_minute': round(t['words'] / minutes, 1) if minutes else 0,
                     'cost': cost(t['key'], t['seconds'], t['tokens_in'], t['tokens_out'], prices)})
    return sorted(rows, key=lambda r: r['last_day'], reverse=True)
