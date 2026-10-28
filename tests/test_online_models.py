# tests/test_online_models.py
# Online transcription and what it costs (docs/design/online-models.md): the
# price list (dated changes, your own prices on top, tokens before minutes,
# free providers), the usage ledger (session, day and meeting sums, AI calls
# counted on a meeting), the OpenAI and Gemini answers turned into text and
# token counts, and the engine falling back to local Whisper. Nothing reaches
# the internet and no file is written to the real settings folder.
import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koli.usage import ledger, prices  # noqa: E402


# Every test gets its own data folder, and a fresh ledger and price cache.
class _Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        folder = self.tmp.name
        self.patches = [mock.patch('koli.utils.get_user_app_data_path', lambda: folder),
                        mock.patch('koli.ui.events.publish', lambda *a, **k: None)]
        for p in self.patches:
            p.start()
        prices._cache['data'] = None
        ledger._state.update({'loaded': False, 'days': {}, 'meetings': {}, 'session': {}, 'unpriced': set()})

    def tearDown(self):
        if ledger._save_timer is not None:
            ledger._save_timer.cancel()
            ledger._save_timer = None
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class PriceTests(_Isolated):
    def test_every_shipped_transcription_model_has_a_price(self):
        rows = prices.catalog(transcribe_only=True)
        self.assertGreaterEqual(len([r for r in rows if r['provider'] == 'openai']), 5)
        self.assertGreaterEqual(len([r for r in rows if r['provider'] == 'gemini']), 12)
        for r in rows:
            self.assertIn(r['transcribe'], ('upload', 'live'), r['spec'])
            self.assertIsNotNone(r['per_hour'], r['spec'])

    def test_minutes_when_no_tokens_tokens_when_given(self):
        self.assertAlmostEqual(prices.cost('openai/whisper-1', seconds=120), 0.012)
        # 1000 audio tokens at $1.25/M + 100 out at $5/M
        self.assertAlmostEqual(prices.cost('openai/gpt-4o-mini-transcribe', seconds=60, audio_tokens=1000, text_out=100),
                               (1000 * 1.25 + 100 * 5.0) / 1e6)

    def test_gemini_audio_tokens_use_the_audio_price(self):
        cost = prices.cost('gemini/gemini-2.5-flash', audio_tokens=1500, text_in=100, text_out=200)
        self.assertAlmostEqual(cost, (1500 * 1.00 + 100 * 0.30 + 200 * 2.50) / 1e6)

    def test_subscriptions_and_local_are_free_unknown_has_no_price(self):
        self.assertEqual(prices.cost('codex/gpt-5.5', text_in=10_000, text_out=5000), 0.0)
        self.assertEqual(prices.cost('local/large-v3', seconds=600), 0.0)
        self.assertIsNone(prices.cost('openrouter/some/model', text_in=100))

    def test_a_dated_change_applies_from_its_day(self):
        before = prices.entry('gemini/gemini-4-argon', datetime.date(2026, 11, 30))
        after = prices.entry('gemini/gemini-4-argon', datetime.date(2026, 12, 1))
        self.assertEqual(before['text_in'], 2.0)
        self.assertEqual(after['text_in'], 4.0)

    def test_your_price_wins_and_reset_brings_back_the_shipped_one(self):
        prices.save_own('openai/whisper-1', {'per_minute': '0.01'})
        self.assertAlmostEqual(prices.cost('openai/whisper-1', seconds=60), 0.01)
        row = next(r for r in prices.catalog() if r['spec'] == 'openai/whisper-1')
        self.assertTrue(row['edited'])
        prices.save_own('openai/whisper-1', None)
        self.assertAlmostEqual(prices.cost('openai/whisper-1', seconds=60), 0.006)

    def test_your_price_is_not_overridden_by_a_later_shipped_change(self):
        prices.save_own('gemini/gemini-4-argon', {'audio_in': 1, 'text_in': 1, 'text_out': 1})
        self.assertEqual(prices.entry('gemini/gemini-4-argon', datetime.date(2027, 6, 1))['text_in'], 1.0)


class LedgerTests(_Isolated):
    def test_sums_per_session_day_and_meeting(self):
        ledger.record('dictation', 'openai/whisper-1', seconds=60)
        with ledger.for_meeting('m1'):
            ledger.record('meeting', 'openai/whisper-1', seconds=120)
        self.assertAlmostEqual(ledger.session()['cost'], 0.018)
        m = ledger.meeting('m1')
        self.assertAlmostEqual(m['cost'], 0.012)
        self.assertEqual(m['transcribed_by'], ['openai/whisper-1'])
        self.assertAlmostEqual(ledger.days(1)[0]['cost'], 0.018)

    def test_saved_and_read_back(self):
        ledger.record('dictation', 'openai/whisper-1', seconds=60, meeting_id='m2')
        ledger.flush()
        ledger._state.update({'loaded': False, 'days': {}, 'meetings': {}})
        self.assertAlmostEqual(ledger.meeting('m2')['cost'], 0.006)

    def test_unpriced_model_is_marked(self):
        ledger.record('polish', 'openrouter/x/y', tokens_in=10, meeting_id='m3')
        self.assertTrue(ledger.meeting('m3')['unpriced'])

    def test_ai_calls_in_a_meeting_block_count_on_it_under_its_name(self):
        from koli import llm

        class Fake:
            label = 'OpenAI API'

            def complete(self, *a):
                return 'answer', 1000, 200

        with mock.patch('koli.llm.routing.route', lambda *a, **k: (['openai/gpt-6-luna'], 30)), \
                mock.patch('koli.llm.providers.get', lambda pid: Fake()):
            with ledger.for_meeting('m4', 'summary'):
                result = llm.complete('meetings', [{'role': 'user', 'content': 'hi'}])
        self.assertAlmostEqual(result.cost, (1000 * 0.10 + 200 * 0.50) / 1e6)
        self.assertIn('summary', ledger.meeting('m4')['by_feature'])

    def test_forget_meeting(self):
        ledger.record('meeting', 'openai/whisper-1', seconds=60, meeting_id='m5')
        ledger.forget_meeting('m5')
        self.assertEqual(ledger.meeting('m5')['cost'], 0)


class TransportTests(_Isolated):
    def test_openai_whisper_answer_keeps_the_certainty_numbers(self):
        from koli.online_stt import openai
        answer = {'text': ' Hello there.', 'language': 'hebrew', 'duration': 2.0,
                  'segments': [{'text': ' Hello there.', 'start': 0, 'end': 2, 'avg_logprob': -0.2,
                                'no_speech_prob': 0.01, 'compression_ratio': 1.1}],
                  'usage': {'type': 'duration', 'seconds': 2}}
        sent = {}

        def raw(method, url, headers, body, timeout):
            sent.update(headers=headers, body=body)
            return 200, {}, json.dumps(answer).encode()

        with mock.patch('koli.llm.http.raw', raw):
            text, info = openai.transcribe('sk-test', 'whisper-1', b'RIFFwav', language='he', prompt='Koli')
        self.assertEqual(text, 'Hello there.')
        self.assertEqual(info['language'], 'he')
        self.assertEqual(info['segments'][0]['avg_logprob'], -0.2)
        self.assertEqual(info['billed_seconds'], 2.0)
        self.assertIn(b'name="response_format"\r\n\r\nverbose_json', sent['body'])
        self.assertIn(b'RIFFwav', sent['body'])
        self.assertTrue(sent['headers']['Content-Type'].startswith('multipart/form-data; boundary='))

    def test_openai_4o_answer_gives_tokens(self):
        from koli.online_stt import openai
        answer = {'text': 'Hi', 'usage': {'type': 'tokens', 'input_tokens': 130, 'output_tokens': 5,
                                          'input_token_details': {'audio_tokens': 120, 'text_tokens': 10}}}
        with mock.patch('koli.llm.http.raw', lambda *a: (200, {}, json.dumps(answer).encode())):
            text, info = openai.transcribe('sk', 'gpt-4o-mini-transcribe', b'x')
        self.assertEqual((text, info['audio_tokens'], info['tokens_in'], info['tokens_out']), ('Hi', 120, 10, 5))

    def test_gemini_answer_splits_audio_and_text_tokens(self):
        from koli.online_stt import gemini
        answer = {'candidates': [{'content': {'parts': [{'text': '{"language": "he", "text": "שלום לכולם"}'}]}}],
                  'usageMetadata': {'promptTokenCount': 140, 'candidatesTokenCount': 12, 'thoughtsTokenCount': 30,
                                    'promptTokensDetails': [{'modality': 'AUDIO', 'tokenCount': 100},
                                                            {'modality': 'TEXT', 'tokenCount': 40}]}}
        with mock.patch('koli.llm.http.post_json', lambda *a: answer):
            text, info = gemini.transcribe('key', 'gemini-2.5-flash', b'x')
        self.assertEqual(text, 'שלום לכולם')
        self.assertEqual(info['language'], 'he')
        self.assertEqual((info['audio_tokens'], info['tokens_in'], info['tokens_out']), (100, 40, 42))


class FakeLocal:
    language = None
    initial_prompt = 'Koli'
    hotwords = ['JumboMail']
    task = 'transcribe'
    vad_manager = None

    def __init__(self):
        self.calls = []
        self.last_info = None

    def transcribe_audio(self, audio, **kw):
        self.calls.append(kw)
        self.last_info = {'backend': 'faster_whisper', 'model': 'large-v3', 'audio_seconds': 1.0, 'segments': []}
        return 'local text'


class EngineTests(_Isolated):
    def setUp(self):
        super().setUp()
        from koli.online_stt import engine
        engine._last_problem.clear()
        self.local = FakeLocal()
        self.engine = engine.OnlineEngine('openai/whisper-1', lambda: self.local, feature='dictation')
        self.audio = np.zeros(16000, dtype=np.float32)

    def test_online_answer_is_priced_and_described(self):
        info = {'language': 'en', 'segments': [], 'audio_tokens': 0, 'tokens_in': 0, 'tokens_out': 0, 'billed_seconds': 1.0}
        seen = {}

        def fake(key, model, wav, language='', prompt='', timeout=0):
            seen.update(model=model, prompt=prompt, wav=wav[:4])
            return 'online text', info

        with mock.patch.dict('koli.online_stt.engine.TRANSPORTS', {'openai': fake}), \
                mock.patch('koli.llm.providers.OpenAI.key', lambda self: 'sk'):
            text = self.engine.transcribe_audio(self.audio)
        self.assertEqual(text, 'online text')
        self.assertEqual(seen, {'model': 'whisper-1', 'prompt': 'Koli JumboMail', 'wav': b'RIFF'})
        self.assertEqual(self.engine.last_info['backend'], 'online')
        self.assertAlmostEqual(self.engine.last_info['cost'], 0.0001)
        self.assertEqual(self.local.calls, [])

    def test_failure_falls_back_to_local(self):
        def boom(*a, **k):
            from koli.llm.http import NetworkError
            raise NetworkError('offline')

        with mock.patch.dict('koli.online_stt.engine.TRANSPORTS', {'openai': boom}), \
                mock.patch('koli.llm.providers.OpenAI.key', lambda self: 'sk'), \
                mock.patch('koli.ui.events.report_problem', lambda m: None):
            text = self.engine.transcribe_audio(self.audio)
        self.assertEqual(text, 'local text')
        self.assertIn('online_error', self.engine.last_info)
        # The caller counts the local call once, as dictation and meetings do.
        from koli.online_stt import record_local
        record_local('dictation', self.engine.last_info)
        self.assertEqual(ledger.session()['by_feature']['dictation']['local/large-v3']['calls'], 1)

    def test_quick_checks_stay_local(self):
        with mock.patch.dict('koli.online_stt.engine.TRANSPORTS', {'openai': None}):
            self.assertEqual(self.engine.transcribe_audio(self.audio, quiet=True), 'local text')

    def test_live_models_are_not_usable_yet(self):
        from koli.online_stt import usable
        self.assertTrue(usable('openai/gpt-4o-mini-transcribe'))
        self.assertTrue(usable('gemini/gemini-2.5-flash-lite'))
        self.assertFalse(usable('openai/gpt-live-transcribe'))
        self.assertFalse(usable('openai/gpt-5.5'))
        self.assertFalse(usable('ollama/llama3'))
        self.assertFalse(usable(''))


if __name__ == '__main__':
    unittest.main()
