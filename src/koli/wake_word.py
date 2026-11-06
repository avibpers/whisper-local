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
import re
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, List, Optional

import numpy as np

from .dictation_cleanup import MAX_NO_SPEECH
from .utils import get_user_app_data_path, normalize_spoken
from .voice_filter import VOICEPRINT_MATCH

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
# A near match under this that starts one of your commands is that command:
# "desk" is 0.67 like the spelling "adeus", and "Desk 2" woke Koli and asked
# the AI about "two" (2026-11-04). "Viush" for "viush" is 1.0, one letter off 0.89.
CLEAR_WAKE_RATIO = 0.85


def ratio_for(sensitivity: str, personal: bool = False) -> float:
    ratio = SENSITIVITY_RATIO.get(str(sensitivity or '').lower(), SENSITIVITY_RATIO['medium'])
    return ratio - PERSONAL_SLACK if personal else ratio


# The text after the wake word ('' when it was said alone), or None when what
# was heard doesn't start with it. Words are compared with their spaces taken
# out and one word more or less than the spelling, because Whisper writes
# "Hey Koli" as "heykoli" or "hey ko li" too.
# `fragment_letters`: the word more is taken only when it is this short, a
# piece of the split word ("ko li", "via uš"), never a command's first word.
# `command_words`: the words your fixed commands start with (lower case), which
# only a clear match may take for the wake word.
def match_wake(text: str, spellings: Iterable[str], min_ratio: float,
               fragment_letters: Optional[int] = None, command_words: Iterable[str] = ()) -> Optional[str]:
    words = normalize_spoken(text).split()
    ratio, take, _ = _closest(words, spellings, fragment_letters)
    if take is None or ratio < min_ratio:
        return None
    if ratio < CLEAR_WAKE_RATIO and words[0] in command_words:
        return None
    return ' '.join(words[take:])


# How close the start of `text` came to a spelling, and which one: for the
# log line that says why a try didn't wake Koli.
def closest_wake(text: str, spellings: Iterable[str]) -> tuple:
    ratio, _, spelling = _closest(normalize_spoken(text).split(), spellings)
    return ratio, spelling


# (best ratio, words it took, spelling) over every spelling and one word more
# or less than it has (the word more only a short one with `fragment_letters`).
def _closest(words: List[str], spellings: Iterable[str], fragment_letters: Optional[int] = None) -> tuple:
    best_ratio, best_take, best_spelling = 0.0, None, ''
    for spelling in spellings:
        target_words = normalize_spoken(spelling).split()
        if not target_words:
            continue
        target = ''.join(target_words)
        for take in (len(target_words) - 1, len(target_words), len(target_words) + 1):
            if take < 1 or take > len(words):
                continue
            if (fragment_letters is not None and take > len(target_words)
                    and len(words[take - 1]) > fragment_letters):
                continue
            ratio = difflib.SequenceMatcher(None, ''.join(words[:take]), target).ratio()
            if ratio > best_ratio:
                best_ratio, best_take, best_spelling = ratio, take, spelling
    return best_ratio, best_take, best_spelling


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


# Each voiceprint made unit length; zero vectors are left out.
def _unit_vectors(vectors) -> List[np.ndarray]:
    units = []
    for v in vectors:
        v = np.asarray(v, dtype=np.float32)
        norm = float(np.linalg.norm(v))
        if norm > 0:
            units.append(v / norm)
    return units


# How much each of `vectors` sounds like the rest of you: its score against
# `saved` (the stored average over `count` recordings) plus the other new
# recordings, itself left out so it can't vouch for itself.
def leave_one_out_scores(vectors, saved: Optional[np.ndarray] = None, count: int = 0) -> List[Optional[float]]:
    units = []
    for v in vectors:
        v = np.asarray(v, dtype=np.float32)
        norm = float(np.linalg.norm(v))
        units.append(v / norm if norm > 0 else None)
    present = [u for u in units if u is not None]
    if not present:
        return [None] * len(units)
    total = np.sum(present, axis=0)
    if saved is not None and count:
        total = total + np.asarray(saved, dtype=np.float32) * count
    scores = []
    for unit in units:
        rest = None if unit is None else total - unit
        norm = 0.0 if rest is None else float(np.linalg.norm(rest))
        scores.append(None if norm == 0 else float(np.dot(unit, rest / norm)))
    return scores


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

    # How loud you said your word on `mic` (dBFS), or None.
    def level(self, mic: str) -> Optional[float]:
        with self._lock:
            entry = self._read().get(mic or 'default') or {}
        value = entry.get('level_db')
        return float(value) if isinstance(value, (int, float)) else None

    # Keeps the average of `vectors` (one per recording) for `mic`, and how
    # loud you said it, so quieter sound there isn't taken for you.
    def set(self, mic: str, vectors: List[np.ndarray], level_db: Optional[float] = None) -> None:
        units = _unit_vectors(vectors)
        if not units:
            return
        centroid = np.mean(units, axis=0)
        with self._lock:
            data = self._read()
            data[mic or 'default'] = {'centroid': [round(float(x), 6) for x in centroid], 'count': len(units),
                                      'saved': time.strftime('%Y-%m-%dT%H:%M:%S')}
            if level_db is not None:
                data[mic or 'default']['level_db'] = round(float(level_db), 1)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data), encoding='utf-8')

    # Adds more recordings to your voiceprint on `mic` instead of replacing
    # it: the average grows by how many recordings each side holds, so three
    # more in today's noise move it as much as the first three set it.
    # The level keeps the louder one. Returns False when nothing is saved on `mic` yet.
    def add(self, mic: str, vectors: List[np.ndarray], level_db: Optional[float] = None) -> bool:
        units = _unit_vectors(vectors)
        if not units:
            return False
        with self._lock:
            data = self._read()
            entry = data.get(mic or 'default')
            if not entry or not entry.get('centroid'):
                return False
            count = int(entry.get('count') or 1)
            old = np.asarray(entry['centroid'], dtype=np.float32)
            if old.shape != units[0].shape:
                count, old = 0, np.zeros_like(units[0])   # made with another speaker model: start over
            centroid = (old * count + np.sum(units, axis=0)) / (count + len(units))
            entry.update(centroid=[round(float(x), 6) for x in centroid], count=count + len(units),
                         saved=time.strftime('%Y-%m-%dT%H:%M:%S'))
            # The level only rises: recordings from further away (-24 dB
            # against -16, 2026-10-16) lowered the gate and let the room in.
            if level_db is not None:
                old_level = entry.get('level_db')
                entry['level_db'] = round(float(level_db) if old_level is None else
                                          max(float(old_level), float(level_db)), 1)
            self.path.write_text(json.dumps(data), encoding='utf-8')
        return True

    # How many recordings make your voiceprint on `mic` (0 when none).
    def count(self, mic: str) -> int:
        with self._lock:
            entry = self._read().get(mic or 'default') or {}
        return int(entry.get('count') or 0) if entry.get('centroid') else 0

    # The saved average on `mic` as it is stored (not made unit length), with
    # its count: what new recordings are weighed against.
    def raw(self, mic: str) -> tuple:
        with self._lock:
            entry = self._read().get(mic or 'default') or {}
        if not entry.get('centroid'):
            return None, 0
        return np.asarray(entry['centroid'], dtype=np.float32), int(entry.get('count') or 1)

    def mics(self) -> List[str]:
        with self._lock:
            return list(self._read())

    # Your voiceprint on every microphone it is recorded on: {mic: unit vector}.
    def all(self) -> dict:
        with self._lock:
            data = self._read()
        out = {}
        for mic, entry in data.items():
            if isinstance(entry, dict) and entry.get('centroid'):
                vec = np.asarray(entry['centroid'], dtype=np.float32)
                norm = float(np.linalg.norm(vec))
                if norm > 0:
                    out[mic] = vec / norm
        return out

    # The saved average (as stored) and count of the microphone with the most
    # recordings whose voiceprints have `shape` (the same speaker model),
    # other than `exclude`: what recordings on a new microphone are weighed
    # against. (None, 0) when there is none.
    def reference(self, shape, exclude: str = '') -> tuple:
        with self._lock:
            data = self._read()
        best = (None, 0)
        for mic, entry in data.items():
            if mic == exclude or not isinstance(entry, dict) or not entry.get('centroid'):
                continue
            vec = np.asarray(entry['centroid'], dtype=np.float32)
            count = int(entry.get('count') or 1)
            if vec.shape == tuple(shape) and count > best[1]:
                best = (vec, count)
        return best

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
        self.max_frames = 0       # cut here even if it never goes quiet (set by the cutter)
        self.paused = False       # the first short pause was reported
        self.checks = 0           # wake checks run on this burst
        self.checked_frames = 0   # speech frames it had at the last check
        self.wake_frames = 0      # speech frames when it woke Koli
        self.wake_rest = ''       # what followed the wake word in that check
        self.peeks = 0            # quick fixed-command checks at its short pauses

    def audio(self) -> np.ndarray:
        return np.concatenate(self.frames) if self.frames else np.zeros(0, dtype=np.float32)

    def speech_seconds(self) -> float:
        return self.speech_frames * FRAME_S


# Frames in, ('pause' | 'head' | 'quiet' | 'end', burst) events out. A burst starts on
# a speech frame (with PREROLL_S before it, so the first sound isn't clipped)
# when `may_start()` agrees, and ends after END_SILENCE_S of silence or at
# MAX_S. 'pause' comes once, at the first PAUSE_S of silence after speech
# (the wake word said on its own is checked then, not 0.6 s after it);
# 'head' once, at HEAD_S or at the end if the burst is shorter (one breath,
# "Hey Koli next song", is checked while you are still talking). 'quiet'
# (only after set_quiet) at every pause that long, while the burst goes on:
# a long AI sentence ends at a 2 s pause, and its short pauses are where a
# fixed command can be caught early.
class BurstCutter:
    SPEECH = 0.5
    HEAD_S = 1.2
    PAUSE_S = 0.3
    END_SILENCE_S = 0.6
    MAX_S = 8.0
    PREROLL_S = 0.3

    def __init__(self, head_s: float = HEAD_S, end_silence_s: float = END_SILENCE_S, max_s: float = MAX_S):
        self.head_frames = max(1, int(round(head_s / FRAME_S)))
        self.pause_frames = max(1, int(round(self.PAUSE_S / FRAME_S)))
        self.end_frames = max(1, int(round(end_silence_s / FRAME_S)))
        self.max_frames = max(self.head_frames, int(round(max_s / FRAME_S)))
        self._preroll = collections.deque(maxlen=max(1, int(round(self.PREROLL_S / FRAME_S))))
        self.burst: Optional[Burst] = None
        self.quiet_frames = 0          # 0: no 'quiet' events

    def feed(self, frame: np.ndarray, prob: float, may_start: Callable[[], bool] = lambda: True) -> list:
        speech = prob >= self.SPEECH
        burst = self.burst
        if burst is None:
            if speech and may_start():
                burst = self.burst = Burst(self._preroll)
                burst.max_frames = self.max_frames
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
        over = burst.silence_frames >= self.end_frames or len(burst.frames) >= burst.max_frames
        if not burst.paused and not over and burst.silence_frames >= self.pause_frames and burst.speech_frames:
            burst.paused = True
            events.append(('pause', burst))
        if self.quiet_frames and not over and burst.silence_frames == self.quiet_frames and burst.speech_frames:
            events.append(('quiet', burst))
        if over:
            burst.over = True
            self.burst = None
        if not burst.head_done and (over or len(burst.frames) >= self.head_frames):
            burst.head_done = True
            events.append(('head', burst))
        if over:
            events.append(('end', burst))
        return events

    # How long new bursts may run before they are cut.
    def set_max_seconds(self, seconds: float) -> None:
        self.max_frames = max(self.head_frames, int(round(seconds / FRAME_S)))

    # The silence that ends a burst, from now on (the one in progress too).
    def set_end_silence(self, seconds: float) -> None:
        self.end_frames = max(1, int(round(seconds / FRAME_S)))

    # The pause that sends a 'quiet' event (0 = none).
    def set_quiet(self, seconds: float) -> None:
        self.quiet_frames = max(1, int(round(seconds / FRAME_S))) if seconds else 0

    # Cut the burst in progress `seconds` from now at the latest.
    def cut_after(self, seconds: float) -> None:
        if self.burst is not None:
            self.burst.max_frames = min(self.burst.max_frames, len(self.burst.frames) + int(round(seconds / FRAME_S)))

    # Let the burst in progress run `seconds` more, even past its first cut.
    def run_for(self, seconds: float) -> None:
        if self.burst is not None:
            self.burst.max_frames = len(self.burst.frames) + int(round(seconds / FRAME_S))

    # Forget the burst in progress (Koli got busy, the queue fell behind).
    def drop(self) -> None:
        self.burst = None
        self._preroll.clear()


# ── The listener ─────────────────────────────────────────────────────────────

SLEEPING, AWAKE, WAITING, FOLLOW_UP = 'sleeping', 'awake', 'waiting', 'follow_up'
# The conversation with the agent (agent/chat.py): open after what you say
# following "Anything else?", or when the AI took a command, and only with your
# own word, whose voice is checked. It lasts as long as the conversation does.
CHAT = 'chat'

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
    # Commands are short: whatever keeps sounding (music, a TV), the command
    # is cut this long after the wake word, and a command said after a pause
    # (or a follow-up) is cut at COMMAND_MAX_S, never at the 8 s burst cut.
    COMMAND_AFTER_WAKE_S = 4.0
    COMMAND_MAX_S = 4.0
    # A command is short and you've just been asked for it: a shorter pause
    # ends it than the 0.6 s that ends a burst while Koli sleeps.
    COMMAND_END_SILENCE_S = 0.45
    # In the conversation a sentence ends at a shorter pause than a first
    # command's 2 s: it's a reply, and you're waiting for Koli's.
    CHAT_END_S = 1.5
    # When the AI can take it (your own word, AI actions on), a command may
    # be a whole sentence: it runs while you talk, up to SENTENCE_MAX_S, and
    # ends at a SENTENCE_END_S pause. A fixed command is still caught at its
    # first short pause (COMMAND_END_SILENCE_S), when what you said so far is
    # exactly one of its phrases; at most QUICK_PEEKS of those per sentence.
    SENTENCE_END_S = 2.0
    SENTENCE_MAX_S = 20.0
    QUICK_PEEKS = 2
    # Wake checks per burst: at its first short pause (or the head), and once
    # more at its end when it grew since ("Hey… Via" with a gap). Bursts
    # longer than RECHECK_MAX_S are talk, not a wake word: no second check.
    MAX_CHECKS_PER_BURST = 2
    RECHECK_MAX_S = 3.0
    # Sound quieter than your voice by more than GATE_MARGIN_DB is silence:
    # music from the speakers reads as speech to TEN VAD and would hold every
    # burst open. Judged like the dictation detector (RecentLoudness: the 0.3 s
    # level, held up by its loud end over LOUD_END_S). The margin is the wake
    # word's own, and wide: your level is the loud end of your recordings
    # (-13.5 dB on a real EMEET), the 0.3 s average of normal speech sits
    # 5-13 dB under it, and speaker music 25+ dB. "Only my voice"'s margin
    # (set to 4 dB there) took every try for background. On the plain 0.3 s
    # level a third to two thirds of normal speech still fell under the line
    # (BY-V20, 2026-10-19: 580 "taken as background" lines); a 0.3 s hold lets
    # half to four fifths of it through. The hold is short: over music a burst
    # ends only once it runs out, and a longer one (0.4 s and up) made "next
    # song" wait over a second after you stopped.
    LOUD_END_S = 0.3
    FLOOR_CACHE_S = 5.0
    GATE_MARGIN_DB = 15.0
    GATED_LOG_S = 30.0        # at most one "taken as background" line this often
    DEFAULT_VOICE_MATCH = 0.45
    # How sure Whisper must be of the wake word (its log-prob). A backstop
    # only: your real "Viush" scored -0.60 to -1.18 and noise read as it
    # -0.99 to -1.31 (2026-10-16), so this can't tell them apart; your
    # voice does (you 0.63-0.75, the noise 0.01-0.27 once well recorded).
    WAKE_MIN_LOGPROB = -1.5
    # The command is read again in your language, so the wake word at its
    # start can come out another way than the wake check heard it ("¡Dios!"
    # woke Koli, the command read "Views increase volume…", 0.60 like
    # "viush"). When the command's burst is known to start with the word,
    # this looser match drops it.
    WAKE_IN_COMMAND_RATIO = 0.5
    # A command said after a pause drops a first word only this much like a
    # spelling: the wake word said again, not a command word near one.
    STRIP_AFTER_PAUSE_RATIO = 0.8
    # That looser match takes a word more than the spelling only when it is a
    # piece of the split wake word: "Viaš, Desk tu" lost "Desk" to "adeus"
    # (0.62 for "viašdesk", 2026-10-20) and the AI got "tu".
    WAKE_FRAGMENT_LETTERS = 3
    # The console shows tries that are probably you: a voice this close under
    # "Sounds like you", or (with "Hey Koli", where every burst reaches
    # Whisper) text this much like the wake word. Other voices and a TV
    # score far below both, so they stay out of it.
    CONSOLE_NEAR_VOICE = 0.10
    CONSOLE_NEAR_WORD = 0.5

    def __init__(self, state_manager, config_manager, *,
                 transcribe: Optional[Callable] = None,
                 voiceprint: Optional[Callable] = None,
                 run_command: Optional[Callable] = None,
                 wake_voices: Optional[WakeVoices] = None,
                 speech_prob: Optional[Callable] = None,
                 is_locked: Optional[Callable[[], bool]] = None,
                 publish: Optional[Callable] = None,
                 play_sound: Optional[Callable[[], None]] = None,
                 frame_level: Optional[Callable[[np.ndarray], float]] = None,
                 learned_level: Optional[Callable[[str], Optional[float]]] = None,
                 ai_listens: Optional[Callable[[], bool]] = None,
                 quick_command: Optional[Callable] = None,
                 my_voice: Optional[Callable] = None,
                 chat_open: Optional[Callable[[], bool]] = None,
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
        self._frame_level = frame_level or _frame_level_db
        self._learned_level = learned_level or self._state_manager_level
        self._ai_listens = ai_listens or self._state_manager_ai
        self._quick_command = quick_command or self._state_manager_quick
        self._my_voice = my_voice or self._meeting_my_voice
        self._chat_open = chat_open or self._state_manager_chat_open
        self._sentence = False       # this command may be a sentence for the AI
        self._last_pieces = None     # Whisper's pieces with its numbers, from the last wake check
        self._wake_heard = ''        # the wake word as the last wake check wrote it ("dios")
        self._wake_in_command = False   # the command being run starts with the wake word
        from .voice_activity_detection import RecentLoudness
        self._loudness = RecentLoudness(FRAME_S, self.LOUD_END_S)
        self._window_db = -200.0
        self._gated_frames, self._gated_peak = 0, -200.0
        self._gated_summary = (0, 0.0, -200.0, -1e9)   # (stretches, seconds, peak dB, last logged)
        self._floor_cache = (None, None, -1e9)     # (mic, floor dB, when)
        self._clock = clock
        self.budget = CheckBudget(clock)
        self.cutter = BurstCutter()
        self.state = SLEEPING
        self._phase = 'sleeping'     # what the bubble was last told
        self._deadline = 0.0
        self._chime_until = 0.0
        self._retry_left = False     # a missed first command may be said once more
        self._locked_cache = (False, -1e9)
        self._busy_checked = -1e9
        self._enrolling = False
        self._noticed_mics = set()
        self._other_model_mics = set()     # wake recordings made with another speaker model
        self._borrowed_mics = set()        # microphones checked against another one's recording (logged once)
        self._stale_mics = set()           # their own recording is from another speaker model
        self._noticed_loading = False
        self._voice_model_ready = False
        self._warm_checked = -1e9
        # Recordings of your word on the setup screen, until saved: {'heard', 'voiceprint'}.
        self.recordings: list = []
        # How you spell the word being recorded ("Hey Avia"), if you typed it.
        self.recording_spelling = ''
        # True while recordings are being added to your saved word, not making a new one.
        self.recording_more = False
        self._hint_override: Optional[str] = None
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

    # The language the wake check reads the word in, from how it is spelled:
    # Hebrew letters → 'he', Latin → 'en', anything else None (detected).
    # Telling Whisper skips its language detection, a second pass over the
    # audio (2026-10-14: 106 ms → 58 ms on an idle GPU, ~0.4 s saved in the
    # running app). The command after it is read in your own language.
    def wake_language(self) -> Optional[str]:
        word = str(self._cfg().get('spelling') or '').strip() or self.spellings()[0]
        if re.search(r'[֐-׿]', word):
            return 'he'
        letters = re.findall(r'[^\W\d_]', word)
        if letters and all('a' <= c.lower() <= 'z' for c in letters):
            return 'en'
        return None

    # Your own word that must sound like you.
    def personal(self) -> bool:
        cfg = self._cfg()
        return bool(cfg.get('only_my_voice')) and bool(cfg.get('phrases'))

    def match_ratio(self) -> float:
        return ratio_for(self._cfg().get('sensitivity', 'medium'), personal=self.personal())

    # Whether a first command may be a whole sentence: only your own word
    # reaches the AI ("Hey Koli" is anyone's), and only with AI actions on.
    def _sentence_mode(self) -> bool:
        try:
            return self.personal() and bool(self._ai_listens())
        except Exception:
            return False

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
        loud = self._loud_enough(frame)      # every frame, so the level window stays current
        if prob >= BurstCutter.SPEECH and not loud:
            prob = 0.0           # speech-like, but much quieter than you: music, a TV across the room
            self._gated_frames += 1
            self._gated_peak = max(self._gated_peak, self._window_db)
        elif self._gated_frames:
            self._log_gated()
        for event, burst in self.cutter.feed(frame, prob, may_start=self._may_start):
            if event == 'end':
                self._on_end(burst)
            elif event == 'quiet':
                self._peek(burst)
            else:
                self._maybe_wake(burst, event)
        self.tick()

    # Time-based moves: the wait for a command or a follow-up runs out, or
    # Koli got busy (hotkey, meeting) in the middle of a wake.
    def tick(self) -> None:
        if self.state == SLEEPING:
            if self._in_chat() and self.personal() and self.may_listen():
                self._enter(CHAT)      # opened by the command hotkey's AI action
            return
        now = self._clock()
        if now - self._busy_checked >= self.BUSY_POLL_S:
            self._busy_checked = now
            if not self.may_listen():
                self.cutter.drop()
                self._sleep()
                return
        # The conversation ends when its window does (agent/chat.py keeps the time).
        if self.state == CHAT and not self._in_chat():
            self._sleep()
            return
        if self.state in (WAITING, FOLLOW_UP) and self.cutter.burst is None and now >= self._deadline:
            self._sleep()

    # Whether the sound around now is loud enough to be you (RecentLoudness)
    # against your level less the margin. True when your level on this
    # microphone isn't known.
    def _loud_enough(self, frame: np.ndarray) -> bool:
        self._window_db = self._loudness.add(self._frame_level(frame))
        floor = self.voice_floor_db()
        return floor is None or self._window_db >= floor

    # A stretch of speech the gate took for background, said once it ends:
    # if it was you, the floor is too high for how you speak now.
    # Summed over GATED_LOG_S: with music playing a stretch ends every second
    # or two, and a line each (135 in ten minutes) re-rendered the app window
    # on every one, which threw away what you were typing in Snippets.
    def _log_gated(self) -> None:
        seconds = self._gated_frames * FRAME_S
        self._gated_frames = 0
        if seconds < self.MIN_SPEECH_S:
            return
        # Awake, this was most likely the command: say it was too quiet.
        if self.state != SLEEPING:
            floor = self.voice_floor_db()
            _console(f"✗ Too quiet for a command: {seconds:.1f} s of speech up to {self._gated_peak:.0f} dB, "
                     f"needs {floor if floor is not None else 0:.0f} dB")
        count, total, peak, since = self._gated_summary
        count, total, peak = count + 1, total + seconds, max(peak, self._gated_peak)
        self._gated_peak = -200.0
        now = self._clock()
        if now - since < self.GATED_LOG_S:
            self._gated_summary = (count, total, peak, since)
            return
        floor = self.voice_floor_db()
        self.logger.info(f"Wake word: {total:.1f} s of speech in {count} stretch(es) taken as background "
                         f"(up to {peak:.0f} dB, needs {floor if floor is not None else 0:.0f} dB)")
        self._gated_summary = (0, 0.0, -200.0, now)

    # Your level on this microphone less the margin (dBFS), or None. Your
    # recorded wake word sets the level; otherwise the one learned from hotkey
    # dictations ("Only my voice"). Looked up every FLOOR_CACHE_S.
    def voice_floor_db(self) -> Optional[float]:
        mic, floor, at = self._floor_cache
        now = self._clock()
        current = self._mic()
        if current == mic and now - at < self.FLOOR_CACHE_S:
            return floor
        level = self.wake_voices.level(current)
        if level is None:
            try:
                level = self._learned_level(current)
            except Exception:
                level = None
        floor = None if level is None else float(level) - self.GATE_MARGIN_DB
        self._floor_cache = (current, floor, now)
        return floor

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

    # A wake check on a burst while Koli sleeps, at its first short pause, its
    # head or its end (`when`). Skipped when nothing new was said since the
    # last check, after MAX_CHECKS_PER_BURST, and for a second check on a
    # burst too long to be a wake word.
    def _maybe_wake(self, burst: Burst, when: str) -> None:
        if self.state != SLEEPING or burst.is_wake:
            return       # a command or follow-up burst is handled whole, at its end
        if burst.speech_frames <= burst.checked_frames or burst.checks >= self.MAX_CHECKS_PER_BURST:
            return
        if burst.checks and burst.speech_seconds() > self.RECHECK_MAX_S:
            return
        if burst.speech_seconds() < self.MIN_SPEECH_S or not self.may_listen():
            return
        burst.checks += 1
        burst.checked_frames = burst.speech_frames
        silence = burst.silence_frames * FRAME_S
        rest = self._check_wake(burst.audio(), burst.speech_seconds(),
                                when=f"at a {silence:.2f} s pause" if silence else f"while talking ({when})")
        if rest is None:
            return
        self._woke()
        burst.is_wake = True
        burst.wake_frames, burst.wake_rest = burst.speech_frames, rest
        if not burst.over:
            self._enter(AWAKE)                       # the command may follow in this burst
            if self._sentence:
                self.cutter.run_for(self.SENTENCE_MAX_S)
            else:
                self.cutter.cut_after(self.COMMAND_AFTER_WAKE_S)
        elif rest:
            self._run(audio=burst.audio(), first=True, has_wake=True)   # "Hey Koli next", read in your language
        else:
            self._enter(WAITING, self._seconds('command_timeout_seconds', 5))

    # A short pause in a sentence: when what you said so far is exactly one
    # of your fixed commands ("next song"), it runs now instead of 2 s later,
    # and the rest of the burst is dropped. Anything else keeps listening.
    def _peek(self, burst: Burst) -> None:
        if not self._sentence or self.state not in (AWAKE, WAITING) or burst.peeks >= self.QUICK_PEEKS:
            return
        if self.state == AWAKE and not burst.is_wake:
            return
        said_frames = burst.speech_frames - (burst.wake_frames if burst.is_wake else 0)
        if said_frames < self.MIN_SPEECH_S / FRAME_S and not burst.wake_rest:
            return       # only the wake word so far
        burst.peeks += 1
        # A command after a pause wasn't voice-checked as part of the wake word.
        if not burst.is_wake and not self._sounds_like_you(burst.audio()):
            return
        self._wake_in_command = burst.is_wake
        try:
            outcome = self._quick_command(burst.audio())
        except Exception as e:
            self.logger.debug(f"Wake word: quick command check failed ({e})")
            return
        finally:
            self._wake_in_command = False
        if outcome == RAN:
            self.cutter.drop()
            self._after_command()

    def _on_end(self, burst: Burst) -> None:
        if burst.is_wake:
            if self.state == AWAKE:
                said_more = burst.speech_frames - burst.wake_frames >= self.MIN_SPEECH_S / FRAME_S
                if burst.wake_rest or said_more:
                    self._run(audio=burst.audio(), first=True, has_wake=True)
                else:
                    # Only the wake word, already read: wait for the command.
                    self._enter(WAITING, self._seconds('command_timeout_seconds', 5))
            return
        if self.state == SLEEPING:
            self._maybe_wake(burst, 'end')
            if burst.is_wake:
                return
        if burst.speech_seconds() < self.MIN_SPEECH_S:
            return
        # With your own word, what follows it has to be you too: noise after
        # a wake turned into "Via" (2026-10-16). Not you, Koli keeps waiting.
        if self.personal() and self.state in (WAITING, FOLLOW_UP, CHAT) \
                and not self._sounds_like_you(burst.audio(), say=self.state == WAITING):
            return
        if self.state == WAITING:
            self._run(audio=burst.audio(), first=True)
        elif self.state in (FOLLOW_UP, CHAT):
            self._run(audio=burst.audio(), first=False)

    # The text after the wake word, or None. Cheapest filter first: length,
    # your voice (personal word), the budget, then Whisper.
    # Each try gets one log line saying why it did or didn't wake Koli; what
    # Whisper heard only with log_transcriptions on.
    # `when` says where in the burst it ran ("at a 0.30 s pause"), and the
    # line ends with how long Whisper took: what the chime waits for.
    def _check_wake(self, audio: np.ndarray, speech_seconds: float, when: str = '') -> Optional[str]:
        if speech_seconds < self.MIN_SPEECH_S:
            return None
        said = f"Wake check ({speech_seconds:.1f} s of speech{', ' + when if when else ''})"
        voice = ''      # the score, for the console
        if self.personal():
            score = self.similarity(audio)
            if score is None:
                self._notice_unknown_voice()
                self.logger.info(f"{said}: no - your voice can't be checked on this microphone yet")
                return None
            said += f", voice {score:.2f} (needs {self.voice_match():.2f})"
            voice = f" (voice {score:.2f}, needs {self.voice_match():.2f})"
            if score < self.voice_match():
                self.logger.info(f"{said}: no - doesn't sound enough like your recordings")
                if score >= self.voice_match() - self.CONSOLE_NEAR_VOICE:
                    _console(f"👂 Almost your voice{voice}: not woken")
                return None
        if not self.budget.take():
            self.logger.info(f"{said}: skipped - checking less often after many misses")
            if self.personal():
                _console(f"👂 Sounded like you{voice}, but skipped: checking less often after many misses")
            return None
        started = time.monotonic()
        self._last_pieces = None
        try:
            text = self._transcribe(audio) or ''
        except Exception as e:
            self.logger.info(f"{said}: Whisper failed ({e})")
            return None
        took_ms = (time.monotonic() - started) * 1000
        rest = match_wake(text, self.spellings(), self.match_ratio(), command_words=self._command_words())
        ratio, spelling = closest_wake(text, self.spellings())
        heard = f" “{text}”" if self._log_text() else f" {len(text.split())} word(s)"
        said += (f", heard{heard} in {took_ms:.0f} ms, {ratio:.2f} like “{spelling}” "
                 f"(needs {self.match_ratio():.2f})")
        sure = self._whisper_sure()
        if sure is not None:
            said += f", sure {sure[1]}"
        # Told to expect your word, Whisper writes it for noise too (2026-10-16:
        # nothing said, "Viush" heard, voice 0.26): its own numbers then say
        # it was guessing.
        if rest is not None and sure is not None and not sure[0]:
            self.budget.missed()
            self.logger.info(f"{said}: no - Whisper wasn't sure of the words")
            _console(f"👂 Heard “{text}”{voice}, but Whisper wasn't sure ({sure[1]}): not woken")
            return None
        if rest is None:
            self.budget.missed()
            near_command = ratio >= self.match_ratio()   # near enough, but a command starts this way
            self.logger.info(f"{said}: no{' - one of your commands starts this way' if near_command else ''}")
            # Your voice, or close to the wake word: show what Whisper wrote.
            if self.personal() or ratio >= self.CONSOLE_NEAR_WORD:
                _console(f"👂 Heard “{text or '…'}”{voice}: not “{spelling}” ({ratio:.2f} alike, "
                         f"needs {self.match_ratio():.2f})")
        else:
            self.logger.info(f"{said}: woke")
            _console(f"👂 Heard “{text}”{voice}: woke, listening for the command")
            words = normalize_spoken(text).split()
            self._wake_heard = ' '.join(words[:len(words) - len(rest.split())])
        return rest

    # `text` (a command as read in your language) without the wake word at
    # its start. Your spellings first; when the command's audio is known to
    # start with the word, also what the wake check heard, more loosely.
    def strip_wake(self, text: str) -> str:
        # A command said after a pause is its own audio: it starts with the
        # wake word only if you said it again, so only a word that clearly is
        # it goes ("desk" is 0.67 like "adeus", and "Desk 6" lost its "Desk").
        ratio = self.match_ratio() if self._wake_in_command else max(self.match_ratio(), self.STRIP_AFTER_PAUSE_RATIO)
        command_words = self._command_words()
        rest = match_wake(text, self.spellings(), ratio, command_words=command_words)
        if rest is None and self._wake_in_command:
            heard = [self._wake_heard] if self._wake_heard else []
            rest = match_wake(text, self.spellings() + heard, self.WAKE_IN_COMMAND_RATIO,
                              fragment_letters=self.WAKE_FRAGMENT_LETTERS, command_words=command_words)
            if rest is not None:
                self.logger.info("Wake word: dropped from the start of the command (read another way)")
        if rest is None:
            return text
        # The command keeps its own words ("30%"); match_wake's are normalised.
        tokens = text.split()
        for start in range(len(tokens) + 1):
            if normalize_spoken(' '.join(tokens[start:])) == rest:
                return ' '.join(tokens[start:])
        return rest

    # Whisper's own numbers for the last wake check: (sure enough, the numbers
    # for the log), or None when the engine gave none (whisper.cpp, tests).
    # Sure enough: some piece above WAKE_MIN_LOGPROB and not rated silence.
    def _whisper_sure(self) -> Optional[tuple]:
        pieces = self._last_pieces
        if not pieces:
            return None
        no_speech = max(p.get('no_speech_prob', 0) for p in pieces)
        log_prob = min(p.get('avg_logprob', 0) for p in pieces)
        sure = any(p.get('no_speech_prob', 0) <= MAX_NO_SPEECH and p.get('avg_logprob', 0) >= self.WAKE_MIN_LOGPROB
                   for p in pieces)
        return sure, f"no-speech {no_speech:.2f} / log-prob {log_prob:.2f}"

    # The words your fixed commands start with, or nothing when they can't be told.
    def _command_words(self) -> set:
        commands = getattr(self.sm, 'voice_command_manager', None)
        try:
            return set(commands.first_words()) if commands is not None else set()
        except Exception:
            return set()

    def _log_text(self) -> bool:
        logging_cfg = (getattr(self.cm, 'config', None) or {}).get('logging') or {}
        return bool(logging_cfg.get('log_transcriptions'))

    # How much `audio` sounds like your recorded word, or None when that can't
    # be told (nothing recorded, no voiceprint model yet, no meeting service
    # under --legacy-ui). This microphone's recording first; a microphone
    # without its own is checked against your other microphones' (the best
    # of them), so a new microphone works without teaching it again.
    # `borrow=False` for what guards something (the spoken password, your
    # answer to the AI): only this microphone's own recording counts there,
    # since the best of several microphones is a looser match.
    def similarity(self, audio: np.ndarray, borrow: bool = True) -> Optional[float]:
        mic = self._mic()
        recorded = self.wake_voices.all()
        if not recorded:
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
        if norm == 0:
            return None
        vec = vec / norm
        # Recordings made with another voiceprint model (meeting.speaker_model
        # changed) have another size and can't be compared.
        usable = {m: v for m, v in recorded.items() if v.shape == vec.shape}
        if not usable:
            self._other_model_mics.add(mic)
            return None
        self._other_model_mics.discard(mic)
        if mic in usable:
            self._stale_mics.discard(mic)
            return float(np.dot(vec, usable[mic]))
        if mic in recorded:
            self._stale_mics.add(mic)
        if not borrow:
            return None
        best_mic, best = max(((m, float(np.dot(vec, v))) for m, v in usable.items()), key=lambda x: x[1])
        if mic not in self._borrowed_mics:
            self._borrowed_mics.add(mic)
            if mic in self._stale_mics:
                self._notice_stale_recording(mic, best_mic)
            else:
                self.logger.info(f"Wake word: no recording on {mic or 'this microphone'} - checking against your "
                                 f"other microphones' (best: {best_mic})")
        return best

    # Your recording on `mic` can't be compared since the speaker model changed.
    def recording_is_stale(self, mic: str) -> bool:
        return mic in self._stale_mics

    # Said once a session: other microphones' recordings of your word miss you
    # far more often, and nothing else would tell you why.
    def _notice_stale_recording(self, mic: str, best_mic: str) -> None:
        self.logger.info(f"Wake word: the recording on {mic or 'this microphone'} is from another speaker model - "
                         f"checking against your other microphones' (best: {best_mic})")
        self._publish('notify', {'kind': 'wake', 'message':
                                 f"Your wake word on {mic or 'this microphone'} was recorded with another speaker "
                                 f"model, so it can miss you. Record it again in Commands."})

    # Is this command or answer (other words than your wake word) you?
    # (True, False or None when it can't be told, and the scores for the log.)
    # Your recording holds one word, so other words in your voice score low
    # against it: on real dictations (BY-V20, 2026-11-04) 25% passed 0.40, while
    # 98% passed against the voice your dictations taught this microphone and
    # 17 other people stayed under 0.30. Either one is enough. Your word itself
    # and the spoken password still go by the recording alone (`similarity`).
    def judge_speech(self, audio: np.ndarray) -> tuple:
        word = self.similarity(audio)
        said = '' if word is None else f"voice {word:.2f} (needs {self.voice_match():.2f})"
        if word is not None and word >= self.voice_match():
            return True, said
        dictated = self._dictated_voice(audio)
        if dictated is not None:
            said = f"{said}, " if said else ''
            said += f"as you dictate {dictated:.2f} (needs {VOICEPRINT_MATCH:.2f})"
            if dictated >= VOICEPRINT_MATCH:
                return True, said
        return (None if word is None and dictated is None else False), said

    # How much `audio` sounds like the voice your dictations taught this
    # microphone, or None when it can't be told.
    def _dictated_voice(self, audio: np.ndarray) -> Optional[float]:
        try:
            return self._my_voice(audio, self._mic())
        except Exception as e:
            self.logger.debug(f"Wake word: dictated voice not checked ({e})")
            return None

    # A command after your word: is it your voice? `say` puts a skipped one
    # on the console (the first command, which you're waiting on; a
    # follow-up window hears the room and stays quiet).
    def _sounds_like_you(self, audio: np.ndarray, say: bool = False) -> bool:
        mine, said = self.judge_speech(audio)
        if mine is None:
            self._notice_unknown_voice()
            return False
        self.logger.info(f"Wake word: command {said}{'' if mine else ' - not you, skipped'}")
        if not mine and say:
            _console(f"✗ Not your voice ({said}): command skipped")
        return mine

    # Your word can't be checked: said once per microphone when it isn't
    # recorded there, once per session while the voice model loads.
    def _notice_unknown_voice(self) -> None:
        mic = self._mic()
        if not self.wake_voices.mics() or mic in self._other_model_mics:
            if mic in self._noticed_mics:
                return
            self._noticed_mics.add(mic)
            message = ("Your wake word was recorded with another speaker model. Record it again in Commands, "
                       "or set Meetings › Speaker model back." if mic in self._other_model_mics
                       else "Your wake word isn't recorded yet. Record it in Commands.")
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
        self._retry_left = True
        self.logger.info("Wake word heard")
        try:
            self._play_sound()
        except Exception:
            pass
        self._chime_until = self._clock() + self.CHIME_GUARD_S

    # A first command that matches nothing gets one more try without the wake
    # word ("Didn't catch that - say it again"); only one, so a TV that keeps
    # talking can't hold Koli awake.
    # `has_wake`: the audio starts with the wake word (said in one breath), so
    # strip_wake may drop it more loosely.
    def _run(self, audio=None, text=None, first: bool = True, has_wake: bool = False) -> None:
        only_media = not first and not self.personal()
        can_retry = first and self._retry_left
        # What you say after "Anything else?", with your own word, is the
        # conversation: anything goes to the AI, and the window opens.
        chat = not first and self.personal()
        self._wake_in_command = has_wake
        try:
            extra = {'chat': True} if chat else {}
            outcome = self._run_command(audio=audio, text=text, only_media=only_media, report_miss=first,
                                        can_retry=can_retry, **extra)
        except Exception as e:
            self.logger.warning(f"Wake word: the command failed ({e})")
            outcome, can_retry = MISSED, False
        finally:
            self._wake_in_command = False
        if self._in_chat():
            self._enter(CHAT)
            return
        if outcome == RAN:
            self._after_command()
        elif outcome == EMPTY and first:
            self._enter(WAITING, self._seconds('command_timeout_seconds', 5))   # only the wake word was said
        elif can_retry and outcome == MISSED:
            self._retry_left = False
            self.logger.info("Wake word: no command matched, listening for one more try")
            self._enter(WAITING, self._seconds('command_timeout_seconds', 5))
        elif first:
            self._sleep()
        # A follow-up that matched nothing (lyrics, talking to someone) keeps its window.

    def _enter(self, state: str, seconds: float = 0.0) -> None:
        self.state = state
        self._deadline = self._clock() + seconds
        # A command after a pause, or a follow-up, is short: cut it soon, and
        # end it at a shorter pause. A first command the AI may take is a
        # sentence: it runs while you talk and ends at a long pause, with
        # quick checks for a fixed command at the short ones. (Follow-ups
        # never reach the AI, so they stay short.)
        # A follow-up may be a sentence for the AI too now: it opens the conversation.
        self._sentence = state in (AWAKE, WAITING, FOLLOW_UP, CHAT) and self._sentence_mode()
        if state in (WAITING, FOLLOW_UP, CHAT):
            self.cutter.set_max_seconds(self.SENTENCE_MAX_S if self._sentence else self.COMMAND_MAX_S)
        else:
            self.cutter.set_max_seconds(BurstCutter.MAX_S)
        end = self.CHAT_END_S if state == CHAT else self.SENTENCE_END_S
        self.cutter.set_end_silence(end if self._sentence else self.COMMAND_END_SILENCE_S)
        self.cutter.set_quiet(self.COMMAND_END_SILENCE_S if self._sentence else 0)
        self._busy_checked = self._clock()
        # Awake → waiting is the same to the bubble: say it once.
        phase = {FOLLOW_UP: 'follow_up', CHAT: 'chat'}.get(state, 'awake')
        if phase != getattr(self, '_phase', None):
            self._phase = phase
            self._publish('wake', {'phase': phase})

    def _sleep(self) -> None:
        was, self.state = self.state, SLEEPING
        self._sentence = False
        self.cutter.set_max_seconds(BurstCutter.MAX_S)
        self.cutter.set_end_silence(BurstCutter.END_SILENCE_S)
        self.cutter.set_quiet(0)
        if was != SLEEPING:
            self._phase = 'sleeping'
            self._publish('wake', {'phase': 'sleeping'})

    # ── Recording your word (setup screen) ───────────────────────────────────

    # What Whisper writes for `audio`, the way a wake check hears it.
    # `spelling`: the word being recorded, for Whisper to expect ('' = none).
    def hear(self, audio: np.ndarray, spelling: Optional[str] = None) -> Optional[str]:
        self._hint_override = spelling
        try:
            return self._transcribe(audio)
        finally:
            self._hint_override = None

    # The word Whisper is told to expect: the one being recorded, your typed
    # spelling, or "Hey Koli". A made-up word gets written as one it knows
    # ("Hey Avia" came out "hey yeah" three times) unless Whisper is told.
    def spelling_hint(self) -> str:
        if self._hint_override is not None:
            return self._hint_override
        if self.personal():
            return str(self._cfg().get('spelling') or '').strip()
        return 'Hey Koli'

    # One burst of speech from the microphone (your word), or None when
    # nothing was said within `timeout`. Works with the feature off: the tap
    # is added for this one recording. Wake detection pauses meanwhile.
    # Practice reads whole sentences: `end_silence_s` is the pause that ends
    # it, and `stop` (an Event) ends it at once with what was said so far.
    def capture_word(self, timeout: float = 6.0, max_seconds: float = 3.0, end_silence_s: float = 0.7,
                     stop: Optional[threading.Event] = None) -> Optional[np.ndarray]:
        recorder = getattr(self.sm, 'audio_recorder', None)
        if recorder is None:
            return None
        chunks = collections.deque()
        arrived = threading.Event()

        def tap(chunk, when=None):
            chunks.append(np.asarray(chunk, dtype=np.float32).reshape(-1))
            arrived.set()

        cutter = BurstCutter(head_s=max_seconds, end_silence_s=end_silence_s, max_s=max_seconds)
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
                if stop is not None and stop.is_set():
                    burst = cutter.burst
                    return burst.audio() if burst is not None and burst.speech_seconds() >= self.MIN_SPEECH_S else None
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
        pieces = []
        text = engine.transcribe_audio(audio, language=self.wake_language() or self.sm.wake_language(),
                                       use_prompt=False, beam_size=1, task='transcribe', quiet=True,
                                       hotwords=self.spelling_hint(), pieces=pieces)
        self._last_pieces = pieces
        return text

    def _meeting_voiceprint(self, audio: np.ndarray):
        meetings = getattr(self.sm, 'meetings', None)
        return meetings.voiceprint_of(audio) if meetings is not None else None

    def _meeting_my_voice(self, audio: np.ndarray, mic: str) -> Optional[float]:
        meetings = getattr(self.sm, 'meetings', None)
        return meetings.my_voice_similarity(audio, mic) if meetings is not None else None

    def _state_manager_run(self, **kwargs) -> str:
        return self.sm.run_wake_command(**kwargs)

    def _state_manager_ai(self) -> bool:
        enabled = getattr(self.sm, '_agent_enabled', None)
        return bool(enabled()) if callable(enabled) else False

    def _state_manager_chat_open(self) -> bool:
        chat = getattr(self.sm, 'agent_chat', None)
        return bool(chat is not None and chat.is_open())

    def _in_chat(self) -> bool:
        try:
            return bool(self._chat_open())
        except Exception:
            return False

    # A command ran: another may follow without the wake word.
    def _after_command(self) -> None:
        self._enter(CHAT if self._in_chat() else FOLLOW_UP, self._seconds('follow_up_seconds', 5))

    def _state_manager_quick(self, audio) -> str:
        quick = getattr(self.sm, 'run_quick_command', None)
        return quick(audio) if callable(quick) else MISSED

    # Your speaking level on `mic`, learned from hotkey dictations.
    def _state_manager_level(self, mic: str) -> Optional[float]:
        levels = getattr(self.sm, 'voice_levels', None)
        return levels.my_level(mic) if levels is not None else None

    def _wake_sound(self) -> None:
        feedback = getattr(self.sm, 'audio_feedback', None)
        if feedback is not None:
            feedback.play_wake_sound()


def _frame_level_db(frame: np.ndarray) -> float:
    from .voice_activity_detection import chunk_level_db
    return chunk_level_db(frame)


def _speech_prob_function():
    from .meeting.capture import speech_prob_function
    return speech_prob_function()


def _platform_is_locked() -> bool:
    from .platform import session
    return session.is_locked()


def _publish(topic, payload):
    from .ui.events import publish
    publish(topic, payload)


# A line in the terminal, like the dictation ones ("✓ Transcribed: ..."). The
# terminal only, not the log: the log has its own line per check, and the app
# window re-renders on every log line.
def _console(line: str) -> None:
    try:
        print(f"   {line}")
    except (UnicodeEncodeError, OSError):
        pass        # a console that can't show it (or none, under pythonw)
