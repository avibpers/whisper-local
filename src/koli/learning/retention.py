# learning/retention.py
# What gets deleted, and when, so the learning folder never keeps growing.
# Audio goes first and records stay: a record is a few hundred bytes and holds
# everything later phases learn from. The order is the plan's ladder: the
# least useful clips go first, the held-back test clips last.
# Design: docs/design/self-learning.md › Retention.

import datetime
import logging
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_CAP_GB = 2.0
DROPPED_DAYS = 14          # clips dropped as noise: long enough to tune the filters on
MAX_RECORDS = 10000        # records without audio beyond this go too, oldest first

# Eviction rank, lowest goes first. A later phase adds "learned from" (first).
def eviction_rank(record: dict) -> int:
    if record.get('split') == 'holdout':
        return 3            # every score is measured on these: last
    if str(record.get('outcome', '')).startswith('dropped'):
        return 2            # the only way to measure dropped real speech
    if (record.get('night') or {}).get('verdict') in ('differs', 'disagree'):
        return 2            # where the models differ is what later phases learn from
    return 1                # kept, typed dictations: the training pool


def _age_days(record: dict, now: datetime.datetime) -> float:
    try:
        return (now - datetime.datetime.fromisoformat(record['ts'])).total_seconds() / 86400
    except (KeyError, ValueError):
        return 0.0


# Applies the rules to `store`. Returns what was done, for the log and the
# Training screen: {'expired', 'evicted', 'records_dropped', 'used_bytes'}.
def enforce(store, cap_gb: float = DEFAULT_CAP_GB, now: Optional[datetime.datetime] = None) -> dict:
    now = now or datetime.datetime.now()
    cap = max(0.0, float(cap_gb)) * 1e9   # decimal GB, as the screen shows it
    records = store.records()

    # 1. Dropped clips have a shorter life.
    expired = [r['id'] for r in records if r.get('audio') and str(r.get('outcome', '')).startswith('dropped')
               and _age_days(r, now) > DROPPED_DAYS]
    store.drop_audio(expired)

    # 2. Over the cap: delete audio by rank, oldest first within a rank.
    evicted = []
    used = store.used_bytes()
    if used > cap:
        with_audio = [r for r in store.records() if r.get('audio')]
        with_audio.sort(key=lambda r: (eviction_rank(r), r.get('ts', '')))
        for r in with_audio:
            if used <= cap:
                break
            evicted.append(r['id'])
            used -= int(r.get('bytes') or 0)
        store.drop_audio(evicted)

    # 3. Too many records: the oldest without audio go.
    records = store.records()
    dropped_records = []
    if len(records) > MAX_RECORDS:
        silent = sorted((r for r in records if not r.get('audio')), key=lambda r: r.get('ts', ''))
        dropped_records = [r['id'] for r in silent[:len(records) - MAX_RECORDS]]
        store.delete(dropped_records)

    result = {'expired': len(expired), 'evicted': len(evicted), 'records_dropped': len(dropped_records),
              'used_bytes': store.used_bytes()}
    if expired or evicted or dropped_records:
        logger.info(f"Learning clips cleaned up: {result}")
    return result
