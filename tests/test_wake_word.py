# tests/test_wake_word.py
# The wake word: matching what Whisper heard against the spellings, the check
# budget, and the listener's states, fed fake audio frames with no hardware.
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# ── Matching ─────────────────────────────────────────────────────────────────

class MatchWakeTests(unittest.TestCase):
    def setUp(self):
        from koli import wake_word
        self.ww = wake_word
        self.medium = wake_word.ratio_for('medium')

    def match(self, text, ratio=None, spellings=None):
        return self.ww.match_wake(text, spellings or self.ww.DEFAULT_SPELLINGS, ratio or self.medium)

    def test_the_rest_of_the_sentence_is_kept(self):
        self.assertEqual(self.match('Hey, Koli! Play Spotify.'), 'play spotify')

    def test_said_alone_gives_an_empty_rest(self):
        self.assertEqual(self.match('Hey Koli.'), '')

    def test_hebrew(self):
        self.assertEqual(self.match('היי, קולי. תפעיל ספוטיפיי'), 'תפעיל ספוטיפיי')
        self.assertEqual(self.match('הי קולי'), '')

    def test_joined_or_split_words(self):
        self.assertEqual(self.match('Heykoli next'), 'next')
        self.assertEqual(self.match('hey ko li louder'), 'louder')

    def test_near_misses_stay_asleep(self):
        for text in ('hey kelly whats up', 'Hey Google', 'hey cool', 'Hey Charlie'):
            self.assertIsNone(self.match(text), text)

    def test_high_lets_a_one_letter_miss_through_and_low_does_not(self):
        self.assertEqual(self.match('hey cool', self.ww.ratio_for('high')), '')
        self.assertIsNone(self.match('hey kodi', self.ww.ratio_for('low')))
        self.assertEqual(self.match('Hey Coley.'), '', 'what Whisper wrote for a synthesised "Hey Koli"')

    def test_the_wake_word_must_come_first(self):
        self.assertIsNone(self.match('okay hey koli'))
        self.assertIsNone(self.match('I told Koli to stop'))

    def test_a_personal_word_has_a_wider_margin(self):
        self.assertIsNone(self.match('Coli, next', spellings=['koli']))
        self.assertEqual(self.match('Coli, next', self.ww.ratio_for('medium', personal=True), ['koli']), 'next')

    def test_unknown_sensitivity_means_medium(self):
        self.assertEqual(self.ww.ratio_for('loud'), self.medium)


class SpellingsFromRecordingsTests(unittest.TestCase):
    def setUp(self):
        from koli.wake_word import spellings_from_recordings
        self.spellings = spellings_from_recordings

    def test_distinct_spellings_are_kept_in_order(self):
        self.assertEqual(self.spellings(['Koli.', 'Coli', 'koli']), ['koli', 'coli'])
        self.assertEqual(self.spellings(['קולי', 'קולי.', 'כולי']), ['קולי', 'כולי'])

    def test_different_words_are_refused(self):
        self.assertIsNone(self.spellings(['Koli', 'Hello', 'Koli']))

    def test_an_empty_recording_is_refused(self):
        self.assertIsNone(self.spellings(['Koli', '', 'Koli']))


# ── Check budget ─────────────────────────────────────────────────────────────

class CheckBudgetTests(unittest.TestCase):
    def setUp(self):
        from koli.wake_word import CheckBudget
        self.clock = FakeClock()
        self.budget = CheckBudget(self.clock)

    def test_at_most_per_minute_then_free_again(self):
        allowed = sum(self.budget.take() for _ in range(30))
        self.assertEqual(allowed, self.budget.PER_MINUTE)
        self.clock.advance(61)
        self.assertTrue(self.budget.take())

    def test_misses_in_a_row_slow_it_down(self):
        for _ in range(self.budget.MISSES_BEFORE_BACKOFF):
            self.assertTrue(self.budget.take())
            self.budget.missed()
            self.clock.advance(1)
        self.assertTrue(self.budget.backing_off())
        self.assertFalse(self.budget.take(), 'the last check was a second ago')
        self.clock.advance(self.budget.BACKOFF_INTERVAL)
        self.assertTrue(self.budget.take())
        self.clock.advance(5)
        self.assertFalse(self.budget.take())
        self.clock.advance(5)
        self.assertTrue(self.budget.take())
        self.clock.advance(self.budget.BACKOFF_SECONDS)
        self.assertFalse(self.budget.backing_off())

    def test_a_wake_starts_afresh(self):
        for _ in range(self.budget.MISSES_BEFORE_BACKOFF):
            self.budget.take()
            self.budget.missed()
        self.budget.woke()
        self.assertFalse(self.budget.backing_off())
        self.assertEqual(sum(self.budget.take() for _ in range(30)), self.budget.PER_MINUTE)


# ── The listener ─────────────────────────────────────────────────────────────
# Fake audio: a "word" is frames filled with its token value, and the fake
# Whisper reads the tokens back in order. Speech probability is 0.9 for a
# word frame and 0 for silence.

import numpy as np  # noqa: E402

# 8 is music from the speakers, quiet at the microphone: Whisper writes
# nothing for it, but TEN VAD hears it as speech. 9 is loud talk (a TV).
WORDS = {1: 'hey koli', 2: 'next song', 3: 'undo', 4: 'koli', 5: 'ding', 8: '', 9: 'blah blah'}
COMMANDS = {'next song': 'media', 'undo': 'other'}
# How loud each token is at the microphone (dBFS); you speak at -18.
LEVELS = {0: -90.0, 8: -45.0}
MY_LEVEL = -18.0


def fake_transcribe(audio):
    tokens = []
    for value in np.round(audio).astype(int):
        if value and (not tokens or tokens[-1] != value):
            tokens.append(int(value))
    return ' '.join(WORDS[t] for t in tokens if WORDS[t]) or None


def fake_level(frame):
    return LEVELS.get(int(round(float(frame[0]))), MY_LEVEL)


class FakeStateManager:
    def __init__(self):
        self.is_paused = False
        self.idle = True
        self.meetings = None
        self.mic = 'Jabra'
        self.audio_recorder = FakeRecorder()

    def is_idle(self):
        return self.idle

    def _mic_key(self):
        return self.mic


class FakeRecorder:
    def __init__(self, chunks=()):
        self.taps = []
        self.chunks = list(chunks)

    def add_tap(self, fn):
        self.taps.append(fn)
        for chunk in self.chunks:       # a recording: what the mic hears next
            fn(chunk, 0.0)

    def remove_tap(self, fn):
        if fn in self.taps:
            self.taps.remove(fn)


class FakeMeetings:
    def __init__(self, running=False):
        self.running = running

    def is_running(self):
        return self.running


class Config:
    def __init__(self, **wake):
        self.config = {'wake_word': {'enabled': True, 'sensitivity': 'medium', 'command_timeout_seconds': 5,
                                     'follow_up_seconds': 5, **wake}}


class ListenerTestCase(unittest.TestCase):
    def make(self, voiceprint=None, locked=False, outcomes=None, learned_level=None, **wake):
        from koli import wake_word
        import tempfile
        self.ww = wake_word
        self.clock = FakeClock()
        self.sm = FakeStateManager()
        self.transcribed = []
        self.runs = []
        self.run_times = []
        self.events = []
        self.sounds = 0
        self.locked = locked
        self.outcomes = outcomes
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.voices = wake_word.WakeVoices(Path(tmp.name) / 'wake_voice.json')

        def transcribe(audio):
            text = fake_transcribe(audio)
            self.transcribed.append(text)
            return text

        def run_command(audio=None, text=None, only_media=False, report_miss=True):
            said = text if text is not None else (fake_transcribe(audio) or '')
            rest = wake_word.match_wake(said, ['hey koli'], 0.9)
            said = said if rest is None else rest
            self.runs.append({'text': said, 'audio': audio is not None, 'only_media': only_media,
                              'report_miss': report_miss})
            self.run_times.append(self.clock.now)
            if self.outcomes:
                return self.outcomes.pop(0)
            if not said:
                return wake_word.EMPTY
            kind = next((k for phrase, k in COMMANDS.items() if phrase in said), None)
            if kind is None or (only_media and kind != 'media'):
                return wake_word.MISSED
            return wake_word.RAN

        def sound():
            self.sounds += 1

        self.listener = wake_word.WakeListener(
            self.sm, Config(**wake), transcribe=transcribe, voiceprint=voiceprint, run_command=run_command,
            wake_voices=self.voices, speech_prob=lambda frame: 0.9 if np.any(frame) else 0.0,
            is_locked=lambda: self.locked, publish=lambda topic, payload: self.events.append((topic, payload)),
            play_sound=sound, clock=self.clock, frame_level=fake_level,
            learned_level=lambda mic: learned_level)
        return self.listener

    def feed(self, value, seconds):
        frames = int(round(seconds / self.ww.FRAME_S))
        for _ in range(frames):
            self.listener.feed(np.full(self.ww.FRAME, float(value), dtype=np.float32), 0.9 if value else 0.0)
            self.clock.advance(self.ww.FRAME_S)

    def say(self, *words, seconds=0.7):
        for word in words:
            self.feed(word, seconds)

    def quiet(self, seconds):
        self.feed(0, seconds)

    def phases(self):
        return [p['phase'] for t, p in self.events if t == 'wake']


class WakeListenerTests(ListenerTestCase):
    def test_wake_then_a_pause_then_the_command(self):
        listener = self.make()
        self.say(1)
        self.quiet(1.0)
        self.assertEqual(listener.state, self.ww.WAITING)
        self.assertEqual(self.sounds, 1)
        self.say(2)
        self.quiet(0.8)
        self.assertEqual(self.runs[-1]['text'], 'next song')
        self.assertTrue(self.runs[-1]['audio'])
        self.assertEqual(listener.state, self.ww.FOLLOW_UP)
        self.assertEqual(self.phases(), ['awake', 'follow_up'])

    def test_one_short_breath_runs_from_the_heads_text(self):
        listener = self.make()
        self.say(1, 2, seconds=0.6)
        self.quiet(0.8)
        self.assertEqual(self.runs, [{'text': 'next song', 'audio': False, 'only_media': False, 'report_miss': True}])
        self.assertEqual(len(self.transcribed), 1, 'no second Whisper pass')
        self.assertEqual(listener.state, self.ww.FOLLOW_UP)

    def test_one_long_breath_runs_the_whole_burst(self):
        listener = self.make()
        self.say(1, seconds=0.8)
        self.say(2, seconds=1.6)
        self.assertEqual(listener.state, self.ww.AWAKE)
        self.quiet(0.8)
        self.assertEqual(self.runs[-1]['text'], 'next song')
        self.assertTrue(self.runs[-1]['audio'])

    def test_the_wake_word_alone_goes_back_to_sleep(self):
        listener = self.make()
        self.say(1)
        self.quiet(6)
        self.assertEqual(listener.state, self.ww.SLEEPING)
        self.assertEqual(self.runs, [])
        self.assertEqual(self.phases(), ['awake', 'sleeping'])

    def test_a_long_wake_word_with_no_command_waits_for_one(self):
        listener = self.make()
        self.say(1, seconds=2.5)          # past the head: the whole burst is run, and holds no command
        self.quiet(0.8)
        self.assertEqual(self.runs[-1]['text'], '')
        self.assertEqual(listener.state, self.ww.WAITING)

    def test_other_speech_never_wakes(self):
        listener = self.make()
        self.say(9)
        self.quiet(1)
        self.say(3)
        self.quiet(1)
        self.assertEqual(listener.state, self.ww.SLEEPING)
        self.assertEqual(self.runs, [])
        self.assertEqual(self.sounds, 0)

    def test_a_follow_up_with_hey_koli_runs_media_only_and_misses_quietly(self):
        listener = self.make()
        self.say(1, 2, seconds=0.6)
        self.quiet(1.2)                   # the command ran; the chime played
        self.say(3)                       # "undo" from the song's lyrics
        self.quiet(0.8)
        self.assertEqual(self.runs[-1], {'text': 'undo', 'audio': True, 'only_media': True, 'report_miss': False})
        self.assertEqual(listener.state, self.ww.FOLLOW_UP, 'a miss keeps the window')
        self.say(2)
        self.quiet(0.8)
        self.assertEqual(self.runs[-1]['text'], 'next song')
        self.quiet(6)
        self.assertEqual(listener.state, self.ww.SLEEPING)

    def test_a_first_command_that_matches_nothing_says_so_and_sleeps(self):
        listener = self.make()
        self.say(1, 9, seconds=0.6)
        self.quiet(0.8)
        self.assertTrue(self.runs[-1]['report_miss'])
        self.assertEqual(listener.state, self.ww.SLEEPING)

    def test_a_hotkey_in_the_middle_of_a_wake_drops_it(self):
        listener = self.make()
        self.say(1)
        self.quiet(1)
        self.sm.idle = False              # a dictation started
        self.quiet(0.3)
        self.assertEqual(listener.state, self.ww.SLEEPING)
        self.assertEqual(self.phases()[-1], 'sleeping')
        self.say(2)
        self.quiet(1)
        self.assertEqual(self.runs, [])

    def test_nothing_is_checked_while_busy_paused_locked_or_in_a_meeting(self):
        for setup in (lambda: setattr(self.sm, 'idle', False), lambda: setattr(self.sm, 'is_paused', True),
                      lambda: setattr(self, 'locked', True), lambda: setattr(self.sm, 'meetings', FakeMeetings(True))):
            self.make()
            setup()
            self.say(1, 2, seconds=0.6)
            self.quiet(1)
            self.assertEqual(self.transcribed, [])
            self.assertEqual(self.runs, [])

    def test_clicks_are_not_checked(self):
        self.make()
        self.say(1, seconds=0.1)
        self.quiet(1)
        self.assertEqual(self.transcribed, [])

    def test_the_budget_caps_whisper_under_a_tv(self):
        self.make()
        for _ in range(40):
            self.say(9, seconds=0.5)
            self.quiet(0.7)
        self.assertLessEqual(len(self.transcribed), self.ww.CheckBudget.PER_MINUTE)

    def test_the_chime_does_not_become_the_command(self):
        listener = self.make()
        self.say(1, seconds=0.6)
        self.quiet(0.6)                   # wake heard at the end of this burst: the chime plays now
        self.say(5, seconds=0.3)          # the chime through the speakers
        self.quiet(0.8)
        self.assertEqual(self.runs, [])
        self.assertEqual(listener.state, self.ww.WAITING)


# Music from the speakers sounds like speech to TEN VAD. Quieter than your
# voice by more than the margin, it is silence, so bursts end when you stop;
# and whatever keeps sounding, a command never waits for the 8 s cut.
class BackgroundSoundTests(ListenerTestCase):
    def test_quiet_music_doesnt_hold_bursts_open(self):
        listener = self.make(learned_level=MY_LEVEL)
        self.say(8, seconds=3)                 # music before you speak
        self.say(1, 2, seconds=0.6)            # "hey koli next song", over the music
        spoke = self.clock.now
        self.say(8, seconds=3)                 # the music goes on
        self.assertEqual(self.runs[-1]['text'], 'next song')
        self.assertLess(self.run_times[-1] - spoke, 1.0, 'runs when you stop, not at the 8 s cut')
        self.assertEqual(listener.state, self.ww.FOLLOW_UP)

    def test_your_recorded_words_level_gates_without_a_learned_one(self):
        self.make()
        self.voices.set('Jabra', [np.ones(3, dtype=np.float32)], level_db=MY_LEVEL)
        self.say(8, seconds=3)
        self.say(1, 2, seconds=0.6)
        spoke = self.clock.now
        self.say(8, seconds=3)
        self.assertEqual(self.runs[-1]['text'], 'next song')
        self.assertLess(self.run_times[-1] - spoke, 1.0)

    # Found in the second real test: the recorded level is your loud end
    # (-13.5 dB) and "Only my voice" had a 4 dB margin, so normal speech
    # (-18 to -26 dB on the 0.3 s average) was all taken for background. The
    # wake word's gate has its own wide margin: it only has to drop music.
    def test_your_normal_speech_isnt_taken_for_background(self):
        LEVELS[6] = -26.0                      # you, saying the word normally
        WORDS[6] = 'hey koli'
        self.addCleanup(LEVELS.pop, 6)
        self.addCleanup(WORDS.pop, 6)
        self.make()
        self.sm._voice_margin_db = lambda: 4.0  # the dictation setting must not narrow it
        self.voices.set('Jabra', [np.ones(3, dtype=np.float32)], level_db=-13.5)
        self.say(6, 2, seconds=0.6)
        self.quiet(0.8)
        self.assertEqual(self.runs[-1]['text'], 'next song')
        self.say(8, seconds=2)                 # speaker music, 30 dB under you, still isn't speech
        self.assertEqual(len(self.transcribed), 1)

    def test_without_a_level_the_command_is_cut_soon_after_the_wake(self):
        self.make()
        start = self.clock.now
        self.say(1, 2, seconds=0.8)            # wake and command...
        self.say(9, seconds=8)                 # ...then loud talk that never stops
        self.assertTrue(self.runs)
        self.assertEqual(self.runs[0]['text'], 'next song blah blah')
        self.assertLessEqual(self.run_times[0] - start, 2.0 + self.ww.WakeListener.COMMAND_AFTER_WAKE_S + 0.1)

    def test_waiting_for_a_command_doesnt_wait_for_the_8_s_cut(self):
        listener = self.make()
        self.say(1)
        self.quiet(1)
        self.assertEqual(listener.state, self.ww.WAITING)
        start = self.clock.now
        self.say(9, seconds=8)
        self.assertTrue(self.runs)
        self.assertLessEqual(self.run_times[0] - start, self.ww.WakeListener.COMMAND_MAX_S + 0.1)


class PersonalWakeWordTests(ListenerTestCase):
    def setUp(self):
        self.mine = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        self.theirs = np.array([0.0, 1.0, 0.0], dtype=np.float32)

    def make_personal(self, voice):
        listener = self.make(voiceprint=lambda audio: voice, phrases=['koli'], only_my_voice=True)
        self.voices.set('Jabra', [self.mine, self.mine * 2])
        return listener

    def test_your_word_in_your_voice_wakes(self):
        listener = self.make_personal(self.mine)
        self.say(4, 2, seconds=0.6)
        self.quiet(0.8)
        self.assertEqual(self.runs[-1]['text'], 'next song')

    def test_another_voice_is_dropped_before_whisper(self):
        listener = self.make_personal(self.theirs)
        self.say(4, 2, seconds=0.6)
        self.quiet(0.8)
        self.assertEqual(self.transcribed, [])
        self.assertEqual(listener.state, self.ww.SLEEPING)

    def test_hey_koli_no_longer_wakes(self):
        self.make_personal(self.mine)
        self.say(1, 2, seconds=0.6)
        self.quiet(0.8)
        self.assertEqual(self.runs, [])

    def test_follow_ups_must_sound_like_you_and_may_be_any_command(self):
        voice = {'now': self.mine}
        listener = self.make(voiceprint=lambda audio: voice['now'], phrases=['koli'], only_my_voice=True)
        self.voices.set('Jabra', [self.mine])
        self.say(4, 2, seconds=0.6)
        self.quiet(0.8)
        voice['now'] = self.theirs
        self.say(3)
        self.quiet(0.8)
        self.assertEqual(len(self.runs), 1, 'someone else in the room')
        voice['now'] = self.mine
        self.say(3)
        self.quiet(0.8)
        self.assertEqual(self.runs[-1], {'text': 'undo', 'audio': True, 'only_media': False, 'report_miss': False})
        self.assertEqual(listener.state, self.ww.FOLLOW_UP)

    def test_a_microphone_without_your_word_says_so_once(self):
        self.make_personal(self.mine)
        self.sm.mic = 'Laptop mic'
        for _ in range(3):
            self.say(4, seconds=0.6)
            self.quiet(0.8)
        self.assertEqual(self.transcribed, [])
        notices = [p for t, p in self.events if t == 'notify']
        self.assertEqual(len(notices), 1)
        self.assertIn('microphone', notices[0]['message'])

    def test_no_voiceprint_yet_doesnt_wake(self):
        self.make_personal(None)
        self.say(4, 2, seconds=0.6)
        self.quiet(0.8)
        self.assertEqual(self.runs, [])
        self.assertIn('loading', [p for t, p in self.events if t == 'notify'][0]['message'])

    # The loading notice is its own: a microphone without your word still
    # says so afterwards.
    def test_the_loading_notice_doesnt_hide_the_microphone_one(self):
        self.make_personal(None)
        self.say(4, seconds=0.6)
        self.quiet(0.8)
        self.sm.mic = 'Laptop mic'
        self.say(4, seconds=0.6)
        self.quiet(0.8)
        messages = [p['message'] for t, p in self.events if t == 'notify']
        self.assertEqual(len(messages), 2)
        self.assertIn('microphone', messages[1])

    # The voice model starts loading as soon as a personal word is set, so
    # the first try after launch isn't lost to it.
    def test_the_voice_model_is_warmed_for_a_personal_word(self):
        asked = []

        class LoadingMeetings(FakeVoiceMeetings):
            def voiceprint_ready(self):
                asked.append(True)
                return len(asked) >= 2

        self.make_personal(self.mine)
        self.sm.meetings = LoadingMeetings(self.mine)
        self.quiet(5)
        self.assertEqual(len(asked), 2, 'asked until ready, every couple of seconds, then never again')

    def test_hey_koli_doesnt_load_the_voice_model(self):
        self.make()
        asked = []
        self.sm.meetings = FakeVoiceMeetings(self.mine)
        self.sm.meetings.voiceprint_ready = lambda: asked.append(True) or True
        self.quiet(3)
        self.assertEqual(asked, [])


class WakeListenerWiringTests(ListenerTestCase):
    def test_the_tap_follows_the_current_recorder(self):
        listener = self.make()
        self.assertTrue(listener.start())
        self.addCleanup(listener.stop)
        first = self.sm.audio_recorder
        self.assertEqual(first.taps, [listener._on_tap])
        swapped = FakeRecorder()
        swapped.taps = list(first.taps)   # a new microphone copies the taps
        self.sm.audio_recorder = swapped
        listener.stop()
        self.assertEqual(swapped.taps, [])
        self.assertEqual(first.taps, [])

    def test_settings_turn_it_on_and_off(self):
        listener = self.make()
        listener.cm.config['wake_word']['enabled'] = False
        listener.apply_settings()
        self.assertFalse(listener.is_running())
        listener.cm.config['wake_word']['enabled'] = True
        listener.apply_settings()
        self.addCleanup(listener.stop)
        self.assertTrue(listener.is_running())
        listener.cm.config['wake_word']['enabled'] = False
        listener.apply_settings()
        self.assertFalse(listener.is_running())
        self.assertEqual(self.sm.audio_recorder.taps, [])

    def test_falling_too_far_behind_starts_clean(self):
        listener = self.make()
        self.say(1)
        self.quiet(1)
        listener._on_tap(np.zeros(int(listener.QUEUE_SECONDS * self.ww.RATE) + 1, dtype=np.float32))
        self.assertIsNone(listener._take_pending())
        self.assertEqual(listener.state, self.ww.SLEEPING)

    def test_capturing_your_word_returns_one_burst_and_removes_its_tap(self):
        listener = self.make()
        silence = np.zeros(self.ww.FRAME * 20, dtype=np.float32)
        word = np.full(self.ww.FRAME * 40, 4.0, dtype=np.float32)
        self.sm.audio_recorder = FakeRecorder([silence, word, silence, silence, silence])
        audio = listener.capture_word(timeout=1.0)
        self.assertIsNotNone(audio)
        self.assertEqual(fake_transcribe(audio), 'koli')
        self.assertEqual(self.sm.audio_recorder.taps, [])
        self.assertFalse(listener._enrolling)

    def test_capturing_nothing_gives_none(self):
        listener = self.make()
        self.sm.audio_recorder = FakeRecorder([np.zeros(self.ww.FRAME * 10, dtype=np.float32)])
        self.assertIsNone(listener.capture_word(timeout=0.3))


# ── Running the command (StateManager) ───────────────────────────────────────

class WakeCommandStateManagerTests(unittest.TestCase):
    def _sm(self, heard='hey koli next song', commands=None):
        import threading
        import unittest.mock as mock
        from koli.state_manager import StateManager
        from koli.voice_commands import VoiceCommandManager
        sm = StateManager.__new__(StateManager)
        sm.logger = __import__('logging').getLogger('test')
        sm._state_lock = threading.Lock()
        sm._idle_actions = []
        sm._command_mode = False
        sm._rephrase_mode = False
        sm._auto_started = False
        sm.is_paused = False
        sm.is_processing = False
        sm.is_model_loading = False
        sm.level_overlay = mock.Mock()
        sm.audio_recorder = mock.Mock()
        sm.audio_recorder.get_recording_status.return_value = False
        sm.config_manager = mock.Mock()
        sm.config_manager.get_logging_config.return_value = {}
        sm.config_manager.get_whisper_config.return_value = {'language': 'auto'}
        sm.whisper_engine = mock.Mock()
        sm.whisper_engine.transcribe_audio.return_value = heard
        manager = VoiceCommandManager.__new__(VoiceCommandManager)
        manager.logger = sm.logger
        manager.enabled = True
        manager.commands = commands or [{'trigger': 'next song', 'hotkey': 'media_next'},
                                        {'trigger': 'undo', 'hotkey': 'ctrl+z'}]
        manager.execute_command = mock.Mock()
        sm.voice_command_manager = manager
        listener = mock.Mock()
        listener.spellings.return_value = ['hey koli']
        listener.match_ratio.return_value = 0.9
        listener.personal.return_value = False
        sm._wake_listener = listener
        return sm

    def run_(self, sm, **kwargs):
        import unittest.mock as mock
        with mock.patch('builtins.print'), mock.patch('koli.state_manager.publish'):
            return sm.run_wake_command(**kwargs)

    def test_the_wake_word_is_dropped_and_the_command_runs(self):
        from koli.wake_word import RAN
        sm = self._sm()
        self.assertEqual(self.run_(sm, audio=np.zeros(16000, dtype=np.float32)), RAN)
        sm.voice_command_manager.execute_command.assert_called_once()
        self.assertEqual(sm.voice_command_manager.execute_command.call_args[0][0]['hotkey'], 'media_next')
        kwargs = sm.whisper_engine.transcribe_audio.call_args.kwargs
        self.assertEqual((kwargs['language'], kwargs['use_prompt'], kwargs['task']), ('auto', False, 'transcribe'))
        sm.level_overlay.flash_success.assert_called_once()
        self.assertFalse(sm.is_processing)

    def test_text_from_the_head_skips_whisper(self):
        from koli.wake_word import RAN
        sm = self._sm()
        self.assertEqual(self.run_(sm, text='next song'), RAN)
        sm.whisper_engine.transcribe_audio.assert_not_called()

    def test_only_the_wake_word_is_empty(self):
        from koli.wake_word import EMPTY
        sm = self._sm(heard='Hey Koli.')
        self.assertEqual(self.run_(sm, audio=np.zeros(16000, dtype=np.float32)), EMPTY)
        sm.voice_command_manager.execute_command.assert_not_called()
        sm.level_overlay.flash_failure.assert_not_called()

    def test_a_first_miss_says_so(self):
        from koli.wake_word import MISSED
        sm = self._sm(heard='hey koli make coffee')
        self.assertEqual(self.run_(sm, audio=np.zeros(16000, dtype=np.float32)), MISSED)
        sm.level_overlay.flash_failure.assert_called_once_with("Didn't catch that")

    def test_a_media_only_follow_up_skips_other_commands_quietly(self):
        from koli.wake_word import MISSED
        sm = self._sm(heard='undo')
        result = self.run_(sm, audio=np.zeros(16000, dtype=np.float32), only_media=True, report_miss=False)
        self.assertEqual(result, MISSED)
        sm.voice_command_manager.execute_command.assert_not_called()
        sm.level_overlay.flash_failure.assert_not_called()
        self.assertTrue(sm.whisper_engine.transcribe_audio.call_args.kwargs['quiet'])

    # Anyone can say "Hey Koli" (a TV, a video): a command that runs a program
    # must ask first. Your own word passed the voice check, so it doesn't.
    def test_hey_koli_commands_confirm_their_runs_and_your_word_doesnt(self):
        sm = self._sm()
        self.run_(sm, text='next song')
        self.assertTrue(sm.voice_command_manager.execute_command.call_args.kwargs.get('confirm_runs'))
        sm = self._sm()
        sm._wake_listener.personal.return_value = True
        self.run_(sm, text='next song')
        self.assertFalse(sm.voice_command_manager.execute_command.call_args.kwargs.get('confirm_runs', False))

    def test_refuses_while_a_dictation_runs(self):
        from koli.wake_word import BUSY
        sm = self._sm()
        sm.audio_recorder.get_recording_status.return_value = True
        self.assertEqual(self.run_(sm, text='next song'), BUSY)
        sm.voice_command_manager.execute_command.assert_not_called()
        self.assertFalse(sm.is_processing)

    def test_the_settings_switch_starts_and_stops_the_listener(self):
        import unittest.mock as mock
        from koli.settings.appliers import Appliers
        appliers = Appliers(mock.Mock())
        listener = appliers.ctx.state_manager._wake_listener
        appliers.wake_word('wake_word.enabled', True, False)
        listener.apply_settings.assert_called_once()
        appliers.wake_word('wake_word.sensitivity', 'high', 'medium')
        listener.apply_settings.assert_called_once()


# ── Recording your word (services/commands) ──────────────────────────────────

class FakeSettings:
    def __init__(self, cm):
        self.cm = cm
        self.sets = []

    def set(self, path, value, source='ui', record_undo=True):
        self.sets.append((path, value, source))
        self.cm.config['wake_word'][path.split('.', 1)[1]] = value
        return {'ok': True}


class FakeVoiceMeetings(FakeMeetings):
    def __init__(self, voice):
        super().__init__()
        self.voice = voice

    def voiceprint_of(self, audio):
        return self.voice

    def voiceprint_ready(self):
        return True


class WakeSetupTests(ListenerTestCase):
    def setUp(self):
        try:
            from koli.services import commands
            self.commands = commands
        except ImportError as e:
            self.skipTest(f'services.commands needs deps the lean env lacks: {e}')
        self.mine = np.array([1.0, 0.0], dtype=np.float32)

    def setup(self, words, voice=None):
        listener = self.make(voiceprint=lambda audio: self.sm.meetings.voice)
        self.sm.meetings = FakeVoiceMeetings(self.mine if voice is None else voice)
        self.sm._wake_listener = listener
        said = [np.full(16000, float(w), dtype=np.float32) for w in words]
        listener.capture_word = lambda timeout=6.0, max_seconds=3.0: said.pop(0) if said else None
        self.settings = FakeSettings(listener.cm)
        return listener

    def test_three_recordings_make_your_word(self):
        listener = self.setup([4, 4, 4])
        self.assertTrue(self.commands.wake_prepare(self.sm)['ready'])
        for i in range(3):
            self.assertEqual(self.commands.wake_record(self.sm)['count'], i + 1)
        status = self.commands.wake_save(self.sm, self.settings)
        self.assertEqual((status['personal'], status['word'], status['recorded_here']), (True, 'koli', True))
        self.assertIn(('wake_word.only_my_voice', True, 'app'), self.settings.sets)
        self.assertIsNotNone(listener.wake_voices.get('Jabra'))
        self.assertIsNotNone(listener.wake_voices.level('Jabra'), 'how loud you said it gates quieter sound')
        self.assertEqual(listener.recordings, [])

    def test_different_words_are_refused_and_start_over(self):
        listener = self.setup([4, 9, 4])
        self.commands.wake_prepare(self.sm)
        for _ in range(3):
            self.commands.wake_record(self.sm)
        with self.assertRaisesRegex(ValueError, 'different words'):
            self.commands.wake_save(self.sm, self.settings)
        self.assertEqual(listener.recordings, [])
        self.assertFalse(listener.personal())

    def test_silence_or_no_voiceprint_must_be_said_again(self):
        self.setup([])
        self.commands.wake_prepare(self.sm)
        with self.assertRaisesRegex(ValueError, "Didn't hear"):
            self.commands.wake_record(self.sm)
        self.setup([4], voice=None)
        self.sm.meetings.voice = None
        with self.assertRaisesRegex(ValueError, 'too quiet'):
            self.commands.wake_record(self.sm)

    def test_the_same_word_on_another_mic_adds_to_it(self):
        listener = self.setup([4, 4, 4, 4, 4, 4])
        self.commands.wake_prepare(self.sm)
        for _ in range(3):
            self.commands.wake_record(self.sm)
        self.commands.wake_save(self.sm, self.settings)
        self.sm.mic = 'Laptop mic'
        self.commands.wake_prepare(self.sm)
        for _ in range(3):
            self.commands.wake_record(self.sm)
        self.commands.wake_save(self.sm, self.settings)
        self.assertEqual(sorted(listener.wake_voices.mics()), ['Jabra', 'Laptop mic'])

    def test_test_shows_the_word_and_the_voice_score(self):
        listener = self.setup([4, 4, 4, 4])
        self.commands.wake_prepare(self.sm)
        for _ in range(3):
            self.commands.wake_record(self.sm)
        self.commands.wake_save(self.sm, self.settings)
        result = self.commands.wake_test(self.sm)
        self.assertEqual((result['woke'], result['heard'], result['similarity']), (True, 'koli', 1.0))
        self.assertEqual(result['needs'], listener.voice_match())

    def test_reset_goes_back_to_hey_koli(self):
        listener = self.setup([4, 4, 4])
        self.commands.wake_prepare(self.sm)
        for _ in range(3):
            self.commands.wake_record(self.sm)
        self.commands.wake_save(self.sm, self.settings)
        status = self.commands.wake_reset(self.sm, self.settings)
        self.assertEqual((status['personal'], status['word']), (False, 'Hey Koli'))
        self.assertIsNone(listener.wake_voices.get('Jabra'))


# ── Running programs from a wake word, and the locked check ──────────────────

class WakeSafetyTests(unittest.TestCase):
    def _manager(self):
        import logging
        import unittest.mock as mock
        from koli.voice_commands import VoiceCommandManager
        manager = VoiceCommandManager.__new__(VoiceCommandManager)
        manager.logger = logging.getLogger('test')
        manager._confirm_risky_command = mock.Mock(return_value=False)
        return manager

    def test_a_wake_run_command_asks_and_doesnt_run_when_declined(self):
        import unittest.mock as mock
        manager = self._manager()
        with mock.patch('koli.voice_commands.subprocess.Popen') as popen, mock.patch('builtins.print'):
            manager.execute_command({'trigger': 'open notes', 'run': 'notepad.exe', 'confirm': False}, confirm_runs=True)
        manager._confirm_risky_command.assert_called_once()
        self.assertTrue(manager._confirm_risky_command.call_args.kwargs['fail_closed'])
        popen.assert_not_called()

    def test_then_steps_ask_too(self):
        import unittest.mock as mock
        manager = self._manager()
        manager._send_hotkey = mock.Mock()
        with mock.patch('koli.voice_commands.subprocess.Popen') as popen, mock.patch('builtins.print'):
            manager.execute_command({'trigger': 'go', 'hotkey': 'ctrl+s', 'then': [{'run': 'calc.exe'}]}, confirm_runs=True)
        popen.assert_not_called()

    def test_a_hotkey_command_runs_unasked(self):
        import unittest.mock as mock
        manager = self._manager()
        with mock.patch('koli.voice_commands.subprocess.Popen') as popen, mock.patch('builtins.print'):
            manager.execute_command({'trigger': 'open notes', 'run': 'notepad.exe'})
        manager._confirm_risky_command.assert_not_called()
        popen.assert_called_once()

    def test_without_a_dialog_a_command_that_must_ask_doesnt_run(self):
        import logging
        import unittest.mock as mock
        from koli.voice_commands import VoiceCommandManager
        manager = VoiceCommandManager.__new__(VoiceCommandManager)
        manager.logger = logging.getLogger('test')
        with mock.patch('koli.voice_commands.os.name', 'posix'):
            self.assertFalse(manager._confirm_risky_command('x', 'y', fail_closed=True))
            self.assertTrue(manager._confirm_risky_command('x', 'y'))

    def test_the_mac_lock_check_fails_closed(self):
        if sys.platform == 'darwin':
            self.skipTest('Quartz is here; this checks the fallback')
        import importlib.util
        # Just the file: the macOS package itself doesn't import elsewhere.
        spec = importlib.util.spec_from_file_location(
            'mac_session', ROOT / 'src' / 'koli' / 'platform' / 'macos' / 'session.py')
        session = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(session)
        self.assertTrue(session.is_locked(), "no Quartz: can't tell, so locked")

    def test_the_windows_lock_check_fails_closed(self):
        import unittest.mock as mock
        if sys.platform != 'win32':
            self.skipTest('Windows only')
        from koli.platform.windows import session
        self.assertIsInstance(session.is_locked(), bool)   # this desktop may be locked or not right now
        with mock.patch.object(session, '_user32', None):
            self.assertTrue(session.is_locked())
        broken = mock.Mock()
        broken.OpenInputDesktop.side_effect = OSError('access denied')
        with mock.patch.object(session, '_user32', broken):
            self.assertTrue(session.is_locked())

    def test_the_listener_treats_a_failing_lock_check_as_locked(self):
        from koli.wake_word import WakeListener

        def broken():
            raise RuntimeError('no session')
        listener = WakeListener(FakeStateManager(), Config(), is_locked=broken, transcribe=lambda a: None,
                                speech_prob=lambda f: 0.0, publish=lambda *a: None, play_sound=lambda: None)
        self.assertFalse(listener.may_listen())


class WakeVoicesTests(unittest.TestCase):
    def test_kept_per_microphone_as_a_unit_average_and_cleared(self):
        import tempfile
        from koli.wake_word import WakeVoices
        with tempfile.TemporaryDirectory() as tmp:
            voices = WakeVoices(Path(tmp) / 'wake_voice.json')
            voices.set('Jabra', [np.array([2.0, 0.0]), np.array([0.0, 2.0])])
            got = voices.get('Jabra')
            self.assertAlmostEqual(float(np.linalg.norm(got)), 1.0, places=5)
            self.assertAlmostEqual(float(got[0]), float(got[1]), places=5)
            self.assertIsNone(voices.get('Laptop mic'))
            voices.clear()
            self.assertIsNone(voices.get('Jabra'))


if __name__ == '__main__':
    unittest.main()
