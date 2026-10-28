# tests/test_dictation_bubble.py
# The dictation bubble (docs/design/dictation-bubble.md): which engine fills
# the bubble, the session model behind it, usage counters, the cloud clients
# and how a session drives a dictation from start to the written text.

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _yaml(path):
    from ruamel.yaml import YAML
    return YAML(typ='safe').load(path.read_text(encoding='utf-8'))


# ── Task 1: which engine fills the bubble ────────────────────────────────────

class LiveWordsDecisionTests(unittest.TestCase):
    def setUp(self):
        from koli.live_words import choose_engine, resolve_language, usage_key, EngineChoice
        self.choose, self.resolve, self.usage_key, self.Choice = choose_engine, resolve_language, usage_key, EngineChoice

    def _pick(self, cfg, rule=None, mode='dictate', language='he', key=True, local=True):
        return self.choose(cfg, rule, mode=mode, language=language, has_key=lambda p: key, local_ready=local)

    def test_rule_overrides_global_engine(self):
        self.assertEqual(self._pick({'engine': 'off'}, {'live_words': 'phrases'}).engine, 'phrases')

    def test_rule_without_the_key_inherits(self):
        self.assertEqual(self._pick({'engine': 'phrases'}, {'auto_send': True}).engine, 'phrases')

    def test_command_rephrase_translate_and_auto_get_off(self):
        for mode in ('command', 'rephrase', 'translate', 'auto'):
            self.assertEqual(self._pick({'engine': 'cloud'}, mode=mode).engine, 'off', mode)

    def test_local_non_english_runs_phrases_with_notice(self):
        c = self._pick({'engine': 'local'}, language='he')
        self.assertEqual(c.engine, 'phrases')
        self.assertIn('English', c.notice)

    def test_local_english_stays_local(self):
        self.assertEqual(self._pick({'engine': 'local'}, language='en').engine, 'local')

    def test_local_not_loaded_runs_phrases(self):
        self.assertEqual(self._pick({'engine': 'local'}, language='en', local=False).engine, 'phrases')

    def test_cloud_without_key_runs_phrases_with_notice(self):
        c = self._pick({'engine': 'cloud', 'cloud': {'provider': 'gemini'}}, key=False)
        self.assertEqual(c.engine, 'phrases')
        self.assertIn('Gemini', c.notice)

    def test_cloud_openai_default_model(self):
        c = self._pick({'engine': 'cloud', 'cloud': {}})
        self.assertEqual((c.engine, c.provider, c.model), ('openai', 'openai', 'gpt-live-transcribe'))

    def test_cloud_gemini_model_from_settings(self):
        c = self._pick({'engine': 'cloud', 'cloud': {'provider': 'gemini', 'gemini_model': 'gemini-3.5-flash-lite'}})
        self.assertEqual((c.engine, c.model), ('gemini', 'gemini-3.5-flash-lite'))

    def test_unknown_engine_is_off(self):
        self.assertEqual(self._pick({'engine': 'banana'}).engine, 'off')

    def test_language_order(self):
        self.assertEqual(self.resolve({'language': 'auto', 'languages': ['he', 'en']}, {'language': 'en'}), 'en')
        self.assertEqual(self.resolve({'language': 'he'}, None), 'he')
        self.assertEqual(self.resolve({'language': 'auto', 'languages': ['he', 'en']}, None), 'he')
        self.assertEqual(self.resolve({'language': 'auto'}, None), '')

    def test_usage_keys(self):
        C = self.Choice
        self.assertEqual(self.usage_key(C('openai', 'openai', 'gpt-live-transcribe')), 'cloud/openai/gpt-live-transcribe')
        self.assertEqual(self.usage_key(C('gemini', 'gemini', 'gemini-3.6-flash')), 'cloud/gemini/gemini-3.6-flash')
        self.assertEqual(self.usage_key(C('phrases')), 'phrases')
        self.assertEqual(self.usage_key(C('local')), 'local')


class LiveWordsSettingsTests(unittest.TestCase):
    def setUp(self):
        self.defaults = _yaml(ROOT / 'src' / 'koli' / 'config.defaults.yaml')

    def test_new_section_defaults(self):
        lw = self.defaults['live_words']
        self.assertEqual(lw['engine'], 'off')
        self.assertEqual(lw['phrase_pause_seconds'], 1)
        self.assertEqual(lw['cloud']['provider'], 'openai')
        self.assertEqual(lw['cloud']['openai_model'], 'gpt-live-transcribe')
        self.assertEqual(lw['cloud']['gemini_model'], 'gemini-3.6-flash')
        self.assertEqual(lw['prices']['openai/gpt-live-transcribe'], {'per_minute': 0.017})
        self.assertEqual(self.defaults['overlay']['live_opacity'], 90)

    def test_old_keys_are_gone(self):
        self.assertNotIn('streaming_enabled', self.defaults['streaming'])
        self.assertNotIn('deliver_to_cursor', self.defaults['streaming'])
        self.assertNotIn('phrase_pause_seconds', self.defaults['auto_dictation'])

    def test_schema_covers_the_new_keys(self):
        from koli.settings.schema import SCHEMA as SETTINGS
        paths = {s.path for s in SETTINGS}
        for p in ('live_words.engine', 'live_words.phrase_pause_seconds', 'live_words.cloud.provider',
                  'live_words.cloud.openai_model', 'live_words.cloud.gemini_model', 'live_words.prices',
                  'overlay.live_opacity'):
            self.assertIn(p, paths)
        for p in ('streaming.streaming_enabled', 'streaming.deliver_to_cursor', 'auto_dictation.phrase_pause_seconds'):
            self.assertNotIn(p, paths)

    def test_app_rules_document_the_key(self):
        text = (ROOT / 'src' / 'koli' / 'app_rules.defaults.yaml').read_text(encoding='utf-8')
        self.assertIn('live_words:', text)


# ── Task 2: the session model ───────────────────────────────────────────────

class DictationSessionTests(unittest.TestCase):
    def setUp(self):
        from koli.dictation_session import DictationSession, join_piece
        self.S, self.join = DictationSession, join_piece

    def test_finals_append_with_spaces_and_capitals(self):
        s = self.S('phrases', 1, None)
        s.add_final('Hello there.')
        s.add_final('how are you?')
        self.assertEqual(s.text(), 'Hello there. How are you?')

    def test_mid_sentence_piece_is_lowercased_but_not_I(self):
        s = self.S('phrases', 1, None)
        s.add_final('I went to the')
        s.add_final('Store')
        self.assertEqual(s.text(), 'I went to the store')
        s.add_final('I think')
        self.assertEqual(s.text(), 'I went to the store I think')

    def test_all_caps_word_is_kept(self):
        self.assertEqual(self.join('send it to', 'NASA today', ''), ' NASA today')

    def test_selection_is_replaced_by_the_next_final(self):
        s = self.S('phrases', 1, None)
        s.add_final('Send it to Jon today.')
        self.assertTrue(s.open())
        s.edit(s.text(), 11, 14)
        s.add_final('John')
        self.assertEqual(s.text(), 'Send it to John today.')

    def test_caret_in_the_middle_inserts_there(self):
        s = self.S('openai', 1, None)
        s.add_final('one three')
        s.open()
        s.edit('one three', 4, 4)
        s.add_final('two')
        self.assertEqual(s.text(), 'one two three')

    def test_caret_moves_after_the_insertion(self):
        s = self.S('openai', 1, None)
        s.add_final('one four')
        s.open()
        s.edit('One four', 4, 4)
        s.add_final('two')
        s.add_final('three')
        self.assertEqual(s.text(), 'One two three four')

    def test_emoji_before_caret_uses_code_points(self):
        s = self.S('phrases', 1, None)
        s.open()
        s.edit('🙂 hi', 4, 4)
        s.add_final('there')
        self.assertEqual(s.text(), '🙂 hi there')

    def test_hebrew_join(self):
        s = self.S('phrases', 1, None)
        s.add_final('שלום')
        s.add_final('מה נשמע')
        self.assertEqual(s.text(), 'שלום מה נשמע')

    def test_interim_is_shown_not_settled(self):
        s = self.S('openai', 1, None)
        s.set_interim('hel')
        self.assertEqual(s.text(), '')
        self.assertEqual(s.snapshot()['interim'], 'hel')
        s.commit_interim()
        self.assertEqual(s.text(), 'Hel')
        self.assertEqual(s.snapshot()['interim'], '')

    def test_a_final_clears_the_interim(self):
        s = self.S('openai', 1, None)
        s.set_interim('hello the')
        s.add_final('Hello there.')
        self.assertEqual(s.snapshot()['interim'], '')

    def test_local_engine_never_opens(self):
        self.assertFalse(self.S('local', 1, None).open())

    def test_edit_marks_edited_and_original_keeps_engine_text(self):
        s = self.S('phrases', 1, None)
        s.add_final('teh cat')
        s.open()
        s.edit('The cat', 7, 7)
        self.assertTrue(s.edited)
        self.assertEqual(s.original(), 'Teh cat')
        self.assertEqual(s.text(), 'The cat')

    def test_moving_the_caret_alone_is_not_an_edit(self):
        s = self.S('phrases', 1, None)
        s.add_final('the cat')
        s.open()
        s.edit('The cat', 2, 2)
        self.assertFalse(s.edited)

    def test_snapshot_shape_and_words(self):
        s = self.S('phrases', 7, {'exe': 'notepad.exe'})
        s.add_final('two words')
        snap = s.snapshot()
        for key in ('id', 'engine', 'settled', 'interim', 'start', 'end', 'opened', 'listening'):
            self.assertIn(key, snap)
        self.assertEqual((snap['id'], snap['settled'], snap['start'], snap['end']), (7, 'Two words', 9, 9))
        self.assertEqual(s.words(), 2)

    def test_replacement_follows_the_case_of_the_replaced_word(self):
        self.assertEqual(self.join('Fix ', 'The', ' cat', replaced='teh'), 'the')
        self.assertEqual(self.join('Send it to ', 'john', ' today.', replaced='Jon'), 'John')

    def test_mid_text_insertion_drops_whispers_closing_period(self):
        s = self.S('phrases', 1, None)
        s.add_final('One three.')
        s.open()
        s.edit('One three.', 4, 4)
        s.add_final('Two.')
        self.assertEqual(s.text(), 'One two three.')

    def test_newline_ends_a_sentence(self):
        self.assertEqual(self.join('Dear Ann,' + chr(10), 'thanks', ''), 'Thanks')


# ── Task 3: usage and cost ──────────────────────────────────────────────────

PRICES = {'openai/gpt-live-transcribe': {'per_minute': 0.017},
          'gemini/gemini-3.6-flash': {'input_per_million': 0.75, 'output_per_million': 3.75}}


class LiveWordsUsageTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from koli import live_words_usage
        self.u = live_words_usage
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / 'live_words_usage.json'

    def test_records_add_up_per_day_and_engine(self):
        self.u.record('phrases', 30, 50, False, path=self.path, day='2026-10-28')
        self.u.record('phrases', 30, 70, True, path=self.path, day='2026-10-28')
        rows = self.u.summary(PRICES, path=self.path)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r['key'], r['recordings'], r['words'], r['fixes'], r['days']), ('phrases', 2, 120, 1, 1))
        self.assertAlmostEqual(r['minutes'], 1.0)
        self.assertAlmostEqual(r['words_per_minute'], 120.0)
        self.assertEqual(r['cost'], 0)

    def test_openai_cost_is_per_minute(self):
        self.assertAlmostEqual(self.u.cost('cloud/openai/gpt-live-transcribe', 120, 0, 0, PRICES), 0.034)

    def test_gemini_cost_is_per_token(self):
        self.assertAlmostEqual(self.u.cost('cloud/gemini/gemini-3.6-flash', 60, 1_000_000, 0, PRICES), 0.75)
        self.assertAlmostEqual(self.u.cost('cloud/gemini/gemini-3.6-flash', 60, 0, 1_000_000, PRICES), 3.75)

    def test_unknown_model_and_local_cost_nothing(self):
        self.assertEqual(self.u.cost('cloud/openai/mystery', 600, 0, 0, PRICES), 0)
        self.assertEqual(self.u.cost('local', 600, 0, 0, PRICES), 0)

    def test_summary_spans_days_newest_first(self):
        self.u.record('phrases', 60, 100, False, path=self.path, day='2026-10-20')
        self.u.record('cloud/openai/gpt-live-transcribe', 60, 90, False, path=self.path, day='2026-10-27')
        self.u.record('cloud/openai/gpt-live-transcribe', 60, 90, False, path=self.path, day='2026-10-28')
        rows = self.u.summary(PRICES, path=self.path)
        self.assertEqual([r['key'] for r in rows], ['cloud/openai/gpt-live-transcribe', 'phrases'])
        self.assertEqual((rows[0]['first_day'], rows[0]['last_day'], rows[0]['days']), ('2026-10-27', '2026-10-28', 2))
        self.assertAlmostEqual(rows[0]['cost'], 0.034)
        self.assertEqual(rows[0]['label'], 'OpenAI · gpt-live-transcribe')

    def test_no_minutes_means_zero_words_per_minute(self):
        self.u.record('phrases', 0, 3, False, path=self.path, day='2026-10-28')
        self.assertEqual(self.u.summary(PRICES, path=self.path)[0]['words_per_minute'], 0)

    def test_a_corrupt_file_reads_empty_and_is_replaced(self):
        self.path.write_text('{not json', encoding='utf-8')
        self.assertEqual(self.u.summary(PRICES, path=self.path), [])
        self.u.record('phrases', 6, 1, False, path=self.path, day='2026-10-28')
        self.assertEqual(len(self.u.summary(PRICES, path=self.path)), 1)


# ── Task 4: the WebSocket client ────────────────────────────────────────────

class WebSocketFramingTests(unittest.TestCase):
    def setUp(self):
        from koli.cloud_stt import ws
        self.ws = ws

    def _reader(self, data: bytes):
        buf = bytearray(data)

        def read_exact(n):
            chunk = bytes(buf[:n])
            del buf[:n]
            if len(chunk) < n:
                raise ConnectionError('closed')
            return chunk
        return read_exact

    def test_masked_text_frame_round_trips(self):
        frame = self.ws.encode_frame(self.ws.TEXT, b'Hello', bytes.fromhex('37fa213d'))
        self.assertEqual(frame, bytes.fromhex('818537fa213d7f9f4d5158'))   # RFC 6455 5.7
        self.assertEqual(self.ws.read_frame(self._reader(frame)), (self.ws.TEXT, True, b'Hello'))

    def test_length_forms(self):
        for size, header_len in ((125, 2), (126, 4), (70000, 10)):
            frame = self.ws.encode_frame(self.ws.BINARY, b'x' * size, b'abcd')
            self.assertEqual(len(frame), header_len + 4 + size, size)
            self.assertEqual(self.ws.read_frame(self._reader(frame))[2], b'x' * size)

    def test_unmasked_server_frame(self):
        self.assertEqual(self.ws.read_frame(self._reader(bytes.fromhex('810548656c6c6f'))), (1, True, b'Hello'))

    def test_accept_check(self):
        self.assertTrue(self.ws._check_accept('dGhlIHNhbXBsZSBub25jZQ==', 's3pPLMBiTxaQ9kYGzzhZRbK+xOo='))
        self.assertFalse(self.ws._check_accept('dGhlIHNhbXBsZSBub25jZQ==', 'wrong'))

    def test_recv_answers_ping_and_joins_continuations(self):
        server = (bytes.fromhex('8900')                       # ping, no payload
                  + bytes.fromhex('0103') + b'Hel'             # text, not final
                  + bytes.fromhex('8002') + b'lo'              # continuation, final
                  + bytes.fromhex('8800'))                     # close
        sent = []
        sock = self.ws.WebSocket(read_exact=self._reader(server), write=sent.append)
        self.assertEqual(sock.recv(), ('text', 'Hello'))
        self.assertEqual(sent[0][0] & 0x0f, self.ws.PONG)
        self.assertIsNone(sock.recv())


# ── Task 5: OpenAI live transcription ───────────────────────────────────────

class _FakeSocket:
    def __init__(self):
        import queue
        self.sent, self.incoming, self.closed = [], queue.Queue(), False

    def send_text(self, text):
        import json
        self.sent.append(json.loads(text))

    def push(self, **event):
        import json
        self.incoming.put(('text', json.dumps(event)))

    def recv(self):
        import queue
        try:
            return self.incoming.get(timeout=3)
        except queue.Empty:
            return None

    def close(self):
        self.closed = True
        self.incoming.put(None)


def _wait(cond, seconds=2):
    import time
    end = time.time() + seconds
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


class OpenAILiveTests(unittest.TestCase):
    def _client(self, language='he', connect=None):
        from koli.cloud_stt.openai_live import OpenAILive
        self.sock = _FakeSocket()
        self.calls = []
        self.interims, self.finals, self.errors = [], [], []

        def default_connect(url, headers, timeout=10):
            self.calls.append((url, headers))
            return self.sock
        return OpenAILive('sk-test', 'gpt-live-transcribe', language, self.interims.append, self.finals.append,
                          self.errors.append, connect=connect or default_connect, retry_delay=0.01)

    def test_session_update_is_first_and_key_only_in_header(self):
        c = self._client()
        c.start()
        self.assertTrue(_wait(lambda: self.sock.sent))
        url, headers = self.calls[0]
        self.assertEqual(url, 'wss://api.openai.com/v1/realtime?intent=transcription')
        self.assertEqual(headers['Authorization'], 'Bearer sk-test')
        self.assertNotIn('sk-test', url)
        self.assertEqual(self.sock.sent[0], {'type': 'session.update', 'session': {
            'type': 'transcription', 'audio': {'input': {
                'format': {'type': 'audio/pcm', 'rate': 24000},
                'transcription': {'model': 'gpt-live-transcribe', 'languages': ['he'], 'delay': 'low'},
                'turn_detection': None}}}})
        c.cancel()

    def test_no_language_sends_no_languages(self):
        c = self._client(language='')
        c.start()
        self.assertTrue(_wait(lambda: self.sock.sent))
        self.assertNotIn('languages', self.sock.sent[0]['session']['audio']['input']['transcription'])
        c.cancel()

    def test_audio_before_open_is_sent_after_the_session_update(self):
        import base64
        import threading
        import numpy as np
        gate = threading.Event()

        def slow_connect(url, headers, timeout=10):
            gate.wait(2)
            return self.sock
        c = self._client(connect=slow_connect)
        c.start()
        c.feed(np.zeros(16000, dtype=np.float32))     # 1 s while still connecting
        gate.set()
        c.finish(timeout=0.3)
        kinds = [m['type'] for m in self.sock.sent]
        self.assertEqual(kinds[0], 'session.update')
        appended = sum(len(base64.b64decode(m['audio'])) for m in self.sock.sent
                       if m['type'] == 'input_audio_buffer.append')
        self.assertEqual(appended, 48000)                # 24 kHz PCM16: 24000 samples x 2 bytes
        self.assertEqual(kinds[-1], 'input_audio_buffer.commit')
        self.assertAlmostEqual(c.seconds_sent, 1.0, places=2)

    def test_deltas_grow_the_interim_and_completions_keep_commit_order(self):
        c = self._client()
        c.start()
        self.assertTrue(_wait(lambda: self.sock.sent))
        s = self.sock
        s.push(type='input_audio_buffer.committed', item_id='A')
        s.push(type='conversation.item.input_audio_transcription.delta', item_id='A', content_index=0, delta='Hel')
        s.push(type='conversation.item.input_audio_transcription.delta', item_id='A', content_index=0, delta='lo')
        s.push(type='input_audio_buffer.committed', item_id='B')
        s.push(type='conversation.item.input_audio_transcription.completed', item_id='B', content_index=0,
               transcript='world')
        s.push(type='conversation.item.input_audio_transcription.completed', item_id='A', content_index=0,
               transcript='Hello')
        self.assertTrue(_wait(lambda: len(self.finals) == 2))
        self.assertEqual(self.finals, ['Hello', 'world'])
        self.assertIn('Hel', self.interims)
        self.assertIn('Hello', self.interims)
        self.assertTrue(c.transcribed_any)
        c.cancel()

    def test_one_retry_then_failure(self):
        attempts = []

        def failing(url, headers, timeout=10):
            attempts.append(1)
            raise ConnectionError('no network')
        c = self._client(connect=failing)
        c.start()
        self.assertTrue(_wait(lambda: c.failed))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.errors), 1)

    def test_retry_succeeds(self):
        attempts = []

        def flaky(url, headers, timeout=10):
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionError('blip')
            return self.sock
        c = self._client(connect=flaky)
        c.start()
        self.assertTrue(_wait(lambda: self.sock.sent))
        self.assertFalse(c.failed)
        c.cancel()

    def test_error_event_is_reported(self):
        c = self._client()
        c.start()
        self.assertTrue(_wait(lambda: self.sock.sent))
        self.sock.push(type='error', error={'message': 'Invalid model'})
        self.assertTrue(_wait(lambda: self.errors))
        self.assertIn('Invalid model', self.errors[0])
        self.assertTrue(c.failed)

    def test_unsent_audio_is_the_turns_without_a_transcript(self):
        import numpy as np
        c = self._client()
        c.start()
        self.assertTrue(_wait(lambda: self.sock.sent))
        c.feed(np.ones(8000, dtype=np.float32) * 0.1)
        c.commit()
        c.feed(np.ones(16000, dtype=np.float32) * 0.2)
        c.commit()
        self.sock.push(type='input_audio_buffer.committed', item_id='A')
        self.sock.push(type='input_audio_buffer.committed', item_id='B')
        self.sock.push(type='conversation.item.input_audio_transcription.completed', item_id='A', content_index=0,
                       transcript='one')
        self.assertTrue(_wait(lambda: self.finals))
        left = c.unsent_audio()
        self.assertEqual(len(left), 16000)
        self.assertAlmostEqual(float(left[0]), 0.2, places=5)
        c.cancel()

# ── Task 6: Gemini transcribes a phrase ─────────────────────────────────────

class GeminiPhraseTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        from koli.cloud_stt import gemini_phrase
        self.g = gemini_phrase
        self.audio = (np.sin(np.arange(8000) / 8.0) * 0.1).astype(np.float32)   # a tone, not silence
        self.posts = []

    def _post(self, answer):
        def post(url, payload, headers=None, timeout=30):
            self.posts.append((url, payload, headers))
            return answer
        return post

    def test_request_shape_and_key_in_header_only(self):
        import base64
        answer = {'candidates': [{'content': {'parts': [{'text': 'שלום לכולם'}]}}],
                  'usageMetadata': {'promptTokenCount': 120, 'candidatesTokenCount': 6}}
        text, tin, tout = self.g.transcribe(self.audio, 'he', 'earlier words', 'AIza-test', 'gemini-3.6-flash',
                                            post=self._post(answer))
        self.assertEqual((text, tin, tout), ('שלום לכולם', 120, 6))
        url, payload, headers = self.posts[0]
        self.assertTrue(url.endswith('/models/gemini-3.6-flash:generateContent'))
        self.assertNotIn('AIza-test', url)
        self.assertEqual(headers, {'x-goog-api-key': 'AIza-test'})
        parts = payload['contents'][0]['parts']
        audio = [p for p in parts if 'inline_data' in p]
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0]['inline_data']['mime_type'], 'audio/wav')
        self.assertTrue(base64.b64decode(audio[0]['inline_data']['data']).startswith(b'RIFF'))
        said = ' '.join(p.get('text', '') for p in parts)
        self.assertIn('Hebrew', said)
        self.assertIn('earlier words', said)
        self.assertIn('verbatim', payload['systemInstruction']['parts'][0]['text'])

    def test_thought_parts_are_dropped(self):
        answer = {'candidates': [{'content': {'parts': [{'text': 'thinking…', 'thought': True}, {'text': 'Hello.'}]}}]}
        self.assertEqual(self.g.transcribe(self.audio, '', '', 'k', 'm', post=self._post(answer))[0], 'Hello.')

    def test_no_candidates_is_empty_text(self):
        answer = {'candidates': [], 'usageMetadata': {'promptTokenCount': 50}}
        self.assertEqual(self.g.transcribe(self.audio, 'en', '', 'k', 'm', post=self._post(answer)), ('', 50, 0))

    def test_wav_is_16k_mono_pcm16(self):
        import io
        import wave
        with wave.open(io.BytesIO(self.g.wav_bytes(self.audio))) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()), (16000, 1, 2, 8000))
class GeminiSilenceTests(unittest.TestCase):
    # Gemini invents a sentence for pure silence (seen 2026-10-28 with
    # gemini-3.6-flash), so silence is never sent.
    def test_silence_is_not_sent(self):
        import numpy as np
        from koli.cloud_stt import gemini_phrase
        posts = []
        result = gemini_phrase.transcribe(np.zeros(16000, dtype=np.float32), 'he', '', 'k', 'm',
                                          post=lambda *a, **k: posts.append(a) or {})
        self.assertEqual(result, ('', 0, 0))
        self.assertEqual(posts, [])

# ── Task 7: the session drives the dictation ────────────────────────────────

class LiveWordsFlowTests(unittest.TestCase):
    WINDOW = {'exe': 'notepad.exe', 'hwnd': 5, 'title': 'notes'}

    def setUp(self):
        import unittest.mock as mock
        self.focus = [False]
        self.patches = [mock.patch('builtins.print'),
                        mock.patch('koli.state_manager.publish'),
                        mock.patch('koli.live_session_flow.publish'),
                        mock.patch('koli.state_manager.postprocess', side_effect=lambda t, cfg, trace=None, polish=None: t),
                        mock.patch('koli.state_manager.record_transcription'),
                        mock.patch('koli.state_manager.record_transcript'),
                        mock.patch('koli.state_manager.foreground'),
                        mock.patch('koli.live_session_flow.foreground'),
                        mock.patch('koli.live_session_flow.live_words_usage')]
        started = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.record_transcript = started[5]
        started[6].get_foreground_app.return_value = dict(self.WINDOW)
        started[7].get_foreground_app.return_value = dict(self.WINDOW)
        self.usage = started[8]
        self.flow_publish = started[2]

    def _sm(self, engine='phrases', phrases=(), rule=None, polish=False):
        import collections
        import threading
        import unittest.mock as mock
        import numpy as np
        from koli.state_manager import StateManager
        from koli.live_session_flow import LiveWordsFlow
        sm = StateManager.__new__(StateManager)
        sm.logger = __import__('logging').getLogger('test')
        sm._state_lock, sm._pipeline_lock = threading.Lock(), threading.Lock()
        for name in ('_auto_started', '_auto_heard', '_auto_pasted', '_command_mode', '_rephrase_mode',
                     '_continuous_aborted', '_streaming_display_active', 'is_paused', 'is_processing',
                     'is_model_loading', '_hands_free'):
            setattr(sm, name, False)
        sm._rephrase_selection = ''
        sm.level_overlay = mock.Mock()
        sm.audio_feedback, sm.system_tray, sm.media_control = mock.Mock(), mock.Mock(), mock.Mock()
        sm.fallback_window = mock.Mock()
        sm.vad_manager = mock.Mock(vad_silence_timeout_seconds=30.0)
        rec = sm.audio_recorder = mock.Mock()
        self.recording = [False]

        def start(**kw):
            self.recording[0] = True
            return True

        def stop():
            self.recording[0] = False
            return np.zeros(8000, dtype=np.float32)
        rec.start_recording.side_effect = start
        rec.stop_recording.side_effect = stop
        rec.cancel_recording.side_effect = lambda: self.recording.__setitem__(0, False)
        rec.get_recording_status.side_effect = lambda: self.recording[0]
        rec.take_recorded_audio.return_value = np.ones(16000, dtype=np.float32) * 0.1
        rec.get_audio_duration.side_effect = lambda a: len(a) / 16000.0
        rec.continuous_vad = mock.Mock(had_speech=True, silence_timeout_sec=30.0, phrase_pause_sec=1.0,
                                       speech_since_phrase=False)
        sm.whisper_engine = mock.Mock(last_info={}, initial_prompt=None, language=None, task='transcribe')
        sm.whisper_engine.transcribe_audio.side_effect = list(phrases)
        sm.app_rules = mock.Mock()
        sm.app_rules.match_for_foreground.return_value = rule
        config = {'live_words': {'engine': engine, 'phrase_pause_seconds': 1}, 'audio': {}}
        sm.config_manager = mock.Mock(config=config)
        sm.config_manager.get_postprocess_config.return_value = {}
        sm.config_manager.get_whisper_config.return_value = {'language': 'en'}
        sm.config_manager.get_path.side_effect = lambda path, default=None: (
            True if (path == 'ai.polish.enabled' and polish) else default)
        sm._ai_polish = mock.Mock(side_effect=lambda t: t + ' [polished]')
        sm.clipboard_manager = mock.Mock(auto_paste=True, delivery_method='paste', auto_send_delay=0,
                                         silent_copy_allowed=True)
        sm.clipboard_manager.deliver_transcription.return_value = True
        sm.clipboard_manager.send_is_pending.return_value = False
        sm._foreground_is_textable = lambda: True
        sm._pending_model_change = sm._pending_device_change = None
        sm._idle_actions = []
        sm._update_ui_state = mock.Mock()
        sm._apply_recording_context = mock.Mock()
        sm._maybe_pause_media = mock.Mock()
        sm._maybe_restart_continuous = mock.Mock()
        sm._begin_clip = mock.Mock(return_value=None)
        sm.recent_transcriptions = collections.deque(maxlen=10)
        sm._keep_my_voice = mock.Mock(side_effect=lambda text, audio, info, unprompted: (text, info))
        sm.bubble_has_focus = lambda: self.focus[0]
        sm.bubble_focus = mock.Mock()
        sm.bubble_focus.give_back.return_value = True
        sm.live = LiveWordsFlow(sm)
        return sm

    def _start(self, sm):
        sm._begin_recording()
        sm._recording_started_at -= 5      # held long enough to be a dictation

    def _pause(self, sm):
        from koli.voice_activity_detection import VadEvent
        sm.handle_vad_event(VadEvent.PHRASE_PAUSE)

    def _delivered(self, sm):
        return [c.args[0] for c in sm.clipboard_manager.deliver_transcription.call_args_list]

    def test_phrases_fill_the_bubble_and_are_written_once(self):
        sm = self._sm(phrases=['Hello there.', 'how are you?'])
        self._start(sm)
        self.assertEqual(sm.audio_recorder.start_recording.call_args.kwargs.get('phrase_pause'), 1.0)
        self._pause(sm)
        self._pause(sm)
        self.assertEqual(sm.live.session.text(), 'Hello there. How are you?')
        sm.stop_recording()
        self.assertEqual(self._delivered(sm), ['Hello there. How are you?'])
        self.assertEqual(sm.whisper_engine.transcribe_audio.call_count, 2, 'the silent tail is not transcribed')
        self.assertIsNone(sm.live.session)
        self.usage.record.assert_called_once()
        self.assertEqual(self.usage.record.call_args.args[0], 'phrases')

    def test_the_tail_with_speech_is_transcribed_into_the_bubble(self):
        sm = self._sm(phrases=['First part.', 'and the rest'])
        self._start(sm)
        self._pause(sm)
        sm.audio_recorder.continuous_vad.speech_since_phrase = True
        sm.stop_recording()
        self.assertEqual(self._delivered(sm), ['First part. And the rest'])

    def test_previous_phrases_prime_whisper(self):
        sm = self._sm(phrases=['Call Avigail.', 'she knows'])
        self._start(sm)
        prompts = []
        sm.whisper_engine.transcribe_audio.side_effect = lambda a: (prompts.append(sm.whisper_engine.initial_prompt)
                                                                    or ['Call Avigail.', 'she knows'][len(prompts) - 1])
        self._pause(sm)
        self._pause(sm)
        self.assertIn('Call Avigail.', prompts[1] or '')

    def test_an_empty_bubble_writes_nothing(self):
        sm = self._sm(phrases=[])
        self._start(sm)
        sm.stop_recording()
        sm.clipboard_manager.deliver_transcription.assert_not_called()
        sm.level_overlay.flash_failure.assert_called_with('No speech detected')

    def test_off_engine_keeps_todays_flow(self):
        sm = self._sm(engine='off', phrases=['whole thing'])
        self._start(sm)
        self.assertIsNone(sm.live.session)
        self.assertFalse(sm.audio_recorder.start_recording.call_args.kwargs.get('phrase_pause'))
        sm.stop_recording()
        self.assertEqual(self._delivered(sm), ['whole thing'])

    def test_auto_dictation_never_gets_a_session(self):
        sm = self._sm(engine='cloud')
        sm.config_manager.config['auto_dictation'] = {'silence_timeout_seconds': 5}
        sm._begin_recording(auto_started=True)
        self.assertIsNone(sm.live.session)

    def test_release_in_the_bubble_pauses_and_the_hotkey_resumes(self):
        sm = self._sm(phrases=['Send it to Jon.', 'John', 'thanks'])
        self._start(sm)
        self._pause(sm)
        session_id = sm.live.session.session_id
        self.assertTrue(sm.bubble_open())
        sm.bubble_focus.take.assert_called_once()
        sm.bubble_edit('Send it to Jon.', 11, 14)
        self.focus[0] = True
        sm.stop_recording()                                  # let go of the keys with the bubble focused
        sm.clipboard_manager.deliver_transcription.assert_not_called()
        self.assertFalse(self.recording[0])
        self.assertFalse(sm.live.session.listening)
        sm.start_recording()                                 # press again: resumes the same session
        self.assertTrue(self.recording[0])
        self.assertEqual(sm.live.session.session_id, session_id)
        self._pause(sm)
        self.assertEqual(sm.live.session.text(), 'Send it to John.')
        self.focus[0] = False                                # back in the app
        sm._recording_started_at -= 5
        sm.stop_recording()
        self.assertEqual(self._delivered(sm), ['Send it to John.'])
        sm.bubble_focus.give_back.assert_called_once()
        self.assertEqual(sm.bubble_focus.give_back.call_args.args[0]['hwnd'], 5)

    def test_the_window_gone_sends_the_text_to_the_fallback(self):
        sm = self._sm(phrases=['Keep this.'])
        self._start(sm)
        self._pause(sm)
        sm.bubble_open()
        sm.bubble_focus.give_back.return_value = False
        sm.bubble_write(False)
        sm.clipboard_manager.deliver_transcription.assert_not_called()
        sm.fallback_window.show.assert_called_once()
        self.assertEqual(sm.fallback_window.show.call_args.args[0], 'Keep this.')

    def test_write_and_send_from_the_bubble(self):
        sm = self._sm(phrases=['Ship it.'])
        self._start(sm)
        self._pause(sm)
        sm.bubble_open()
        self.focus[0] = True
        self.assertTrue(sm.bubble_write(True))
        call = sm.clipboard_manager.deliver_transcription.call_args
        self.assertEqual((call.args[0], call.args[1]), ('Ship it.', True))

    def test_hotkey_release_in_the_app_follows_the_rules_auto_send(self):
        sm = self._sm(phrases=['Hi.'], rule={'match': ['notepad.exe'], 'auto_send': True})
        self._start(sm)
        self._pause(sm)
        sm.stop_recording()
        self.assertTrue(sm.clipboard_manager.deliver_transcription.call_args.args[1])

    def test_a_late_phrase_after_cancel_is_dropped(self):
        sm = self._sm(phrases=['Late words.'])
        self._start(sm)
        old = sm.live.session
        sm.bubble_cancel()
        self.assertIsNone(sm.live.session)
        import numpy as np
        sm.live.on_phrase(np.ones(16000, dtype=np.float32) * 0.1, session=old)
        self.assertEqual(old.text(), '')
        sm.clipboard_manager.deliver_transcription.assert_not_called()

    def test_edited_text_is_not_polished(self):
        sm = self._sm(phrases=['teh plan.'], polish=True)
        self._start(sm)
        self._pause(sm)
        sm.bubble_open()
        sm.bubble_edit('The plan.', 9, 9)
        sm.bubble_write(False)
        self.assertEqual(self._delivered(sm), ['The plan.'])
        sm._ai_polish.assert_not_called()

    def test_unedited_text_is_polished_once(self):
        sm = self._sm(phrases=['The plan.'], polish=True)
        self._start(sm)
        self._pause(sm)
        sm.stop_recording()
        self.assertEqual(self._delivered(sm), ['The plan. [polished]'])
        sm._ai_polish.assert_called_once()

    def test_esc_with_the_bubble_focused_cancels_nothing(self):
        sm = self._sm(phrases=['Words.'])
        self._start(sm)
        self._pause(sm)
        sm.bubble_open()
        self.focus[0] = True
        self.assertFalse(sm.cancel_recording_hotkey_pressed())
        self.assertIsNotNone(sm.live.session)

    def test_esc_in_the_app_discards_and_keeps_it_in_history(self):
        sm = self._sm(phrases=['Words.'])
        self._start(sm)
        self._pause(sm)
        self.assertTrue(sm.cancel_recording_hotkey_pressed())
        self.assertIsNone(sm.live.session)
        sm.clipboard_manager.deliver_transcription.assert_not_called()
        self.assertTrue(self.record_transcript.called)

# ── Task 8: local, OpenAI and Gemini feed the bubble ────────────────────────

class _FakeOpenAI:
    instances = []

    def __init__(self, key, model, language, on_interim, on_final, on_error, **kw):
        import numpy as np
        self.key, self.model, self.language = key, model, language
        self.on_interim, self.on_final, self.on_error = on_interim, on_final, on_error
        self.fed, self.commits, self.finished, self.cancelled = [], 0, False, False
        self.failed, self.transcribed_any, self.seconds_sent = False, False, 0.0
        self.left = np.ones(16000, dtype=np.float32) * 0.1
        _FakeOpenAI.instances.append(self)

    def start(self):
        pass

    def feed(self, mono):
        self.fed.append(mono)

    def commit(self):
        self.commits += 1

    def finish(self, timeout=5.0):
        self.finished = True
        if not self.failed:
            self.left = None

    def cancel(self):
        self.cancelled = True

    def unsent_audio(self):
        return self.left


class CloudAndLocalEngineTests(unittest.TestCase):
    def setUp(self):
        self.base = LiveWordsFlowTests('test_off_engine_keeps_todays_flow')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        _FakeOpenAI.instances = []

    def _sm(self, engine, provider='openai', phrases=()):
        sm = self.base._sm(engine='cloud' if engine in ('openai', 'gemini') else engine, phrases=phrases)
        sm.config_manager.config['live_words']['cloud'] = {'provider': provider}
        sm.live._has_key = lambda p: True
        sm.live._key = lambda p: 'test-key'
        sm.live.openai_factory = _FakeOpenAI
        self.taps = []
        sm.audio_recorder.add_tap.side_effect = self.taps.append
        sm.audio_recorder.remove_tap.side_effect = lambda fn: self.taps.remove(fn) if fn in self.taps else None
        return sm

    def test_openai_words_reach_the_bubble_and_are_written(self):
        import numpy as np
        sm = self._sm('openai')
        self.base._start(sm)
        client = _FakeOpenAI.instances[-1]
        self.assertEqual((client.key, client.model, client.language), ('test-key', 'gpt-live-transcribe', 'en'))
        self.assertEqual(len(self.taps), 1)
        self.taps[0](np.zeros(160, dtype=np.float32), 0.0)
        self.assertEqual(len(client.fed), 1)
        client.on_interim('hello wor')
        self.assertEqual(sm.live.session.snapshot()['interim'], 'hello wor')
        client.on_final('Hello world.')
        self.base._pause(sm)                                     # a phrase pause commits the turn
        self.assertEqual(client.commits, 1)
        sm.whisper_engine.transcribe_audio.assert_not_called()
        sm.stop_recording()
        self.assertTrue(client.finished)
        self.assertEqual(self.taps, [])
        self.assertEqual(self.base._delivered(sm), ['Hello world.'])
        self.assertEqual(self.base.usage.record.call_args.args[0], 'cloud/openai/gpt-live-transcribe')

    def test_openai_failing_at_the_start_continues_as_phrases(self):
        sm = self._sm('openai', phrases=['Said while offline.', 'and after'])
        self.base._start(sm)
        client = _FakeOpenAI.instances[-1]
        client.failed = True
        client.on_error("Couldn't reach OpenAI")
        sm.live._wait_for_phrases()
        self.assertEqual(sm.live.session.text(), 'Said while offline.')   # the unsent audio, by Whisper
        self.base._pause(sm)
        self.assertEqual(sm.live.session.text(), 'Said while offline. And after')
        self.assertEqual(sm.live.choice.engine, 'phrases')

    def test_openai_dropping_mid_recording_keeps_every_word(self):
        sm = self._sm('openai', phrases=['the rest'])
        self.base._start(sm)
        client = _FakeOpenAI.instances[-1]
        client.on_final('First part,')
        client.transcribed_any = True
        client.failed = True
        client.on_error('OpenAI closed the connection')
        sm.live._wait_for_phrases()
        sm.stop_recording()
        self.assertEqual(self.base._delivered(sm), ['First part, the rest'])

    def test_gemini_transcribes_phrases_and_falls_back_to_whisper(self):
        sm = self._sm('gemini', provider='gemini', phrases=['by Whisper'])
        answers = [('From Gemini.', 100, 5), RuntimeError('quota')]

        def fake(audio, language, context, key, model):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        sm.live.gemini_transcribe = fake
        self.base._start(sm)
        self.base._pause(sm)
        self.base._pause(sm)
        self.assertEqual(sm.live.session.text(), 'From Gemini. By Whisper')
        sm.stop_recording()
        args = self.base.usage.record.call_args.args
        self.assertEqual(args[0], 'cloud/gemini/gemini-3.6-flash')
        self.assertEqual((args[4], args[5]), (100, 5))

    def test_local_words_are_a_preview_and_whisper_writes(self):
        sm = self._sm('local', phrases=['Whisper wins.'])
        sm.config_manager.get_whisper_config.return_value = {'language': 'en'}
        sm.audio_recorder.continuous_streaming = object()
        self.base._start(sm)
        self.assertEqual(sm.live.choice.engine, 'local')
        self.assertTrue(sm.audio_recorder.stream_partials)
        self.assertIsNone(sm.audio_recorder.start_recording.call_args.kwargs.get('phrase_pause'))
        sm.handle_streaming_result('whisper wins', False)
        self.assertEqual(sm.live.session.snapshot()['interim'], 'whisper wins')
        self.assertFalse(sm.bubble_open(), 'a preview never opens')
        sm.stop_recording()
        self.assertEqual(self.base._delivered(sm), ['Whisper wins.'])
        self.assertFalse(sm.audio_recorder.stream_partials)

# ── Task 9: bridge, bubble focus and placement, the hotkey focus rule ───────

class BubbleFocusRuleTests(unittest.TestCase):
    def _listener(self, bubble_focused):
        import unittest.mock as mock
        from koli.hotkey_listener import HotkeyListener
        listener = HotkeyListener.__new__(HotkeyListener)
        listener.logger = __import__('logging').getLogger('test')
        listener.is_paused = False
        listener.keys_armed = True
        listener.tap_latch = None
        listener.stop_key, listener.auto_send_key, listener.recording_hotkey = 'ctrl', 'alt', 'ctrl+win'
        listener.state_manager = mock.Mock()
        listener.state_manager.bubble_has_focus.return_value = bubble_focused
        listener.state_manager.is_auto_recording.return_value = False
        listener.state_manager.audio_recorder.get_recording_status.return_value = True
        return listener

    def test_stop_and_send_keys_are_editing_keys_in_the_bubble(self):
        import unittest.mock as mock
        listener = self._listener(bubble_focused=True)
        with mock.patch.object(type(listener), 'own_keys', staticmethod(lambda: False)):
            listener._stop_key_pressed()
            listener._auto_send_key_pressed()
        listener.state_manager.stop_recording.assert_not_called()

    def test_stop_and_send_keys_work_in_the_app(self):
        import unittest.mock as mock
        listener = self._listener(bubble_focused=False)
        with mock.patch.object(type(listener), 'own_keys', staticmethod(lambda: False)):
            listener._stop_key_pressed()
        listener.state_manager.stop_recording.assert_called_once()


class BubblePlacementTests(unittest.TestCase):
    def test_a_recording_field_wins_over_the_resting_spot(self):
        from koli.ui.floating_bar import bar_origin
        area, size = (0, 0, 1920, 1040), (420, 120)
        field = (100, 500, 900, 540)
        x, y = bar_origin(area, size, 1.0, 'bottom-center', field=field)
        self.assertEqual(x, 906)                      # just past the field's right end
        self.assertEqual(y, 520 - 60)

    def test_probe_focused_is_on_both_platforms(self):
        import importlib
        for name in ('koli.platform.windows.input_watch', 'koli.platform.macos.input_watch'):
            try:
                module = importlib.import_module(name)
            except Exception:
                continue
            self.assertTrue(callable(getattr(module, 'probe_focused', None)), name)
        source = (ROOT / 'src' / 'koli' / 'platform' / 'macos' / 'input_watch.py').read_text(encoding='utf-8')
        self.assertIn('def probe_focused', source)


class BubbleBridgeTests(unittest.TestCase):
    def _bridge(self):
        import unittest.mock as mock
        from koli.ui.bridge import Bridge
        host = mock.Mock()
        return Bridge(host), host.ctx.state_manager

    def test_calls_reach_the_state_manager(self):
        bridge, sm = self._bridge()
        sm.bubble_write.return_value = True
        self.assertEqual(bridge.bubble_write(True), {'ok': True, 'data': True})
        sm.bubble_write.assert_called_once_with(True)
        bridge.bubble_edit('fixed text', 3, 5)
        sm.bubble_edit.assert_called_once_with('fixed text', 3, 5, None)
        bridge.bubble_open()
        sm.bubble_open.assert_called_once()
        bridge.bubble_listen(False)
        sm.bubble_listen.assert_called_once_with(False)
        bridge.bubble_cancel()
        sm.bubble_cancel.assert_called_once()

    def test_bubble_page_listens_to_the_session(self):
        from koli.ui.host import BAR_TOPICS
        self.assertIn('live_words', BAR_TOPICS)
# ── Task 10: the bubble pane ────────────────────────────────────────────────

class BubblePaneTests(unittest.TestCase):
    def setUp(self):
        self.js = (ROOT / 'src' / 'koli' / 'ui' / 'web' / 'js' / 'bar.js').read_text(encoding='utf-8')

    def test_the_pane_calls_every_bubble_bridge_call(self):
        for name in ('bubble_open', 'bubble_edit', 'bubble_write', 'bubble_listen', 'bubble_cancel'):
            self.assertIn(f"call('{name}'", self.js, name)

    def test_the_pane_follows_the_session(self):
        self.assertIn("on('live_words'", self.js)
        self.assertIn('contenteditable', self.js)

    def test_offsets_are_code_points(self):
        self.assertIn('codePoints', self.js)

    def test_the_page_reports_when_it_shows_the_pane(self):
        self.assertIn('resting, pane)', self.js)
# ── Task 11: the usage card and the doctor line ─────────────────────────────

class LiveWordsReportTests(unittest.TestCase):
    def test_doctor_line_names_the_engine_and_the_key(self):
        from koli.live_words import describe
        self.assertEqual(describe({'engine': 'off'}, lambda p: True), 'off')
        self.assertEqual(describe({'engine': 'phrases'}, lambda p: True), 'Phrases (Whisper)')
        self.assertIn('English only', describe({'engine': 'local'}, lambda p: True))
        self.assertEqual(describe({'engine': 'cloud', 'cloud': {'provider': 'openai'}}, lambda p: True),
                         'Cloud: OpenAI gpt-live-transcribe (key found)')
        self.assertIn('no Gemini key', describe({'engine': 'cloud', 'cloud': {'provider': 'gemini'}}, lambda p: False))

    def test_doctor_prints_it(self):
        source = (ROOT / 'src' / 'koli' / 'doctor.py').read_text(encoding='utf-8')
        self.assertIn('Words while you speak', source)

    def test_bridge_returns_the_usage_rows(self):
        import unittest.mock as mock
        from koli.ui.bridge import Bridge
        host = mock.Mock()
        host.ctx.config_manager.get_path.return_value = {'openai/x': {'per_minute': 1}}
        with mock.patch('koli.live_words_usage.summary', return_value=[{'key': 'phrases'}]) as summary:
            self.assertEqual(Bridge(host).live_words_usage(), {'ok': True, 'data': [{'key': 'phrases'}]})
        summary.assert_called_once_with({'openai/x': {'per_minute': 1}})

    def test_history_screen_shows_the_card(self):
        js = (ROOT / 'src' / 'koli' / 'ui' / 'web' / 'js' / 'screens' / 'history.js').read_text(encoding='utf-8')
        self.assertIn("call('live_words_usage'", js)
        self.assertIn('Words while you speak', js)

# ── Final review fixes ──────────────────────────────────────────────────────

class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.base = LiveWordsFlowTests('test_off_engine_keeps_todays_flow')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        _FakeOpenAI.instances = []

    def _paused_open_session(self, phrases=('Fix this.',)):
        sm = self.base._sm(phrases=list(phrases))
        self.base._start(sm)
        self.base._pause(sm)
        sm.bubble_open()
        self.base.focus[0] = True
        sm.stop_recording()                     # paused, the text waits in the bubble
        self.base.focus[0] = False
        return sm

    # 1. Clicking back into the field must not wipe a paused session.
    def test_auto_dictation_waits_while_the_bubble_holds_text(self):
        sm = self._paused_open_session()
        sm.config_manager.config['auto_dictation'] = {'silence_timeout_seconds': 5}
        self.assertFalse(sm.begin_auto_recording())
        self.assertEqual(sm.live.session.text(), 'Fix this.')

    # 2. The length limit stops the recorder before Koli hears about it.
    def test_max_duration_with_local_words_still_transcribes(self):
        import numpy as np
        sm = self.base._sm(engine='local', phrases=['A very long dictation.'])
        sm.audio_recorder.continuous_streaming = object()
        self.base._start(sm)
        self.base.recording[0] = False          # the recorder already stopped itself
        sm.handle_max_recording_duration_reached(np.ones(16000, dtype=np.float32) * 0.1)
        self.assertEqual(self.base._delivered(sm), ['A very long dictation.'])

    # 3. Enter then Write (or Esc) while the last phrase is still being written.
    def test_a_second_finish_during_the_first_is_ignored(self):
        sm = self.base._sm(phrases=[])
        self.base._start(sm)
        sm.audio_recorder.continuous_vad.speech_since_phrase = True

        def tail(audio):
            sm.bubble_write(True)                # a second press while the tail is transcribed
            sm.bubble_cancel()
            return 'The tail.'
        sm.whisper_engine.transcribe_audio.side_effect = tail
        sm.stop_recording()
        self.assertEqual(self.base._delivered(sm), ['The tail.'])

    # 4. Phrases go into the bubble in the order they were spoken.
    def test_phrases_are_written_in_spoken_order(self):
        import threading
        import time
        import numpy as np
        sm = self.base._sm(phrases=[])
        self.base._start(sm)
        first = np.ones(16000, dtype=np.float32) * 0.1
        second = np.ones(16000, dtype=np.float32) * 0.2

        def transcribe(audio):
            if float(audio[0]) < 0.15:
                time.sleep(0.3)
                return 'First.'
            return 'Second.'
        sm.whisper_engine.transcribe_audio.side_effect = transcribe
        session = sm.live.session
        t1, t2 = sm.live.ticket(), sm.live.ticket()
        b = threading.Thread(target=sm.live.on_phrase, args=(second,), kwargs={'session': session, 'ticket': t2})
        a = threading.Thread(target=sm.live.on_phrase, args=(first,), kwargs={'session': session, 'ticket': t1})
        b.start()
        time.sleep(0.05)
        a.start()
        a.join(3)
        b.join(3)
        self.assertEqual(session.text(), 'First. Second.')

    # 5. OpenAI failing while Koli waits for its last words.
    def test_openai_failing_during_finish_sends_the_rest_to_whisper(self):
        sm = self.base._sm(engine='cloud', phrases=['the last words'])
        sm.config_manager.config['live_words']['cloud'] = {'provider': 'openai'}
        sm.live._has_key = lambda p: True
        sm.live._key = lambda p: 'k'
        sm.live.openai_factory = _FakeOpenAI
        self.base._start(sm)
        client = _FakeOpenAI.instances[-1]
        client.on_final('Words from OpenAI,')

        def finish(timeout=5.0):
            client.failed = True               # dropped during the wait
            client.on_error('OpenAI closed the connection')
        client.finish = finish
        sm.stop_recording()
        self.assertEqual(self.base._delivered(sm), ['Words from OpenAI, the last words'])

    def test_a_tiny_last_turn_is_not_committed(self):
        import numpy as np
        t = OpenAILiveTests('test_no_language_sends_no_languages')
        c = t._client()
        c.start()
        self.assertTrue(_wait(lambda: t.sock.sent))
        c.feed(np.zeros(800, dtype=np.float32))     # 50 ms
        c.finish(timeout=0.2)
        self.assertNotIn('input_audio_buffer.commit', [m['type'] for m in t.sock.sent])
        self.assertIsNone(c.unsent_audio())

    def test_commit_empty_is_not_fatal(self):
        t = OpenAILiveTests('test_no_language_sends_no_languages')
        c = t._client()
        c.start()
        self.assertTrue(_wait(lambda: t.sock.sent))
        t.sock.push(type='error', error={'code': 'input_audio_buffer_commit_empty', 'message': 'buffer too small'})
        import time
        time.sleep(0.2)
        self.assertFalse(c.failed)
        c.cancel()

    # 6. Once the bubble has focus, the app's rule still decides formatting.
    def test_the_app_rule_is_read_once_per_session(self):
        sm = self._paused_open_session(phrases=('Fix this.', 'more'))
        calls = sm.app_rules.match_for_foreground.call_count
        sm._apply_recording_context.reset_mock()
        sm.start_recording()                      # resume with the bubble in front
        self.base._pause(sm)
        self.assertEqual(sm.app_rules.match_for_foreground.call_count, calls)
        sm._apply_recording_context.assert_not_called()

    # 8. A tap is still a tap.
    def test_a_tap_with_a_words_engine_is_refused(self):
        sm = self.base._sm(phrases=['Thank you.'])
        sm._begin_recording()                     # released at once
        sm.stop_recording()
        sm.clipboard_manager.deliver_transcription.assert_not_called()
        sm.whisper_engine.transcribe_audio.assert_not_called()
        sm.level_overlay.flash_failure.assert_called_with('Hold the keys while you talk')

    def test_only_my_voice_filters_bubble_phrases(self):
        import unittest.mock as mock
        sm = self.base._sm(phrases=['TV in the background.'])
        sm._keep_my_voice = mock.Mock(return_value=('', {}))
        self.base._start(sm)
        self.base._pause(sm)
        self.assertEqual(sm.live.session.text(), '')
        sm._keep_my_voice.assert_called_once()

    def test_a_bubble_dictation_keeps_a_clip(self):
        sm = self.base._sm(phrases=['One.', 'two'])
        sm._begin_clip.return_value = 'clip-7'
        self.base._start(sm)
        self.base._pause(sm)
        self.base._pause(sm)
        sm.stop_recording()
        sm._begin_clip.assert_called_once()
        self.assertEqual(len(sm._begin_clip.call_args.args[0]), 32000)      # both phrases' audio
        self.assertEqual(self.base.record_transcript.call_args.kwargs.get('clip_id'), 'clip-7')

    # 9. An edit typed against older text must not erase a phrase that landed.
    def test_a_stale_edit_is_rejected(self):
        from koli.dictation_session import DictationSession
        s = DictationSession('phrases', 1, None)
        s.add_final('One.')
        s.open()
        rev = s.snapshot()['rev']
        s.add_final('two')
        self.assertFalse(s.edit('One!', 4, 4, rev=rev))
        self.assertEqual(s.text(), 'One. Two')
        self.assertTrue(s.edit('One. Two!', 9, 9, rev=s.snapshot()['rev']))


class GiveBackTests(unittest.TestCase):
    # 7. Writing goes ahead only when the app is really in front again.
    def test_give_back_checks_the_window_is_in_front(self):
        import unittest.mock as mock
        from koli.ui.host import UiHost
        host = UiHost.__new__(UiHost)
        host.bar_hwnd, host._last_external_hwnd = 9, 0
        with mock.patch('koli.ui.host.win32') as win32, mock.patch('koli.ui.host.time'):
            win32.is_window.return_value = True
            win32.foreground.return_value = 77              # something else stayed in front
            self.assertFalse(host.give_back({'hwnd': 5}))
            win32.foreground.return_value = 5
            self.assertTrue(host.give_back({'hwnd': 5}))

if __name__ == '__main__':
    unittest.main()
