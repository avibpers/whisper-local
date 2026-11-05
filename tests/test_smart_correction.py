# tests/test_smart_correction.py
# A small model fixes misheard words right after Whisper. What matters most here:
# a dictation never waits for it, it never goes to the cloud behind your back, and
# a rewrite never reaches your text.

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koli import smart_correction as sc  # noqa: E402

MODEL = 'ollama/gemma4:e4b'


class AcceptTests(unittest.TestCase):
    def test_a_fixed_word_is_written(self):
        self.assertEqual(sc.accept('check the roll of the user', 'check the role of the user'), 'check the role of the user')

    def test_the_same_words_keep_your_punctuation(self):
        self.assertEqual(sc.accept('check the roll, please', 'Check the roll please.'), '')

    def test_a_rewrite_is_thrown_away(self):
        self.assertEqual(sc.accept('so I think that we need to check it now', 'We should verify this immediately before going'), '')

    def test_a_translation_is_thrown_away(self):
        self.assertEqual(sc.accept('מה קורה אחי', 'what is up bro'), '')

    def test_letters_from_a_third_alphabet_are_thrown_away(self):
        # These are real answers from a small model on 2026-11-05.
        self.assertEqual(sc.accept('בבקשה תשלחי את זה עכשיו', 'בבקשה תשלчи את זה עכשיו'), '')
        self.assertEqual(sc.accept('אני מצליח להיכנס עכשיו', 'אני מצליחに入る עכשיו'), '')

    def test_an_empty_answer_keeps_the_dictation(self):
        self.assertEqual(sc.accept('check the roll', ''), '')

    def test_a_hebrew_dictation_may_take_a_latin_term(self):
        self.assertEqual(sc.accept('נו כבר לעשות מרד עכשיו', 'נו כבר לעשות merge עכשיו'), 'נו כבר לעשות merge עכשיו')


class MessagesTests(unittest.TestCase):
    def test_the_model_is_told_the_language_window_words_and_what_came_before(self):
        system, user = sc.messages('check the roll', 'English', ['Viaset', 'Koli'], 'code.exe', 'Koli', ['one', 'two'])
        self.assertEqual(user, {'role': 'user', 'content': 'check the roll'})
        for part in ('Language of this dictation: English', 'Viaset, Koli', 'code.exe - Koli', 'one\ntwo'):
            self.assertIn(part, system['content'])

    def test_a_cloud_model_gets_neither_the_window_nor_what_came_before(self):
        system = sc.messages('hi', 'English', ['Koli'], 'code.exe', 'Secret title', ['my last message'], private=False)[0]['content']
        self.assertNotIn('Secret title', system)
        self.assertNotIn('my last message', system)
        self.assertIn('Koli', system)

    def test_the_fixed_instruction_comes_first_for_speed(self):
        self.assertTrue(sc.messages('hi', 'English')[0]['content'].startswith(sc.INSTRUCTION))

    def test_previous_dictations_come_from_this_window_only_oldest_first(self):
        entries = [{'app': 'a', 'title': 't', 'text': f'text {i}', 'timestamp': f'2026-11-05T10:0{i}'} for i in range(6)]
        entries += [{'app': 'a', 'title': 'other', 'text': 'no', 'timestamp': '2026-11-05T11:00'},
                    {'app': 'a', 'title': 't', 'text': 'gone', 'timestamp': '2026-11-05T11:00', 'cancelled': True}]
        self.assertEqual(sc.previous_in_window(entries, 'a', 't'), ['text 2', 'text 3', 'text 4', 'text 5'])
        self.assertEqual(sc.previous_in_window(entries, '', ''), [])


class RecentHistoryTests(unittest.TestCase):
    def _journal(self, count):
        import json, tempfile
        from unittest import mock
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'transcripts.jsonl'
        path.write_text(''.join(json.dumps({'text': f'text {i} ' + 'x' * 100, 'timestamp': str(i)}) + chr(10) for i in range(count)), encoding='utf-8')
        patcher = mock.patch('koli.transcript_log.get_user_app_data_path', return_value=folder.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def test_it_reads_only_the_end_and_never_half_a_line(self):
        from koli.transcript_log import load_recent_transcripts
        self._journal(2000)
        entries = load_recent_transcripts(max_bytes=2000)
        self.assertLess(len(entries), 30)
        self.assertEqual(entries[-1]['timestamp'], '1999')
        self.assertTrue(all(e['text'].startswith('text') for e in entries))

    def test_a_short_journal_is_read_whole(self):
        from koli.transcript_log import load_recent_transcripts
        self._journal(3)
        self.assertEqual([e['timestamp'] for e in load_recent_transcripts()], ['0', '1', '2'])


class FakeLlm:
    def __init__(self, text='{"text": "check the role"}', error=''):
        self.text, self.error = text, error
        self.calls = []

    def __call__(self, feature, messages, **kw):
        self.calls.append((feature, messages, kw))
        return SimpleNamespace(text=self.text, error=self.error)


def ready_warmer():
    warmer = sc.Warmer(lambda spec, options: None)
    warmer._ready[MODEL] = True
    return warmer


class CorrectTests(unittest.TestCase):
    local = staticmethod(lambda spec: 'local')
    cloud = staticmethod(lambda spec: 'cloud')

    def test_a_ready_model_corrects(self):
        llm = FakeLlm()
        self.assertEqual(sc.correct('check the roll', MODEL, llm, self.local, ready_warmer(), 'English').text, 'check the role')
        self.assertIn('polish', [call[0] for call in llm.calls])

    def test_a_local_model_never_falls_back_to_the_cloud(self):
        llm = FakeLlm()
        sc.correct('check the roll', MODEL, llm, self.local, ready_warmer())
        self.assertIs(llm.calls[0][2]['fallbacks'], False)

    def test_a_model_still_loading_costs_no_wait(self):
        llm = FakeLlm()
        warmed = []
        warmer = sc.Warmer(lambda spec, options: warmed.append((spec, options)))
        self.assertEqual(sc.correct('check the roll', MODEL, llm, self.local, warmer).text, '')
        self.assertEqual(llm.calls, [])       # the model wasn't asked at all
        for thread in threading.enumerate():
            if thread.name == 'correction-warm-up':
                thread.join(2)
        self.assertEqual(warmed[0][0], MODEL)

    def test_a_long_dictation_is_not_sent(self):
        llm = FakeLlm()
        self.assertEqual(sc.correct('word ' * 100, MODEL, llm, self.local, ready_warmer()).text, '')
        self.assertEqual(llm.calls, [])

    def test_no_answer_in_time_keeps_the_dictation_and_marks_the_model_lost(self):
        warmer = ready_warmer()
        warmer._warm_model = lambda spec, options: threading.Event().wait(0.3)
        self.assertEqual(sc.correct('check the roll', MODEL, FakeLlm(text='', error='No answer within 1 seconds'), self.local, warmer).text, '')
        self.assertFalse(warmer.ready(MODEL))

    def test_the_warm_up_sends_the_options_the_calls_use(self):
        llm = FakeLlm()
        sc.correct('check the roll', MODEL, llm, self.local, ready_warmer())
        self.assertIs(llm.calls[0][2]['options'], sc.OLLAMA_OPTIONS)
        self.assertIs(sc.OLLAMA_OPTIONS['think'], False)


class ConsoleTests(unittest.TestCase):
    def _run(self, llm, text='check the roll', warmer=None):
        shown = []
        warmer = warmer or ready_warmer()
        sc.correct(text, MODEL, llm, lambda spec: 'local', warmer, 'English', ['Viaset'], 'code.exe', 'Koli', ['one', 'two'], show=shown.append)
        return shown

    def test_it_shows_what_the_model_gets_and_what_came_back(self):
        shown = self._run(FakeLlm())
        for part in ('Language of this dictation: English', 'Viaset', 'code.exe - Koli', 'one\ntwo', 'Dictation: check the roll'):
            self.assertIn(part, shown[0])
        self.assertNotIn('Never rephrase', shown[0])     # not the fixed instruction every time
        self.assertEqual(shown[1], 'AI correction changed it to: check the role')

    def test_it_says_when_nothing_changed_or_an_answer_was_thrown_away(self):
        self.assertEqual(self._run(FakeLlm(text='{"text": "check the roll"}'))[1], 'AI correction: no change')
        rewrite = '{"text": "please verify this user account permission setting now"}'
        self.assertEqual(self._run(FakeLlm(text=rewrite))[1], 'AI correction thrown away: please verify this user account permission setting now')

    def test_it_says_why_the_model_was_not_used(self):
        self.assertIn('still loading', self._run(FakeLlm(), warmer=sc.Warmer(lambda spec, options: None))[0])
        self.assertIn('over the 400 limit', self._run(FakeLlm(), text='word ' * 100)[0])

    def test_the_live_console_gets_it_and_the_log_file_does_not(self):
        import logging
        from unittest import mock
        from koli.ui import log_stream
        handler = log_stream.LogStreamHandler()
        seen = []
        root = logging.getLogger()
        spy = logging.Handler()
        spy.emit = seen.append
        root.addHandler(spy)
        self.addCleanup(root.removeHandler, spy)
        with mock.patch.object(log_stream, '_handler', handler), mock.patch.object(log_stream, 'publish'):
            log_stream.show('first\nsecond', 'smart_correction')
        self.assertEqual([r['msg'] for r in handler.tail()], ['first', 'second'])
        self.assertEqual(seen, [])


class TestButtonTests(unittest.TestCase):
    def test_testing_the_correction_model_does_not_reload_it(self):
        from unittest import mock
        from koli import llm
        with mock.patch.object(llm, 'complete', return_value=SimpleNamespace(ok=True, text='pong', error='')) as run:
            llm.test(MODEL, sc.OLLAMA_OPTIONS)
            self.assertIs(run.call_args.kwargs['options'], sc.OLLAMA_OPTIONS)


class AnsweringLlm(FakeLlm):
    # A different answer for the correction and for the score, like the real calls.
    def __init__(self, text='{"text": "check the role"}', score='{"score": 4}', error=''):
        super().__init__(text, error)
        self.score = score

    def __call__(self, feature, messages, **kw):
        self.calls.append((feature, messages, kw))
        if feature == 'polish_score':
            return SimpleNamespace(text=self.score, error='')
        return SimpleNamespace(text=self.text, error=self.error)


class GrammarTests(unittest.TestCase):
    def _run(self, text, answer, **kw):
        return sc.correct(text, MODEL, AnsweringLlm(answer), lambda spec: 'local', ready_warmer(), 'English', grammar=True, **kw)

    def test_it_fixes_the_english_and_says_which_words(self):
        outcome = self._run('why we is going there now', '{"text": "why we are going there now"}')
        self.assertEqual(outcome.text, 'why we are going there now')
        self.assertEqual(outcome.kind, 'grammar')
        self.assertEqual(outcome.changes, [{'from': 'is', 'to': 'are'}])

    def test_what_you_said_is_kept_with_what_changed(self):
        outcome = self._run('why we is going there now', '{"text": "why we are going there now"}')
        self.assertEqual(outcome.record(), {'kind': 'grammar', 'before': 'why we is going there now', 'after': 'why we are going there now',
                                            'changes': [{'from': 'is', 'to': 'are'}], 'score': 4})

    def test_an_invented_ending_is_thrown_away(self):
        # A real answer on 2026-11-05: the rest of the sentence was made up.
        text = 'Also those need to be click a bell to rush the animation to see results'
        answer = '{"text": "Also, those need to be clickable, so the user will not need to wait to see all the steps."}'
        outcome = self._run(text, answer)
        self.assertEqual(outcome.text, '')
        self.assertEqual(outcome.changes, [])

    def test_hebrew_is_never_given_the_grammar_rewrite(self):
        llm = AnsweringLlm('{"text": "נו כבר לעשות merge עכשיו"}')
        outcome = sc.correct('נו כבר לעשות מרד עכשיו', MODEL, llm, lambda spec: 'local', ready_warmer(), 'Hebrew', grammar=True)
        self.assertEqual(outcome.kind, 'correct')
        system = next(call[1][0]['content'] for call in llm.calls if call[0] == 'polish')
        self.assertTrue(system.startswith(sc.INSTRUCTION))

    def test_without_the_mode_english_keeps_its_grammar_rule_out(self):
        llm = AnsweringLlm()
        outcome = sc.correct('check the roll', MODEL, llm, lambda spec: 'local', ready_warmer(), 'English')
        self.assertEqual(outcome.kind, 'correct')


class ScoreTests(unittest.TestCase):
    def test_the_score_is_kept_and_shown_and_changes_nothing_yet(self):
        shown = []
        llm = AnsweringLlm('{"text": "All accounts that work him"}', score='{"score": 2}')
        outcome = sc.correct('All accounts that work him', MODEL, llm, lambda spec: 'local', ready_warmer(), 'English', show=shown.append)
        self.assertEqual(outcome.score, 2)
        self.assertEqual(outcome.text, '')
        self.assertTrue(any('Makes sense: 2/5' in line and 'held' in line for line in shown))

    def test_a_score_that_makes_no_sense_is_ignored(self):
        for bad in ('{"score": 9}', '{"score": "high"}', 'not json', '{}'):
            outcome = sc.correct('check the roll', MODEL, AnsweringLlm(score=bad), lambda spec: 'local', ready_warmer(), 'English')
            self.assertIsNone(outcome.score, bad)

    def test_it_asks_with_the_same_options_and_never_the_cloud(self):
        llm = AnsweringLlm()
        sc.correct('check the roll', MODEL, llm, lambda spec: 'local', ready_warmer(), 'English')
        score_call = next(call for call in llm.calls if call[0] == 'polish_score')
        self.assertIs(score_call[2]['options'], sc.OLLAMA_OPTIONS)
        self.assertIs(score_call[2]['fallbacks'], False)
        self.assertEqual(score_call[2]['model'], MODEL)

    def test_a_cloud_model_scores_without_what_you_wrote_before(self):
        llm = AnsweringLlm()
        sc.correct('check the roll', 'openai/x', llm, lambda spec: 'cloud', None, 'English', before=['my last message'])
        score_call = next(call for call in llm.calls if call[0] == 'polish_score')
        self.assertNotIn('my last message', score_call[1][0]['content'])


class TranslateWantedTests(unittest.TestCase):
    HEBREW = 'מה קורה אחי הכל טוב'
    DISCORD = {'translate': 'he_en'}

    def test_the_key_translates_hebrew_anywhere(self):
        self.assertTrue(sc.translate_wanted(self.HEBREW, None, key_held=True))

    def test_an_app_rule_translates_hebrew_without_the_key(self):
        self.assertTrue(sc.translate_wanted(self.HEBREW, self.DISCORD))

    def test_in_a_translating_app_the_key_writes_as_you_said_it(self):
        self.assertFalse(sc.translate_wanted(self.HEBREW, self.DISCORD, key_held=True))

    def test_nothing_asked_means_no_translation(self):
        self.assertFalse(sc.translate_wanted(self.HEBREW, None))
        self.assertFalse(sc.translate_wanted(self.HEBREW, {'style': 'casual'}))

    def test_english_stays_english(self):
        self.assertFalse(sc.translate_wanted('how are you doing', self.DISCORD))
        self.assertFalse(sc.translate_wanted('how are you doing', None, key_held=True))


class TranslateTests(unittest.TestCase):
    HEBREW = 'על משימות בגמבו מייל'
    local = staticmethod(lambda spec: 'local')

    def test_it_writes_the_english(self):
        outcome = sc.translate(self.HEBREW, MODEL, AnsweringLlm('{"text": "On tasks in JumboMail."}'), self.local, ready_warmer())
        self.assertEqual(outcome.text, 'On tasks in JumboMail.')
        self.assertFalse(outcome.failed)
        self.assertEqual(outcome.record()['before'], self.HEBREW)

    def test_it_gets_a_longer_limit_than_a_correction(self):
        llm = AnsweringLlm('{"text": "On tasks in JumboMail."}')
        sc.translate(self.HEBREW, MODEL, llm, self.local, ready_warmer())
        call = next(c for c in llm.calls if c[0] == 'polish')
        self.assertEqual(call[2]['timeout'], sc.TRANSLATE_TIMEOUT)

    def test_a_model_still_loading_means_not_translated_and_no_wait(self):
        llm = AnsweringLlm()
        outcome = sc.translate(self.HEBREW, MODEL, llm, self.local, sc.Warmer(lambda spec, options: None))
        self.assertTrue(outcome.failed)
        self.assertEqual((outcome.text, llm.calls), ('', []))

    def test_no_answer_in_time_means_not_translated(self):
        warmer = ready_warmer()
        outcome = sc.translate(self.HEBREW, MODEL, AnsweringLlm('', error='timed out'), self.local, warmer)
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.text, '')

    def test_an_answer_that_is_not_a_translation_is_thrown_away(self):
        # A real answer on 2026-11-05 to a dictated "Answer in English please".
        for answer in ('Please provide the dictation you would like me to translate.', 'עדיין בעברית לגמרי', 'word ' * 60):
            self.assertEqual(sc.accept_translation(self.HEBREW, answer), '', answer)

    def test_a_good_translation_is_kept_even_though_every_word_changed(self):
        self.assertEqual(sc.accept_translation(self.HEBREW, 'On tasks in JumboMail.'), 'On tasks in JumboMail.')

    def test_a_long_dictation_is_still_translated(self):
        outcome = sc.translate('מילה ' * 120, MODEL, AnsweringLlm('{"text": "' + 'word ' * 100 + '"}'), self.local, ready_warmer())
        self.assertFalse(outcome.failed)


class PipelineStepTests(unittest.TestCase):
    def _manager(self, settings):
        from unittest import mock
        from koli.state_manager import StateManager
        sm = StateManager.__new__(StateManager)
        sm.logger = mock.Mock()
        sm.config_manager = mock.Mock()
        sm.config_manager.get_path = lambda path: settings.get(path)
        sm.config_manager.get_postprocess_config = lambda: {}
        sm.whisper_engine = mock.Mock(last_info={'language': 'he'})
        return sm

    def test_a_translation_runs_even_with_polish_off(self):
        from unittest import mock
        sm = self._manager({})
        with mock.patch.object(type(sm), '_ai_translate', return_value='English') as run:
            self.assertEqual(sm._ai_step('מה קורה אחי', {'translate': 'he_en'}, False, False), 'English')
            run.assert_called_once()

    def test_nothing_runs_when_nothing_is_on(self):
        sm = self._manager({})
        self.assertEqual(sm._ai_step('מה קורה אחי', None, False, False), '')

    def test_grammar_mode_asks_for_grammar(self):
        from unittest import mock
        sm = self._manager({'ai.polish.mode': 'grammar'})
        with mock.patch.object(type(sm), '_ai_correct', return_value='') as run:
            sm._ai_step('how is you', None, False, True)
            self.assertIs(run.call_args.kwargs['grammar'], True)


class KeptForLearningTests(unittest.TestCase):
    def test_the_clip_keeps_what_you_said_and_what_the_ai_changed(self):
        import tempfile
        from koli.learning.clips import ClipStore
        from koli.learning.recorder import ClipRecorder
        import numpy as np
        ai = {'kind': 'grammar', 'before': 'we is', 'after': 'we are', 'changes': [{'from': 'is', 'to': 'are'}]}
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        config = SimpleNamespace(config={'learning': {'enabled': True, 'disk_cap_gb': 2}})
        recorder = ClipRecorder(config, None, store=ClipStore(Path(folder.name)))
        recorder.begin(np.ones(16000, dtype=np.float32) * 0.1, kind='hotkey', mode='dictate', mic='m')
        recorder.on_event('pipeline', {'stage': 'transcribed', 'raw': 'we is', 'info': {'language': 'en'}})
        recorder.on_event('pipeline', {'stage': 'delivered', 'outcome': 'pasted', 'text': 'we are', 'ai': ai})
        recorder.flush()
        clip = recorder.store.records()[-1]
        self.assertEqual(clip['live']['text'], 'we is')
        self.assertEqual(clip['final_text'], 'we are')
        self.assertEqual(clip['ai'], ai)

    def test_the_journal_keeps_them_too(self):
        import json
        import tempfile
        from unittest import mock
        from koli import transcript_log
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        ai = {'kind': 'translate', 'before': 'מה קורה', 'after': "What's up"}
        with mock.patch.object(transcript_log, 'get_user_app_data_path', return_value=folder.name):
            transcript_log.record_transcript("What's up", heard='מה קורה', language='he', ai=ai)
            entry = transcript_log.load_recent_transcripts()[-1]
        self.assertEqual((entry['heard'], entry['ai']['kind'], entry['language']), ('מה קורה', 'translate', 'he'))


class ProfileTests(unittest.TestCase):
    def test_no_shipped_profile_changes_your_speech_model(self):
        from ruamel.yaml import YAML
        path = ROOT / 'src' / 'koli' / 'profiles.defaults.yaml'
        profiles = YAML().load(path.read_text(encoding='utf-8'))['profiles']
        for name, profile in profiles.items():
            self.assertNotIn('model', (profile.get('overrides') or {}).get('whisper') or {}, name)


class WarmerTests(unittest.TestCase):
    def _join(self):
        for thread in threading.enumerate():
            if thread.name == 'correction-warm-up':
                thread.join(2)

    def test_it_is_ready_after_a_warm_up_and_not_while_one_runs(self):
        release = threading.Event()
        warmer = sc.Warmer(lambda spec, options: release.wait(2))
        warmer.warm(MODEL)
        self.assertFalse(warmer.ready(MODEL))
        release.set()
        self._join()
        self.assertTrue(warmer.ready(MODEL))

    def test_ollama_being_down_means_not_ready_and_no_crash(self):
        def down(spec, options):
            raise OSError('connection refused')
        warmer = sc.Warmer(down)
        warmer.warm(MODEL)
        self._join()
        self.assertFalse(warmer.ready(MODEL))

    def test_two_warm_ups_at_once_load_it_once(self):
        calls, release = [], threading.Event()

        def slow(spec, options):
            calls.append(spec)
            release.wait(2)
        warmer = sc.Warmer(slow)
        warmer.warm(MODEL)
        warmer.warm(MODEL)
        release.set()
        self._join()
        self.assertEqual(calls, [MODEL])


class OllamaWireTests(unittest.TestCase):
    def test_the_options_reach_ollama_and_stay_optional(self):
        from unittest import mock
        from koli.llm.transports import ollama
        with mock.patch.object(ollama.http, 'post_json', return_value={'message': {'content': 'hi'}}) as post:
            ollama.complete('http://x', 'm', [{'role': 'user', 'content': 'a'}], timeout=1, options=sc.OLLAMA_OPTIONS)
            payload = post.call_args[0][1]
            self.assertIs(payload['think'], False)
            self.assertEqual(payload['keep_alive'], '2h')
            self.assertEqual(payload['options']['num_ctx'], 2048)
            ollama.complete('http://x', 'm', [{'role': 'user', 'content': 'a'}], timeout=1)
            self.assertNotIn('think', post.call_args[0][1])

    def test_warming_up_loads_with_the_same_options(self):
        from unittest import mock
        from koli.llm.transports import ollama
        with mock.patch.object(ollama.http, 'post_json', return_value={}) as post:
            ollama.warm('http://x', 'm', sc.OLLAMA_OPTIONS)
            payload = post.call_args[0][1]
            self.assertEqual(payload['messages'], [])
            self.assertEqual(payload['options']['num_ctx'], 2048)


class PipelineTests(unittest.TestCase):
    def _manager(self, settings, answer='{"text": "check the role"}', info=None):
        from unittest import mock
        from koli.state_manager import StateManager
        sm = StateManager.__new__(StateManager)
        sm.logger = mock.Mock()
        sm.config_manager = mock.Mock()
        sm.config_manager.get_path = lambda path: settings.get(path)
        sm.config_manager.get_postprocess_config = lambda: {'corrections': {'Viaset': ['via set']}}
        sm.whisper_engine = mock.Mock(last_info=info or {'language': 'en'})
        return sm

    def test_the_mode_decides_which_step_runs(self):
        from unittest import mock
        sm = self._manager({'ai.polish.mode': 'correct'})
        with mock.patch.object(type(sm), '_ai_correct', return_value='fixed') as run:
            self.assertEqual(sm._ai_polish('text'), 'fixed')
            run.assert_called_once()

    def test_the_old_polish_still_works(self):
        from unittest import mock
        from koli import llm
        sm = self._manager({'ai.polish.mode': 'punctuation'})
        with mock.patch.object(llm, 'complete', return_value=SimpleNamespace(text='{"text": "Hi."}', error='')):
            self.assertEqual(sm._ai_polish('hi'), 'Hi.')


if __name__ == '__main__':
    unittest.main()
