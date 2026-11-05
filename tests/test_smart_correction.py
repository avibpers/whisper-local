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
        self.assertEqual(sc.correct('check the roll', MODEL, llm, self.local, ready_warmer(), 'English'), 'check the role')
        self.assertEqual(llm.calls[0][0], 'polish')

    def test_a_local_model_never_falls_back_to_the_cloud(self):
        llm = FakeLlm()
        sc.correct('check the roll', MODEL, llm, self.local, ready_warmer())
        self.assertIs(llm.calls[0][2]['fallbacks'], False)

    def test_a_model_still_loading_costs_no_wait(self):
        llm = FakeLlm()
        warmed = []
        warmer = sc.Warmer(lambda spec, options: warmed.append((spec, options)))
        self.assertEqual(sc.correct('check the roll', MODEL, llm, self.local, warmer), '')
        self.assertEqual(llm.calls, [])       # the model wasn't asked at all
        for thread in threading.enumerate():
            if thread.name == 'correction-warm-up':
                thread.join(2)
        self.assertEqual(warmed[0][0], MODEL)

    def test_a_long_dictation_is_not_sent(self):
        llm = FakeLlm()
        self.assertEqual(sc.correct('word ' * 100, MODEL, llm, self.local, ready_warmer()), '')
        self.assertEqual(llm.calls, [])

    def test_no_answer_in_time_keeps_the_dictation_and_marks_the_model_lost(self):
        warmer = ready_warmer()
        warmer._warm_model = lambda spec, options: threading.Event().wait(0.3)
        self.assertEqual(sc.correct('check the roll', MODEL, FakeLlm(text='', error='No answer within 1 seconds'), self.local, warmer), '')
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
