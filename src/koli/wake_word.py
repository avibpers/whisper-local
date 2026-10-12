# wake_word.py
# "Hey Koli, play Spotify": runs a voice command with no hotkey. A tap on the
# recorder's always-open microphone cuts speech into bursts; the start of a
# burst goes to Whisper (any language), and when it begins with the wake word
# the rest, or the next thing you say, runs as a command. With your own
# recorded word it also has to sound like you. No OS calls live here, so the
# policy is tested with fakes. Design: docs/design/wake-word.md

import collections
import difflib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, List, Optional

import numpy as np

from .utils import get_user_app_data_path, normalize_spoken

logger = logging.getLogger(__name__)

RATE = 16000
FRAME = 256                     # TEN VAD's frame (VAD_CHUNK_SIZE), 16 ms
FRAME_S = FRAME / RATE


# ── Matching the wake word ───────────────────────────────────────────────────

# How Whisper usually writes "Hey Koli", for the built-in word. A personal
# word replaces these with what Whisper wrote while you recorded it.
DEFAULT_SPELLINGS = ('hey koli', 'hey coli', 'hey coley', 'hey collie', 'hey cooley', 'hey kohli',
                     'היי קולי', 'היי כולי', 'הי קולי')

# How close the start of what was heard must be to a spelling (difflib ratio).
# The spellings carry the variants, so the margin is small: one letter off a
# short word is 0.86 ("hey cool" against "hey coli"), which only "high" lets
# through; "hey kelly" scores under 0.7 against all of them.
SENSITIVITY_RATIO = {'low': 0.97, 'medium': 0.9, 'high': 0.8}
# A personal word has to sound like you as well, and its spellings come from
# only three recordings, so its margin is wider: "coli" for "koli" (0.75)
# passes at medium.
PERSONAL_SLACK = 0.15
# Your recordings count as one word when they are this close: "Koli" and
# "Coli" are, "Koli" and "Hello" aren't.
RECORDINGS_AGREE_RATIO = 0.6


def ratio_for(sensitivity: str, personal: bool = False) -> float:
    ratio = SENSITIVITY_RATIO.get(str(sensitivity or '').lower(), SENSITIVITY_RATIO['medium'])
    return ratio - PERSONAL_SLACK if personal else ratio


# The text after the wake word ('' when it was said alone), or None when what
# was heard doesn't start with it. Words are compared with their spaces taken
# out and one word more or less than the spelling, because Whisper writes
# "Hey Koli" as "heykoli" or "hey ko li" too.
def match_wake(text: str, spellings: Iterable[str], min_ratio: float) -> Optional[str]:
    words = normalize_spoken(text).split()
    if not words:
        return None
    best_ratio, best_take = 0.0, None
    for spelling in spellings:
        target_words = normalize_spoken(spelling).split()
        if not target_words:
            continue
        target = ''.join(target_words)
        for take in (len(target_words) - 1, len(target_words), len(target_words) + 1):
            if take < 1 or take > len(words):
                continue
            ratio = difflib.SequenceMatcher(None, ''.join(words[:take]), target).ratio()
            if ratio >= min_ratio and ratio > best_ratio:
                best_ratio, best_take = ratio, take
    if best_take is None:
        return None
    return ' '.join(words[best_take:])


# The distinct spellings in what Whisper wrote for your recordings, or None
# when they don't agree (Whisper isn't hearing the word reliably): each one
# has to match every other at `min_ratio`.
def spellings_from_recordings(heard: List[str], min_ratio: float = RECORDINGS_AGREE_RATIO) -> Optional[List[str]]:
    spellings = []
    for text in heard:
        spelling = normalize_spoken(text)
        if not spelling:
            return None
        if spelling not in spellings:
            spellings.append(spelling)
    for a in spellings:
        for b in spellings:
            if a != b and match_wake(a, [b], min_ratio) != '':
                return None
    return spellings


# ── How often Whisper may check ──────────────────────────────────────────────

# With "Hey Koli" for anyone nothing filters before Whisper, so a TV or a
# song would send every burst to it. At most PER_MINUTE checks a minute; after
# MISSES_BEFORE_BACKOFF bursts in a row that weren't the wake word, one check
# every BACKOFF_INTERVAL seconds for BACKOFF_SECONDS. A wake starts afresh.
class CheckBudget:
    PER_MINUTE = 20
    MISSES_BEFORE_BACKOFF = 10
    BACKOFF_INTERVAL = 10.0
    BACKOFF_SECONDS = 60.0

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._checks: List[float] = []
        self._misses = 0
        self._backoff_until = 0.0

    # Whether a check may run now; one that may is counted.
    def take(self) -> bool:
        now = self._clock()
        self._checks = [t for t in self._checks if now - t < 60.0]
        if now < self._backoff_until:
            if self._checks and now - self._checks[-1] < self.BACKOFF_INTERVAL:
                return False
        elif len(self._checks) >= self.PER_MINUTE:
            return False
        self._checks.append(now)
        return True

    def missed(self) -> None:
        self._misses += 1
        if self._misses >= self.MISSES_BEFORE_BACKOFF:
            self._misses = 0
            self._backoff_until = self._clock() + self.BACKOFF_SECONDS

    def woke(self) -> None:
        self._misses = 0
        self._backoff_until = 0.0
        self._checks.clear()

    def backing_off(self) -> bool:
        return self._clock() < self._backoff_until


# ── Your voice for a personal word ───────────────────────────────────────────
# A voiceprint of you saying your wake word, one per microphone, kept in its
# own file. Comparing a one-second word with recordings of the same word is
# steadier than comparing it with the voiceprint made from whole dictations,
# and it leaves that one (auto-dictation, meetings) alone.

WAKE_VOICE_FILE = 'wake_voice.json'


class WakeVoices:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else Path(get_user_app_data_path()) / WAKE_VOICE_FILE
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    # Your wake-word voiceprint on `mic` (unit vector), or None.
    def get(self, mic: str) -> Optional[np.ndarray]:
        with self._lock:
            entry = self._read().get(mic or 'default')
        if not entry or not entry.get('centroid'):
            return None
        vec = np.asarray(entry['centroid'], dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else None

    # Keeps the average of `vectors` (one per recording) for `mic`.
    def set(self, mic: str, vectors: List[np.ndarray]) -> None:
        units = []
        for v in vectors:
            v = np.asarray(v, dtype=np.float32)
            norm = float(np.linalg.norm(v))
            if norm > 0:
                units.append(v / norm)
        if not units:
            return
        centroid = np.mean(units, axis=0)
        with self._lock:
            data = self._read()
            data[mic or 'default'] = {'centroid': [round(float(x), 6) for x in centroid], 'count': len(units),
                                      'saved': time.strftime('%Y-%m-%dT%H:%M:%S')}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data), encoding='utf-8')

    def mics(self) -> List[str]:
        with self._lock:
            return list(self._read())

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


# ── Cutting the microphone into bursts of speech ─────────────────────────────

class Burst:
    def __init__(self, frames):
        self.frames = list(frames)
        self.speech_frames = 0
        self.silence_frames = 0
        self.over = False
        self.head_done = False
        self.is_wake = False      # this burst held the wake word

    def audio(self) -> np.ndarray:
        return np.concatenate(self.frames) if self.frames else np.zeros(0, dtype=np.float32)

    def speech_seconds(self) -> float:
        return self.speech_frames * FRAME_S


# Frames in, ('head' | 'end', burst) events out. A burst starts on a speech
# frame (with PREROLL_S before it, so the first sound isn't clipped) when
# `may_start()` agrees, and ends after END_SILENCE_S of silence or at MAX_S.
# 'head' comes once, at HEAD_S or at the end if the burst is shorter.
class BurstCutter:
    SPEECH = 0.5
    HEAD_S = 2.0
    END_SILENCE_S = 0.6
    MAX_S = 8.0
    PREROLL_S = 0.3

    def __init__(self, head_s: float = HEAD_S, end_silence_s: float = END_SILENCE_S, max_s: float = MAX_S):
        self.head_frames = max(1, int(round(head_s / FRAME_S)))
        self.end_frames = max(1, int(round(end_silence_s / FRAME_S)))
        self.max_frames = max(self.head_frames, int(round(max_s / FRAME_S)))
        self._preroll = collections.deque(maxlen=max(1, int(round(self.PREROLL_S / FRAME_S))))
        self.burst: Optional[Burst] = None

    def feed(self, frame: np.ndarray, prob: float, may_start: Callable[[], bool] = lambda: True) -> list:
        speech = prob >= self.SPEECH
        burst = self.burst
        if burst is None:
            if speech and may_start():
                burst = self.burst = Burst(self._preroll)
                self._preroll.clear()
            else:
                self._preroll.append(frame)
                return []
        burst.frames.append(frame)
        if speech:
            burst.speech_frames += 1
            burst.silence_frames = 0
        else:
            burst.silence_frames += 1
        events = []
        over = burst.silence_frames >= self.end_frames or len(burst.frames) >= self.max_frames
        if over:
            burst.over = True
            self.burst = None
        if not burst.head_done and (over or len(burst.frames) >= self.head_frames):
            burst.head_done = True
            events.append(('head', burst))
        if over:
            events.append(('end', burst))
        return events

    # Forget the burst in progress (Koli got busy, the queue fell behind).
    def drop(self) -> None:
        self.burst = None
        self._preroll.clear()


# ── The listener ─────────────────────────────────────────────────────────────

SLEEPING, AWAKE, WAITING, FOLLOW_UP = 'sleeping', 'awake', 'waiting', 'follow_up'

# What run_wake_command reports back.
RAN, MISSED, EMPTY, BUSY = 'ran', 'missed', 'empty', 'busy'


class WakeListener:
    MIN_SPEECH_S = 0.3        # shorter bursts (a click, a cough) aren't checked
    # No new burst while the wake sound plays (app_ready.wav is 0.45 s, plus
    # starting it). Speech begun just before the end still makes it in
    # through the burst's pre-roll.
    CHIME_GUARD_S = 0.6
    WARM_POLL_S = 2.0
    LOCK_POLL_S = 2.0
    BUSY_POLL_S = 0.25
    QUEUE_SECONDS = 20.0      # audio waiting for the worker before it gives up on a burst
    DEFAULT_VOICE_MATCH = 0.45

    def __init__(self, state_manager, config_manager, *,
                 transcribe: Optional[Callable] = None,
                 voiceprint: Optional[Callable] = None,
                 run_command: Optional[Callable] = None,
                 wake_voices: Optional[WakeVoices] = None,
                 speech_prob: Optional[Callable] = None,
                 is_locked: Optional[Callable[[], bool]] = None,
                 publish: Optional[Callable] = None,
                 play_sound: Optional[Callable[[], None]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.sm = state_manager
        self.cm = config_manager
        self._transcribe = transcribe or self._engine_transcribe
        self._voiceprint = voiceprint or self._meeting_voiceprint
        self._run_command = run_command or self._state_manager_run
        self.wake_voices = wake_voices or WakeVoices()
        self._speech_prob_factory = (lambda: speech_prob) if speech_prob else _speech_prob_function
        self._is_locked = is_locked or _platform_is_locked
        self._publish = publish or _publish
        self._play_sound = play_sound or self._wake_sound
        self._clock = clock
        self.budget = CheckBudget(clock)
        self.cutter = BurstCutter()
        self.state = SLEEPING
        self._deadline = 0.0
        self._chime_until = 0.0
        self._locked_cache = (False, -1e9)
        self._busy_checked = -1e9
        self._enrolling = False
        self._noticed_mics = set()
        self._noticed_loading = False
        self._voice_model_ready = False
        self._warm_checked = -1e9
        # Recordings of your word on the setup screen, until saved: {'heard', 'voiceprint'}.
        self.recordings: list = []
        # The worker: audio from the tap waits here, measured in samples.
        self._pending = collections.deque()
        self._pending_samples = 0
        self._pending_lock = threading.Lock()
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tapped = None        # the recorder our tap is on
        self._remainder = np.zeros(0, dtype=np.float32)
        self.logger = logging.getLogger(__name__)

    # ── Settings ─────────────────────────────────────────────────────────────

    def _cfg(self) -> dict:
        cfg = (getattr(self.cm, 'config', None) or {}).get('wake_word')
        return cfg if isinstance(cfg, dict) else {}

    def enabled(self) -> bool:
        return bool(self._cfg().get('enabled', False))

    def spellings(self) -> List[str]:
        phrases = [p for p in (self._cfg().get('phrases') or []) if isinstance(p, str) and p.strip()]
        return phrases or list(DEFAULT_SPELLINGS)

    # Your own word that must sound like you.
    def personal(self) -> bool:
        cfg = self._cfg()
        return bool(cfg.get('only_my_voice')) and bool(cfg.get('phrases'))

    def match_ratio(self) -> float:
        return ratio_for(self._cfg().get('sensitivity', 'medium'), personal=self.personal())

    def _seconds(self, key: str, default: float) -> float:
        try:
            return max(1.0, float(self._cfg().get(key, default)))
        except (TypeError, ValueError):
            return default

    def voice_match(self) -> float:
        try:
            return float(self._cfg().get('voice_match', self.DEFAULT_VOICE_MATCH))
        except (TypeError, ValueError):
            return self.DEFAULT_VOICE_MATCH

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if not self.enabled():
            return False
        if self.is_running():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='wake-word', daemon=True)
        self._thread.start()
        self._attach()
        self.logger.info("Wake word: listening")
        return True

    def stop(self) -> None:
        self._detach()
        self._stop.set()
        self._wakeup.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._clear_pending()
        self.cutter.drop()
        self._sleep()

    # The Settings switch, live.
    def apply_settings(self) -> None:
        if self.enabled():
            self.start()
        elif self.is_running():
            self.stop()
            self.logger.info("Wake word: off")

    # The recorder is rebuilt when the microphone changes and copies its taps,
    # so the tap is always added to and removed from the current one.
    def _attach(self) -> None:
        recorder = getattr(self.sm, 'audio_recorder', None)
        if recorder is not None and hasattr(recorder, 'add_tap'):
            recorder.add_tap(self._on_tap)
            self._tapped = recorder

    def _detach(self) -> None:
        for recorder in (getattr(self.sm, 'audio_recorder', None), self._tapped):
            if recorder is not None:
                try:
                    recorder.remove_tap(self._on_tap)
                except Exception:
                    pass
        self._tapped = None

    # ── Audio in (audio thread: queue only) ──────────────────────────────────

    def _on_tap(self, chunk, when=None) -> None:
        mono = np.asarray(chunk, dtype=np.float32).reshape(-1)
        with self._pending_lock:
            self._pending.append(mono)
            self._pending_samples += len(mono)
        self._wakeup.set()

    def _clear_pending(self) -> None:
        with self._pending_lock:
            self._pending.clear()
            self._pending_samples = 0

    # The next chunk to process, or None. When the worker fell too far behind
    # (Whisper busy), what waits would splice the burst: start clean instead.
    def _take_pending(self) -> Optional[np.ndarray]:
        with self._pending_lock:
            if not self._pending:
                return None
            if self._pending_samples <= self.QUEUE_SECONDS * RATE:
                chunk = self._pending.popleft()
                self._pending_samples -= len(chunk)
                return chunk
            self._pending.clear()
            self._pending_samples = 0
        self.logger.info("Wake word: fell behind, starting clean")
        self.cutter.drop()
        self._remainder = np.zeros(0, dtype=np.float32)
        self._sleep()
        return None

    # ── Worker ───────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        try:
            speech_prob = self._speech_prob_factory()
        except Exception as e:
            self.logger.warning(f"Wake word: no speech detector ({e})")
            return
        while not self._stop.is_set():
            chunk = self._take_pending()
            if chunk is None:
                self._wakeup.wait(0.1)
                self._wakeup.clear()
                self.tick()
                continue
            audio = np.concatenate([self._remainder, chunk]) if len(self._remainder) else chunk
            whole = len(audio) // FRAME * FRAME
            self._remainder = audio[whole:]
            for start in range(0, whole, FRAME):
                frame = audio[start:start + FRAME]
                try:
                    self.feed(frame, speech_prob(frame))
                except Exception as e:
                    self.logger.warning(f"Wake word: frame failed ({e})")
                    self.cutter.drop()
                    self._sleep()

    # ── The state machine (one 16 ms frame at a time) ────────────────────────

    def feed(self, frame: np.ndarray, prob: float) -> None:
        self._warm_voice_model()
        for event, burst in self.cutter.feed(frame, prob, may_start=self._may_start):
            if event == 'head':
                self._on_head(burst)
            else:
                self._on_end(burst)
        self.tick()

    # Time-based moves: the wait for a command or a follow-up runs out, or
    # Koli got busy (hotkey, meeting) in the middle of a wake.
    def tick(self) -> None:
        if self.state == SLEEPING:
            return
        now = self._clock()
        if now - self._busy_checked >= self.BUSY_POLL_S:
            self._busy_checked = now
            if not self.may_listen():
                self.cutter.drop()
                self._sleep()
                return
        if self.state in (WAITING, FOLLOW_UP) and self.cutter.burst is None and now >= self._deadline:
            self._sleep()

    def _may_start(self) -> bool:
        return self._clock() >= self._chime_until and self.may_listen()

    # Whether wake listening is allowed right now.
    def may_listen(self) -> bool:
        if not self.enabled() or self._enrolling:
            return False
        sm = self.sm
        if getattr(sm, 'is_paused', False):
            return False
        commands = getattr(sm, 'voice_command_manager', None)
        if commands is not None and not getattr(commands, 'enabled', True):
            return False      # voice commands are off: nothing to wake for
        try:
            if not sm.is_idle():
                return False
        except Exception:
            return False
        meetings = getattr(sm, 'meetings', None)
        if meetings is not None:
            try:
                if meetings.is_running():
                    return False
            except Exception:
                pass
        return not self._locked()

    def _locked(self) -> bool:
        value, at = self._locked_cache
        now = self._clock()
        if now - at >= self.LOCK_POLL_S:
            try:
                value = bool(self._is_locked())
            except Exception:
                value = True      # can't tell: don't listen on what may be a lock screen
            self._locked_cache = (value, now)
        return value

    def _on_head(self, burst: Burst) -> None:
        if self.state != SLEEPING:
            return       # a command or follow-up burst is handled whole, at its end
        if not self.may_listen():
            return
        rest = self._check_wake(burst.audio(), burst.speech_seconds())
        if rest is None:
            return
        self._woke()
        burst.is_wake = True
        if not burst.over:
            self._enter(AWAKE)                       # still talking: the command follows
        elif rest:
            self._run(text=rest, first=True)         # "Hey Koli next", all inside the head
        else:
            self._enter(WAITING, self._seconds('command_timeout_seconds', 5))

    def _on_end(self, burst: Burst) -> None:
        if burst.is_wake:
            if self.state == AWAKE:
                self._run(audio=burst.audio(), first=True)
            return
        if burst.speech_seconds() < self.MIN_SPEECH_S:
            return
        if self.state == WAITING:
            self._run(audio=burst.audio(), first=True)
        elif self.state == FOLLOW_UP:
            if self.personal() and not self._sounds_like_you(burst.audio()):
                return
            self._run(audio=burst.audio(), first=False)

    # The text after the wake word, or None. Cheapest filter first: length,
    # your voice (personal word), the budget, then Whisper.
    def _check_wake(self, audio: np.ndarray, speech_seconds: float) -> Optional[str]:
        if speech_seconds < self.MIN_SPEECH_S:
            return None
        if self.personal() and not self._sounds_like_you(audio):
            return None
        if not self.budget.take():
            return None
        try:
            text = self._transcribe(audio) or ''
        except Exception as e:
            self.logger.debug(f"Wake check failed: {e}")
            return None
        rest = match_wake(text, self.spellings(), self.match_ratio())
        if rest is None:
            self.budget.missed()
            self.logger.debug("Wake check: not the wake word")
        return rest

    # How much `audio` sounds like your recorded word on this microphone, or
    # None when that can't be told (no recording on this mic, no voiceprint
    # model yet, no meeting service under --legacy-ui).
    def similarity(self, audio: np.ndarray) -> Optional[float]:
        mine = self.wake_voices.get(self._mic())
        if mine is None:
            return None
        try:
            vec = self._voiceprint(audio)
        except Exception as e:
            self.logger.debug(f"Wake voiceprint failed: {e}")
            return None
        if vec is None:
            return None
        vec = np.asarray(vec, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        return float(np.dot(vec / norm, mine)) if norm > 0 else None

    def _sounds_like_you(self, audio: np.ndarray) -> bool:
        score = self.similarity(audio)
        if score is None:
            self._notice_unknown_voice()
            return False
        self.logger.debug(f"Wake word: voice {score:.2f} (needs {self.voice_match():.2f})")
        return score >= self.voice_match()

    # Your word can't be checked: said once per microphone when it isn't
    # recorded there, once per session while the voice model loads.
    def _notice_unknown_voice(self) -> None:
        mic = self._mic()
        if self.wake_voices.get(mic) is None:
            if mic in self._noticed_mics:
                return
            self._noticed_mics.add(mic)
            message = "Your wake word isn't recorded on this microphone yet. Record it in Commands."
        else:
            if self._noticed_loading:
                return
            self._noticed_loading = True
            message = "Your wake word can't be checked yet: the voice model is still loading."
        self.logger.info(f"Wake word: {message}")
        self._publish('notify', {'kind': 'wake', 'message': message})

    # A personal word needs the voiceprint model, which loads on first use:
    # start it as soon as one is set, so your first try after launch works.
    # The meeting service may attach after the listener starts, so keep asking.
    def _warm_voice_model(self) -> None:
        if self._voice_model_ready or not self.personal():
            return
        now = self._clock()
        if now - self._warm_checked < self.WARM_POLL_S:
            return
        self._warm_checked = now
        meetings = getattr(self.sm, 'meetings', None)
        if meetings is None or not hasattr(meetings, 'voiceprint_ready'):
            return
        try:
            self._voice_model_ready = bool(meetings.voiceprint_ready())
        except Exception as e:
            self.logger.debug(f"Wake word: voice model not started ({e})")

    def _mic(self) -> str:
        try:
            return self.sm._mic_key()
        except Exception:
            return 'default'

    def _woke(self) -> None:
        self.budget.woke()
        self.logger.info("Wake word heard")
        try:
            self._play_sound()
        except Exception:
            pass
        self._chime_until = self._clock() + self.CHIME_GUARD_S

    def _run(self, audio=None, text=None, first: bool = True) -> None:
        only_media = not first and not self.personal()
        try:
            outcome = self._run_command(audio=audio, text=text, only_media=only_media, report_miss=first)
        except Exception as e:
            self.logger.warning(f"Wake word: the command failed ({e})")
            outcome = MISSED
        if outcome == RAN:
            self._enter(FOLLOW_UP, self._seconds('follow_up_seconds', 5))
        elif outcome == EMPTY and first:
            self._enter(WAITING, self._seconds('command_timeout_seconds', 5))   # only the wake word was said
        elif first:
            self._sleep()
        # A follow-up that matched nothing (lyrics, talking to someone) keeps its window.

    def _enter(self, state: str, seconds: float = 0.0) -> None:
        self.state = state
        self._deadline = self._clock() + seconds
        self._busy_checked = self._clock()
        self._publish('wake', {'phase': 'follow_up' if state == FOLLOW_UP else 'awake'})

    def _sleep(self) -> None:
        was, self.state = self.state, SLEEPING
        if was != SLEEPING:
            self._publish('wake', {'phase': 'sleeping'})

    # ── Recording your word (setup screen) ───────────────────────────────────

    # What Whisper writes for `audio`, the way a wake check hears it.
    def hear(self, audio: np.ndarray) -> Optional[str]:
        return self._transcribe(audio)

    # One burst of speech from the microphone (your word), or None when
    # nothing was said within `timeout`. Works with the feature off: the tap
    # is added for this one recording. Wake detection pauses meanwhile.
    def capture_word(self, timeout: float = 6.0, max_seconds: float = 3.0) -> Optional[np.ndarray]:
        recorder = getattr(self.sm, 'audio_recorder', None)
        if recorder is None:
            return None
        chunks = collections.deque()
        arrived = threading.Event()

        def tap(chunk, when=None):
            chunks.append(np.asarray(chunk, dtype=np.float32).reshape(-1))
            arrived.set()

        cutter = BurstCutter(head_s=max_seconds, end_silence_s=0.7, max_s=max_seconds)
        speech_prob = self._speech_prob_factory()
        remainder = np.zeros(0, dtype=np.float32)
        give_up = time.monotonic() + timeout
        self._enrolling = True
        self.cutter.drop()
        self._sleep()
        recorder.add_tap(tap)
        try:
            # Waits for speech until `give_up`; a word already started may finish.
            while time.monotonic() < give_up or (cutter.burst is not None and time.monotonic() < give_up + max_seconds):
                arrived.wait(0.1)
                arrived.clear()
                while chunks:
                    audio = np.concatenate([remainder, chunks.popleft()])
                    whole = len(audio) // FRAME * FRAME
                    remainder = audio[whole:]
                    for start in range(0, whole, FRAME):
                        frame = audio[start:start + FRAME]
                        for event, burst in cutter.feed(frame, speech_prob(frame)):
                            if event == 'end' and burst.speech_seconds() >= self.MIN_SPEECH_S:
                                return burst.audio()
            return None
        finally:
            recorder.remove_tap(tap)
            self._enrolling = False

    # ── Default wiring to the app ────────────────────────────────────────────

    def _engine_transcribe(self, audio: np.ndarray) -> Optional[str]:
        engine = getattr(self.sm, 'whisper_engine', None)
        if engine is None or getattr(engine, 'model', None) is None:
            return None
        return engine.transcribe_audio(audio, language=self.sm.wake_language(), use_prompt=False,
                                       beam_size=1, task='transcribe', quiet=True)

    def _meeting_voiceprint(self, audio: np.ndarray):
        meetings = getattr(self.sm, 'meetings', None)
        return meetings.voiceprint_of(audio) if meetings is not None else None

    def _state_manager_run(self, **kwargs) -> str:
        return self.sm.run_wake_command(**kwargs)

    def _wake_sound(self) -> None:
        feedback = getattr(self.sm, 'audio_feedback', None)
        if feedback is not None:
            feedback.play_wake_sound()


def _speech_prob_function():
    from .meeting.capture import speech_prob_function
    return speech_prob_function()


def _platform_is_locked() -> bool:
    from .platform import session
    return session.is_locked()


def _publish(topic, payload):
    from .ui.events import publish
    publish(topic, payload)
