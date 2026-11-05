# learning/night_pass.py
# The night pass: while the PC is locked or nobody has touched it for a while,
# two bigger Whisper models hear your kept clips again. When they write the
# same words with good confidence, that text can be trusted; when they don't,
# the clip is skipped, never guessed. Nothing is learned here and nothing you
# dictate changes: this only labels clips, for the learning phases after it.
# Stops between clips on any input. Nothing leaves this computer.
# Design: docs/design/self-learning.md › Phase 2.

import datetime
import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── The rule ─────────────────────────────────────────────────────────────────

# Two teachers per language, from different families: the two ivrit.ai models
# share training data, so their agreement alone proves little on Hebrew.
TEACHERS = {'he': ('hebrew-large-v3', 'large'), 'en': ('large', 'distil-large-v3.5')}
OTHER_TEACHERS = ('large',)
# Both teachers' mean word confidence, at least. Raising it wouldn't catch more
# mistakes: the one wrong agreement in the 2026-11-05 test scored 0.93 and 0.96.
MIN_WORD_CONFIDENCE = 0.7

# What a clip's night record can say. Only the first three are trusted, and
# trusted means "worth checking further", not "right": on 2026-11-05 both
# teachers heard "deploy finished" as "the play finished" with good confidence.
# Nothing may learn from a single clip; the next phase needs several clips and
# a better score on the held-back ones.
SAME = 'same'                  # the teachers agree with what Koli typed
DIFFERS = 'differs'            # the teachers agree, and Koli typed something else
SPEECH = 'speech'              # Koli dropped it, and the teachers agree it was speech
DISAGREE = 'disagree'          # the teachers wrote different words
UNSURE = 'unsure'              # they agree without confidence, or you said it again
NOISE = 'noise'                # nobody heard words in a dropped clip
TRUSTED = (SAME, DIFFERS, SPEECH)
VERDICTS = (SAME, DIFFERS, SPEECH, DISAGREE, UNSURE, NOISE)

# ── When it may run ──────────────────────────────────────────────────────────

IDLE_SECONDS = 10 * 60         # untouched this long, or locked
CHECK_SECONDS = 60             # how often a sleeping pass looks again
MAX_CLIP_SECONDS = 300         # longer clips would hold the model too long
MAX_FAILURES_IN_A_ROW = 5      # something is wrong: wait for the next check
FAILED_CLIP_RETRIES = 2


def teachers_for(language: str) -> tuple:
    return TEACHERS.get(language or '', OTHER_TEACHERS)


def _words(text: str) -> list:
    from ..practice.align import tokens
    return [t[1] for t in tokens(text or '')]


# The verdict on one clip from what the teachers heard. A language with one
# teacher takes the text Koli typed as the second voice: agreement can still
# confirm it, but a lone teacher never overrules it.
def judge(record: dict, heard: list, said_again: bool = False) -> dict:
    dropped = str(record.get('outcome', '')).startswith('dropped')
    live = (record.get('live') or {}).get('text', '')
    typed = record.get('final_text') or live
    voices = [_words(h.get('text', '')) for h in heard]
    if len(voices) == 1 and typed and not dropped:
        voices.append(_words(typed))
    if not any(voices):
        verdict = NOISE if dropped else UNSURE
    elif len(voices) < 2 or any(v != voices[0] for v in voices):
        verdict = DISAGREE
    elif any(float(h.get('mean_prob') or 0) < MIN_WORD_CONFIDENCE for h in heard):
        verdict = UNSURE
    elif dropped:
        verdict = SPEECH
    elif voices[0] in (_words(typed), _words(live)):
        # You said it again, so what Koli typed looked wrong to you. The
        # teachers agreeing with it doesn't settle that: skip the clip.
        verdict = UNSURE if said_again else SAME
    else:
        verdict = DIFFERS
    text = heard[0].get('text', '') if heard and verdict in TRUSTED else ''
    return {'verdict': verdict, 'trust': 'agreement' if verdict in TRUSTED else 'none', 'text': text}


class NightPass:
    # `is_busy`: a dictation or a meeting is running. The rest are here so
    # tests can run a whole night without waiting for one.
    def __init__(self, recorder, night, is_busy: Callable[[], bool] = lambda: False,
                 is_locked: Optional[Callable[[], bool]] = None, seconds_idle: Optional[Callable[[], float]] = None,
                 on_battery: Optional[Callable[[], bool]] = None, clock: Callable[[], datetime.datetime] = datetime.datetime.now):
        from ..platform import idle, session
        self.recorder = recorder
        self.store = recorder.store
        self.night = night
        self._is_busy = is_busy
        self._is_locked = is_locked or session.is_locked
        self._seconds_idle = seconds_idle or idle.seconds_idle
        self._on_battery = on_battery or idle.on_battery
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_run: Optional[str] = None
        self._last_stop = ''
        self._failures = {}       # clip id → failed attempts, for this session

    # ── The switch and the conditions ───────────────────────────────────────

    def enabled(self) -> bool:
        learning = (getattr(self.recorder.cm, 'config', None) or {}).get('learning') or {}
        return self.recorder.enabled() and learning.get('night_pass', True) is not False

    # Why it can't work right now, in a word for the screen; '' when it can.
    def why_not(self) -> str:
        if not self.enabled():
            return 'off'
        if self._on_battery():
            return 'battery'
        if not self._model_ready():
            return 'model'
        if self._is_busy():
            return 'busy'
        if not self._is_locked() and self._seconds_idle() < IDLE_SECONDS:
            return 'in_use'
        return ''

    # The night models borrow the live engine's device; with none (the app is
    # starting, or an online model takes dictation) there is nothing to borrow.
    def _model_ready(self) -> bool:
        try:
            self.night.is_missing('large')
            return True
        except Exception:
            return False

    # ── The clips ───────────────────────────────────────────────────────────

    # Clips not heard yet, newest first: the oldest may be deleted for space
    # before their turn, and the newest are the most like how you speak now.
    def pending(self) -> list:
        clips = [r for r in self.store.records()
                 if r.get('audio') and not r.get('night') and float(r.get('seconds') or 0) <= MAX_CLIP_SECONDS
                 and self._failures.get(r['id'], 0) < FAILED_CLIP_RETRIES]
        return list(reversed(clips))

    def missing_models(self) -> list:
        wanted = []
        for language in ('he', 'en'):
            wanted += [k for k in teachers_for(language) if k not in wanted]
        try:
            return [k for k in wanted if self.night.is_missing(k)]
        except Exception as e:
            logger.debug(f"Night pass couldn't check its models: {e}")
            return []

    # The language to hear a clip in: the teacher's own detection among your
    # languages, because a teacher told the wrong language translates.
    def _language_of(self, record: dict, audio) -> tuple:
        recorded = record.get('language') or ''
        whisper = (getattr(self.recorder.cm, 'config', None) or {}).get('whisper') or {}
        allowed = [str(code) for code in (whisper.get('languages') or [])]
        detected = self.night.detect(audio, allowed)
        if detected and detected != recorded:
            return detected, bool(recorded)
        return recorded or detected, False

    # Hears one clip and writes the result on its record.
    def hear_clip(self, record: dict, said_again: bool = False) -> dict:
        from .clips import read_audio
        path = self.store.audio_path(record)
        audio = read_audio(path)
        language, changed = self._language_of(record, audio)
        started = time.monotonic()
        heard = []
        for key in teachers_for(language):
            if self.night.is_missing(key):
                continue
            result = self.night.hear_with(key, audio, language)
            heard.append({'model': key, 'text': result['text'], 'mean_prob': result['mean_prob']})
        if not heard:
            raise ValueError(f'No night model for {language or "this clip"} on this computer')
        verdict = judge(record, heard, said_again)
        night = {'at': self._clock().isoformat(timespec='seconds'), 'language': language, 'language_changed': changed,
                 'teachers': heard, 'verdict': verdict['verdict'], 'text': verdict['text'],
                 'seconds': round(time.monotonic() - started, 1)}
        self.store.update(record['id'], {'night': night, 'trust': verdict['trust']})
        return night

    # ── One run ─────────────────────────────────────────────────────────────

    # Works through the clips until something needs the PC or none are left.
    # Returns how many clips it heard. The models load on the first clip and
    # go when the run ends, so they never sit in memory in the daytime.
    def run_once(self) -> int:
        done, failures_in_a_row = 0, 0
        self._running = True
        try:
            clips = self.pending()
            said_again = {(r.get('signals') or {}).get('repeats') for r in self.store.records()} - {None}
            for record in clips:
                if self._stop.is_set():
                    self._last_stop = 'stopped'
                    break
                reason = self.why_not()
                if reason:
                    self._last_stop = reason
                    break
                if self.store.get(record['id']) is None:
                    continue         # deleted since the list was made
                try:
                    self.hear_clip(record, record['id'] in said_again)
                    done += 1
                    failures_in_a_row = 0
                except Exception as e:
                    self._failures[record['id']] = self._failures.get(record['id'], 0) + 1
                    failures_in_a_row += 1
                    logger.warning(f"Night pass couldn't hear clip {record['id']}: {e}")
                    if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                        self._last_stop = 'failed'
                        break
            else:
                self._last_stop = 'finished'
        finally:
            self._running = False
            self.night.unload()
        if done:
            self._last_run = self._clock().isoformat(timespec='seconds')
            logger.info(f"Night pass heard {done} clips ({self._last_stop})")
        return done

    # ── The thread ──────────────────────────────────────────────────────────

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name='night-pass')
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(CHECK_SECONDS):
            try:
                if not self.why_not() and self.pending() and not self.missing_models():
                    self.run_once()
            except Exception as e:
                logger.warning(f"Night pass stopped: {e}")

    # ── What the screen shows ───────────────────────────────────────────────

    def status(self) -> dict:
        records = [r for r in self.store.records() if r.get('audio') or r.get('night')]
        heard = [r['night'] for r in records if r.get('night')]
        counts = {v: sum(1 for n in heard if n.get('verdict') == v) for v in VERDICTS}
        counts['wrong_language'] = sum(1 for n in heard if n.get('language_changed'))
        return {'enabled': self.enabled(), 'running': self._running, 'waiting': '' if self._running else self.why_not(),
                'heard': len(heard), 'left': len(self.pending()), 'counts': counts, 'last_run': self._last_run,
                'needs': self.missing_models()}
