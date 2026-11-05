# tests/test_night_pass.py
# The night pass hears kept clips again while nobody uses the PC, trusts only
# what two teachers agree on, and stops between clips the moment you come back.
# The models are fakes: these tests run a whole night in a moment.

import datetime
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koli.learning.clips import ClipStore  # noqa: E402
from koli.learning.night_pass import NightPass, judge, teachers_for  # noqa: E402
from koli.learning.retention import eviction_rank  # noqa: E402
from koli.services.learning import LearningService  # noqa: E402


def voice(model, text, prob=0.9):
    return {'model': model, 'text': text, 'mean_prob': prob}


class JudgeTests(unittest.TestCase):
    TYPED = {'outcome': 'typed', 'final_text': 'check the roll', 'live': {'text': 'check the roll'}}
    DROPPED = {'outcome': 'dropped: quieter than your voice', 'live': {'text': ''}}

    def _verdict(self, record, heard, **kw):
        return judge(record, heard, **kw)['verdict']

    def test_agreeing_with_what_was_typed_is_trusted(self):
        result = judge(self.TYPED, [voice('a', 'Check the roll.'), voice('b', 'check the roll')])
        self.assertEqual((result['verdict'], result['trust']), ('same', 'agreement'))

    def test_agreeing_on_other_words_is_trusted_and_gives_the_text(self):
        result = judge(self.TYPED, [voice('a', 'check the role'), voice('b', 'check the role')])
        self.assertEqual((result['verdict'], result['trust'], result['text']), ('differs', 'agreement', 'check the role'))

    def test_teachers_who_differ_are_skipped_not_guessed(self):
        result = judge(self.TYPED, [voice('a', 'check the role'), voice('b', 'check the rule')])
        self.assertEqual((result['verdict'], result['trust'], result['text']), ('disagree', 'none', ''))

    def test_agreement_without_confidence_is_not_trusted(self):
        heard = [voice('a', 'check the role', 0.9), voice('b', 'check the role', 0.4)]
        self.assertEqual(judge(self.TYPED, heard)['trust'], 'none')
        self.assertEqual(self._verdict(self.TYPED, heard), 'unsure')

    def test_a_clip_you_said_again_does_not_confirm_what_was_typed(self):
        heard = [voice('a', 'check the roll'), voice('b', 'check the roll')]
        self.assertEqual(self._verdict(self.TYPED, heard, said_again=True), 'unsure')

    def test_said_again_still_trusts_teachers_who_found_other_words(self):
        heard = [voice('a', 'check the role'), voice('b', 'check the role')]
        self.assertEqual(self._verdict(self.TYPED, heard, said_again=True), 'differs')

    def test_a_dropped_clip_is_speech_or_noise(self):
        self.assertEqual(self._verdict(self.DROPPED, [voice('a', 'hello there'), voice('b', 'hello there')]), 'speech')
        self.assertEqual(self._verdict(self.DROPPED, [voice('a', ''), voice('b', '')]), 'noise')

    def test_a_lone_teacher_can_confirm_but_never_overrule(self):
        self.assertEqual(self._verdict(self.TYPED, [voice('a', 'check the roll')]), 'same')
        self.assertEqual(self._verdict(self.TYPED, [voice('a', 'check the role')]), 'disagree')
        self.assertEqual(self._verdict(self.DROPPED, [voice('a', 'hello')]), 'disagree')

    def test_hebrew_needs_a_teacher_from_another_family(self):
        self.assertEqual(teachers_for('he'), ('hebrew-large-v3', 'large'))
        self.assertEqual(teachers_for('en'), ('large', 'distil-large-v3.5'))
        self.assertEqual(teachers_for('fr'), ('large',))


class FakeNight:
    def __init__(self, answers=None, detected='', missing=(), fail_on=None):
        self.answers = answers or {}
        self.detected = detected
        self.missing = set(missing)
        self.fail_on = fail_on
        self.calls = []
        self.unloaded = 0

    def is_missing(self, key):
        return key in self.missing

    def detect(self, audio, allowed=None):
        return self.detected

    def hear_with(self, key, audio, language):
        self.calls.append((key, language))
        if self.fail_on and self.fail_on(key):
            raise RuntimeError('out of memory')
        return {'text': self.answers.get(key, 'the same words'), 'mean_prob': 0.9}

    def unload(self):
        self.unloaded += 1


class FakeRecorder:
    def __init__(self, store, night_on=True, learning_on=True):
        self.store = store
        self.cm = type('Config', (), {'config': {'learning': {'night_pass': night_on}, 'whisper': {'languages': ['he', 'en']}}})()
        self._on = learning_on

    def enabled(self):
        return self._on


class Clips(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = ClipStore(Path(self._dir.name))
        self.locked = True
        self.idle = 0.0
        self.battery = False
        self.busy = False

    def _add(self, clip_id, language='en', text='the same words', ts='2026-11-05T10:00:00', **extra):
        return self.store.add({'id': clip_id, 'ts': ts, 'language': language, 'seconds': 3.0, 'outcome': 'typed',
                               'final_text': text, 'live': {'text': text}, **extra}, np.zeros(16000, dtype=np.float32))

    def _pass(self, night=None, recorder=None):
        return NightPass(recorder or FakeRecorder(self.store), night or FakeNight(), is_busy=lambda: self.busy,
                         is_locked=lambda: self.locked, seconds_idle=lambda: self.idle, on_battery=lambda: self.battery,
                         clock=lambda: datetime.datetime(2026, 11, 6, 2, 0, 0))


class NightPassTests(Clips):
    # ── When it runs ────────────────────────────────────────────────────────

    def test_it_works_when_locked_or_left_alone(self):
        night_pass = self._pass()
        self.assertEqual(night_pass.why_not(), '')
        self.locked = False
        self.assertEqual(night_pass.why_not(), 'in_use')
        self.idle = 601
        self.assertEqual(night_pass.why_not(), '')

    def test_it_waits_for_a_dictation_a_meeting_or_the_mains(self):
        night_pass = self._pass()
        self.busy = True
        self.assertEqual(night_pass.why_not(), 'busy')
        self.busy, self.battery = False, True
        self.assertEqual(night_pass.why_not(), 'battery')

    def test_it_waits_when_the_speech_model_is_not_there(self):
        night = FakeNight()
        def no_engine(key):
            raise ValueError('The speech model is still loading')
        night.is_missing = no_engine
        self.assertEqual(self._pass(night).why_not(), 'model')

    def test_both_switches_can_turn_it_off(self):
        self.assertEqual(self._pass(recorder=FakeRecorder(self.store, night_on=False)).why_not(), 'off')
        self.assertEqual(self._pass(recorder=FakeRecorder(self.store, learning_on=False)).why_not(), 'off')

    # ── One run ─────────────────────────────────────────────────────────────

    def test_a_run_labels_every_clip_and_frees_the_models(self):
        self._add('a')
        self._add('b', ts='2026-11-05T11:00:00')
        night = FakeNight()
        night_pass = self._pass(night)
        self.assertEqual(night_pass.run_once(), 2)
        record = self.store.get('a')
        self.assertEqual((record['night']['verdict'], record['trust']), ('same', 'agreement'))
        self.assertEqual(record['night']['at'], '2026-11-06T02:00:00')
        self.assertEqual(night.unloaded, 1)
        self.assertEqual(night_pass.pending(), [])
        self.assertEqual(night_pass.run_once(), 0)   # nothing is heard twice

    def test_the_newest_clips_go_first(self):
        self._add('old', ts='2026-11-01T10:00:00')
        self._add('new', ts='2026-11-05T10:00:00')
        self.assertEqual([r['id'] for r in self._pass().pending()], ['new', 'old'])

    def test_coming_back_stops_it_between_clips(self):
        for i in range(3):
            self._add(f'c{i}', ts=f'2026-11-05T1{i}:00:00')
        night = FakeNight()
        original = night.hear_with

        def come_back(key, audio, language):
            self.locked = False      # you sit down during the first clip
            return original(key, audio, language)
        night.hear_with = come_back
        night_pass = self._pass(night)
        self.assertEqual(night_pass.run_once(), 1)
        self.assertEqual(len(night_pass.pending()), 2)
        self.assertEqual(night.unloaded, 1)

    def test_a_clip_deleted_during_the_run_is_passed_over(self):
        self._add('a')
        self._add('b', ts='2026-11-05T11:00:00')
        night = FakeNight()
        original = night.hear_with

        def delete_the_other(key, audio, language):
            self.store.delete(['a'])
            return original(key, audio, language)
        night.hear_with = delete_the_other
        self.assertEqual(self._pass(night).run_once(), 1)
        self.assertIsNone(self.store.get('a'))

    def test_a_failing_clip_is_not_tried_forever(self):
        self._add('a')
        night_pass = self._pass(FakeNight(fail_on=lambda key: True))
        self.assertEqual(night_pass.run_once(), 0)
        self.assertEqual(night_pass.run_once(), 0)
        self.assertEqual(night_pass.pending(), [])
        self.assertIsNone(self.store.get('a').get('night'))

    def test_many_failures_end_the_run(self):
        for i in range(8):
            self._add(f'c{i}', ts=f'2026-11-05T1{i}:00:00')
        night = FakeNight(fail_on=lambda key: True)
        self._pass(night).run_once()
        self.assertEqual(len({call for call in night.calls}), 1)
        self.assertLessEqual(len(night.calls), 5)

    def test_a_clip_decoded_in_the_wrong_language_is_heard_in_the_right_one(self):
        self._add('a', language='en', text='What is going on')
        night = FakeNight(answers={'hebrew-large-v3': 'מה קורה', 'large': 'מה קורה'}, detected='he')
        self._pass(night).run_once()
        record = self.store.get('a')
        self.assertEqual(night.calls, [('hebrew-large-v3', 'he'), ('large', 'he')])
        self.assertTrue(record['night']['language_changed'])
        self.assertEqual((record['night']['verdict'], record['night']['text']), ('differs', 'מה קורה'))

    def test_a_missing_second_teacher_leaves_the_typed_text_as_the_second_voice(self):
        self._add('a', language='en', text='the same words')
        self._pass(FakeNight(missing=['distil-large-v3.5'])).run_once()
        self.assertEqual(self.store.get('a')['night']['verdict'], 'same')

    def test_said_again_comes_from_the_later_clip(self):
        self._add('first', text='check the roll')
        self._add('second', ts='2026-11-05T10:00:20', text='check the role', signals={'repeats': 'first'})
        night = FakeNight(answers={'large': 'check the roll', 'distil-large-v3.5': 'check the roll'})
        self._pass(night).run_once()
        self.assertEqual(self.store.get('first')['night']['verdict'], 'unsure')

    # ── What it leaves behind ───────────────────────────────────────────────

    def test_disagreements_outlast_plain_clips_on_the_disk(self):
        plain = {'split': 'train', 'outcome': 'typed'}
        self.assertEqual(eviction_rank(plain), 1)
        self.assertEqual(eviction_rank({**plain, 'night': {'verdict': 'same'}}), 1)
        self.assertEqual(eviction_rank({**plain, 'night': {'verdict': 'differs'}}), 2)
        self.assertEqual(eviction_rank({**plain, 'night': {'verdict': 'disagree'}}), 2)

    def test_the_label_survives_a_restart(self):
        self._add('a')
        self._pass().run_once()
        self.assertEqual(ClipStore(Path(self._dir.name)).get('a')['night']['verdict'], 'same')

    def test_status_counts_what_it_found(self):
        self._add('a')
        self._add('b', ts='2026-11-05T11:00:00', text='other words')
        night_pass = self._pass()
        night_pass.run_once()
        status = night_pass.status()
        self.assertEqual((status['heard'], status['left']), (2, 0))
        self.assertEqual((status['counts']['same'], status['counts']['differs']), (1, 1))
        self.assertEqual(status['last_run'], '2026-11-06T02:00:00')


class ScreenTests(Clips):
    def _service(self, night_pass=None):
        recorder = FakeRecorder(self.store)
        recorder.cap_gb = lambda: 2.0
        recorder.flush = lambda: None
        recorder.night_pass = night_pass
        return LearningService(recorder, clock=lambda: datetime.datetime(2026, 11, 6, 2, 0, 0))

    def test_the_screen_shows_what_the_night_found(self):
        self._add('a', text='check the roll')
        self._add('b', ts='2026-11-05T11:00:00', text='the same words')
        night_pass = self._pass(FakeNight(answers={'large': 'check the role', 'distil-large-v3.5': 'check the role'}))
        night_pass.run_once()
        service = self._service(night_pass)
        self.assertEqual(service.state()['night']['counts']['differs'], 2)
        items = service.recent(show='night')['items']
        self.assertEqual({i['id'] for i in items}, {'a', 'b'})
        result = next(i for i in items if i['id'] == 'a')['test']
        self.assertEqual((result['verdict'], result['text']), ('differs', 'check the role'))
        self.assertEqual(result['model'], 'large + distil-large-v3.5')
        self.assertEqual([w['text'] for w in result['words'] if w['changed']], ['role'])

    def test_a_disagreement_shows_both_teachers(self):
        self._add('a', text='check the roll')
        night_pass = self._pass(FakeNight(answers={'large': 'check the role', 'distil-large-v3.5': 'check the rule'}))
        night_pass.run_once()
        result = self._service(night_pass).recent(show='night')['items'][0]['test']
        self.assertEqual((result['verdict'], result['text'], result['other']), ('disagree', 'check the role', 'check the rule'))

    def test_the_app_without_a_night_pass_says_nothing(self):
        self.assertIsNone(self._service().state()['night'])


class PlatformTests(unittest.TestCase):
    def test_the_platform_answers_with_a_number_and_a_yes_or_no(self):
        from koli.platform import idle
        self.assertIsInstance(idle.seconds_idle(), float)
        self.assertGreaterEqual(idle.seconds_idle(), 0.0)
        self.assertIn(idle.on_battery(), (True, False))


if __name__ == '__main__':
    unittest.main()
