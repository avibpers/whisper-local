# services/learning.py
# The Training screen's view of self-learning: whether clips are kept, how
# much disk they use against the cap, what happened to the dictations this
# week, the recent clips (play one, or Test how the night model hears it),
# and the delete buttons. The recorder (learning/recorder.py) does the
# keeping; this reads the store, deletes from it and runs Test.
# Design: docs/design/self-learning.md.

import datetime
from typing import Optional

from ..learning import retention
from ..learning.night import SPEECH_MIN_PROB

# The delete buttons: how far back each reaches.
SPANS = {'hour': datetime.timedelta(hours=1), 'day': datetime.timedelta(days=1),
         'week': datetime.timedelta(days=7)}


class LearningService:
    # `engine`: returns the live WhisperEngine (the night model borrows its
    # device and precision); tests pass a night model of their own.
    def __init__(self, recorder, clock=datetime.datetime.now, engine=None, night=None):
        self.recorder = recorder
        self.store = recorder.store
        self._clock = clock
        self._engine = engine or (lambda: None)
        self._night_model = night
        self._tests = {}     # clip id → last Test result, for this session only

    # ── What the screen shows ───────────────────────────────────────────────

    def state(self) -> dict:
        self.recorder.flush()   # the dictation that just ended is on disk, so it counts
        records = self.store.records()
        now = self._clock()
        week_ago = (now - datetime.timedelta(days=7)).isoformat(timespec='seconds')
        week = [r for r in records if r.get('ts', '') >= week_ago]
        with_audio = [r for r in records if r.get('audio')]
        used = sum(int(r.get('bytes') or 0) for r in with_audio)
        cap = self.recorder.cap_gb() * 1e9
        seconds = sum(float(r.get('seconds') or 0) for r in with_audio)
        return {
            'enabled': self.recorder.enabled(),
            'folder': str(self.store.folder),
            'used_bytes': used, 'cap_bytes': int(cap),
            'clips': len(with_audio), 'records': len(records),
            'audio_hours': round(seconds / 3600, 2),
            'oldest': min((r['ts'] for r in with_audio), default=None),
            'newest': max((r['ts'] for r in records if r.get('ts')), default=None),
            'week': self._week_counts(week),
            'by_language': self._languages(with_audio),
        }

    @staticmethod
    def _week_counts(week: list) -> dict:
        typed = [r for r in week if r.get('outcome') == 'typed']
        dropped = [r for r in week if str(r.get('outcome', '')).startswith('dropped')]
        return {
            'kept': len(week), 'typed': len(typed), 'dropped': len(dropped),
            'said_again': sum(1 for r in typed if (r.get('signals') or {}).get('repeats')),
            'after_drop': sum(1 for r in typed if (r.get('signals') or {}).get('after_drop')),
            'hotkey': sum(1 for r in week if r.get('kind') == 'hotkey'),
            'auto': sum(1 for r in week if r.get('kind') == 'auto'),
        }

    @staticmethod
    def _languages(records: list) -> dict:
        out = {}
        for r in records:
            lang = r.get('language') or '?'
            out[lang] = out.get(lang, 0) + 1
        return out

    # ── Recent clips (play and test) ────────────────────────────────────────

    # Newest first, `limit` at a time from `offset`. `show`: all, typed,
    # dropped or again (said again, or right after a drop).
    def recent(self, limit: int = 20, offset: int = 0, show: str = 'all') -> dict:
        self.recorder.flush()
        rows = [r for r in reversed(self.store.records()) if self._shown(r, show)]
        page = rows[max(0, offset):max(0, offset) + max(1, min(100, limit))]
        return {'total': len(rows), 'items': [self._row(r) for r in page]}

    @staticmethod
    def _shown(r: dict, show: str) -> bool:
        outcome = str(r.get('outcome', ''))
        signals = r.get('signals') or {}
        return {'typed': outcome == 'typed', 'dropped': outcome.startswith('dropped'),
                'again': bool(signals.get('repeats') or signals.get('after_drop'))}.get(show, True)

    def _row(self, r: dict) -> dict:
        return {
            'id': r['id'], 'ts': r.get('ts'), 'app': r.get('app', ''), 'title': r.get('title', ''),
            'kind': r.get('kind', ''), 'seconds': r.get('seconds', 0), 'language': r.get('language', ''),
            'outcome': r.get('outcome', ''), 'why': r.get('why', ''),
            'heard': (r.get('live') or {}).get('text', ''), 'typed': r.get('final_text', ''),
            'signals': r.get('signals') or {}, 'has_audio': bool(r.get('audio')),
            'test': self._tests.get(r['id']),
        }

    # The clip's audio for the page's player, as a data URL.
    def audio(self, clip_id: str) -> dict:
        import base64
        record = self._record(clip_id)
        path = self.store.audio_path(record)
        if path is None or not path.exists():
            raise ValueError('The audio of this clip was deleted to save space')
        mime = 'audio/flac' if path.suffix == '.flac' else 'audio/wav'
        return {'url': f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"}

    # How the night pass would hear this clip, now. Nothing is saved or
    # learned; the result is remembered only for this screen. When the night
    # model isn't on this computer, says which one to download instead.
    def test(self, clip_id: str) -> dict:
        from ..learning.clips import read_audio
        from ..learning.night import compare
        record = self._record(clip_id)
        path = self.store.audio_path(record)
        if path is None or not path.exists():
            raise ValueError('The audio of this clip was deleted to save space')
        night = self._night()
        language = record.get('language') or None
        missing = night.missing(language)
        if missing:
            models = ((getattr(self.recorder.cm, 'config', None) or {}).get('whisper') or {}).get('models') or {}
            return {'needs': [{'key': k, 'label': (models.get(k) or {}).get('label', k)} for k in missing]}
        heard = night.hear(read_audio(path), language)
        reference = record.get('final_text') or (record.get('live') or {}).get('text', '')
        words, same = compare(reference, heard['text'])
        dropped = str(record.get('outcome', '')).startswith('dropped')
        verdict = ('speech' if heard['text'] and heard['mean_prob'] >= SPEECH_MIN_PROB else 'noise') if dropped \
            else ('same' if same else 'different')
        result = {'text': heard['text'], 'words': words, 'verdict': verdict, 'model': heard['model'],
                  'language': heard['language'], 'seconds': heard['seconds'], 'mean_prob': heard['mean_prob']}
        self._tests[clip_id] = result
        return result

    def _record(self, clip_id: str) -> dict:
        record = self.store.get(str(clip_id))
        if record is None:
            raise ValueError('That clip is gone')
        return record

    def _night(self):
        if self._night_model is None:
            from ..learning.night import NightModel
            self._night_model = NightModel(self._engine)
        return self._night_model

    # ── Deleting ────────────────────────────────────────────────────────────

    # `span`: hour, day or week back from now. Returns how many clips went.
    def delete_recent(self, span: str) -> int:
        if span not in SPANS:
            raise ValueError(f"Unknown span: {span}")
        return self.store.delete_since(self._clock() - SPANS[span])

    # Clips between two dates (YYYY-MM-DD, both days included).
    def delete_range(self, start: str, end: str) -> int:
        try:
            first = datetime.date.fromisoformat(start)
            last = datetime.date.fromisoformat(end)
        except (TypeError, ValueError):
            raise ValueError('Pick a start and an end date')
        if last < first:
            first, last = last, first
        lo, hi = first.isoformat(), (last + datetime.timedelta(days=1)).isoformat()
        return self.store.delete(r['id'] for r in self.store.records() if lo <= r.get('ts', '') < hi)

    def delete_all(self) -> int:
        self._tests.clear()
        return self.store.delete_all()

    # The cap changed or the screen asked: apply the rules now.
    def cleanup(self) -> dict:
        return self.recorder.cleanup()


_service: Optional[LearningService] = None


def get(recorder, engine=None) -> LearningService:
    global _service
    if _service is None or _service.recorder is not recorder:
        _service = LearningService(recorder, engine=engine)
    return _service
