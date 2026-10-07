# services/insights.py
# Numbers for the History & insights screen, Live › Today and Simple Home:
# totals, time saved, speaking pace, streaks, words per day, where you dictate
# and which languages. Reads stats.jsonl (metadata only) and, for the language
# split, transcripts.jsonl.

import datetime
import json
from collections import Counter
from pathlib import Path

from .. import transcript_log
from ..stats import (CHARS_PER_WORD, STATS_FILE, WPM_TYPING_BASELINE, _compute_streaks,
                     _safe_float, _safe_int)
from ..utils import get_user_app_data_path


def _entries() -> list:
    path = Path(get_user_app_data_path()) / STATS_FILE
    out = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return out


def _summarise(entries: list) -> dict:
    count = len(entries)
    chars = sum(_safe_int(e.get('chars')) for e in entries)
    seconds = sum(_safe_float(e.get('duration_s')) for e in entries)
    words = chars // CHARS_PER_WORD
    minutes_speaking = seconds / 60
    saved = max(0.0, words / WPM_TYPING_BASELINE - minutes_speaking)
    return {
        'count': count, 'words': words, 'seconds': round(seconds, 1),
        'minutes_saved': round(saved, 1),
        'wpm': round(words / minutes_speaking) if minutes_speaking > 0 else 0,
        'avg_words': round(words / count) if count else 0,
        'avg_seconds': round(seconds / count, 1) if count else 0,
    }


def summary(days: int = 14) -> dict:
    entries = _entries()
    today = datetime.date.today()
    today_s = today.isoformat()
    active = {e.get('ts', '')[:10] for e in entries if e.get('ts')}
    streak, longest = _compute_streaks(active, today)

    per_day = []
    for i in range(days - 1, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        day_entries = [e for e in entries if (e.get('ts') or '').startswith(d)]
        per_day.append({'date': d, 'words': sum(_safe_int(e.get('chars')) for e in day_entries) // CHARS_PER_WORD,
                        'count': len(day_entries)})

    month_ago = (today - datetime.timedelta(days=30)).isoformat()
    recent = [e for e in entries if (e.get('ts') or '') >= month_ago] or entries
    apps = Counter((e.get('app') or 'unknown') for e in recent)
    total_recent = sum(apps.values()) or 1

    langs = Counter((t.get('language') or '') for t in transcript_log.load_transcripts())
    langs.pop('', None)
    total_langs = sum(langs.values()) or 1

    return {
        'lifetime': _summarise(entries),
        'today': _summarise([e for e in entries if (e.get('ts') or '').startswith(today_s)]),
        'streak': streak, 'longest_streak': longest, 'active_days': len(active),
        'first': entries[0].get('ts') if entries else None,
        'per_day': per_day,
        'apps': [{'app': a, 'count': c, 'share': round(c / total_recent, 3)} for a, c in apps.most_common(6)],
        'languages': [{'code': l, 'count': c, 'share': round(c / total_langs, 3)} for l, c in langs.most_common(6)],
    }
