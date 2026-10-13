# tests/test_llm.py
# The AI provider layer: routes and fallbacks, reasoning stripping, the wire
# formats (OpenAI chat with and without streaming, Ollama, Codex's Responses
# stream, Gemini mapping), subscription token refresh with rotation, the
# client-version rules, and the encrypted credential files. A small local HTTP
# server stands in for the providers; nothing reaches the internet, and no
# file is written to the real settings folder.
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ── Helpers ──────────────────────────────────────────────────────────────────

class FakeConfig:
    def __init__(self, values: dict):
        self.values = values

    def get_path(self, path, default=None):
        return self.values.get(path, default)


# Serves canned answers by path. `routes[path]` is (status, body, content
# type) or a function(handler, body) -> that tuple. Every request is recorded.
class FakeServer:
    def __init__(self, routes: dict):
        self.routes = routes
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length) if length else b''
                outer.requests.append({'path': self.path, 'body': body, 'headers': dict(self.headers)})
                route = outer.routes.get(self.path.split('?')[0])
                if route is None:
                    status, payload, ctype = 404, '{"error": "not found"}', 'application/json'
                else:
                    status, payload, ctype = route(self, body) if callable(route) else route
                data = payload.encode('utf-8') if isinstance(payload, str) else payload
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', ctype)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except ConnectionError:
                    pass   # the client gave up first (the deadline test)

            do_GET = _serve
            do_POST = _serve

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def as_json(obj, status=200):
    return status, json.dumps(obj), 'application/json'


def as_sse(events: list):
    text = ''.join(f"data: {json.dumps(e) if not isinstance(e, str) else e}\n\n" for e in events)
    return 200, text, 'text/event-stream'


# Credentials and client versions go to a temporary folder.
class TempAppData:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name)
        self.patches = [
            mock.patch('koli.llm.credentials.folder', lambda: path),
            mock.patch('koli.llm.client_version._file', lambda: path / 'ai-client-versions.json'),
        ]
        for p in self.patches:
            p.start()
        return path

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


# ── Routes ───────────────────────────────────────────────────────────────────

class RoutingTests(unittest.TestCase):
    def test_parse_splits_at_the_first_slash(self):
        from koli.llm.routing import parse
        self.assertEqual(parse('lmstudio/qwen/qwen3-8b'), ('lmstudio', 'qwen/qwen3-8b'))
        self.assertEqual(parse(' Codex/gpt-5.5 '), ('codex', 'gpt-5.5'))
        self.assertIsNone(parse('llama3.2'))
        self.assertIsNone(parse(''))
        self.assertIsNone(parse('ollama/'))

    def test_feature_model_then_fallbacks_without_repeats(self):
        from koli.llm.routing import route
        cfg = FakeConfig({'ai.model': 'codex/gpt-5.5', 'ai.fallbacks': ['lmstudio/qwen3', 'codex/gpt-5.5', 'bad'],
                          'ai.timeout': 30, 'ai.polish.model': 'ollama/llama3.2', 'ai.polish.timeout': 5})
        models, timeout = route(cfg.get_path, 'polish')
        # The feature's own model, then the default fallbacks (bad names skipped).
        self.assertEqual(models, ['ollama/llama3.2', 'lmstudio/qwen3', 'codex/gpt-5.5'])
        self.assertEqual(timeout, 5)
        models, timeout = route(cfg.get_path, 'rewrite')
        self.assertEqual(models, ['codex/gpt-5.5', 'lmstudio/qwen3'])
        self.assertEqual(timeout, 30)
        models, _ = route(cfg.get_path, 'rewrite', override='gemini/gemini-2.5-flash', fallbacks=False)
        self.assertEqual(models, ['gemini/gemini-2.5-flash'])
        # An old-style transform model without a provider is ignored.
        models, _ = route(cfg.get_path, 'rewrite', override='llama3.2')
        self.assertEqual(models[0], 'codex/gpt-5.5')

    def test_grok_pending_answer_in_a_200_is_not_a_login(self):
        from koli.llm.oauth import Grok
        with mock.patch('koli.llm.http.post_form', return_value={'error': 'authorization_pending'}), \
                mock.patch.object(Grok, 'user_agent', return_value='test'):
            self.assertIsNone(Grok()._poll_device({'device_code': 'd'}))
        with mock.patch('koli.llm.http.post_form', return_value={'access_token': 'a', 'refresh_token': 'r'}), \
                mock.patch.object(Grok, 'user_agent', return_value='test'):
            self.assertEqual(Grok()._poll_device({'device_code': 'd'})['access_token'], 'a')

    def test_no_model_chosen_is_a_plain_error(self):
        from koli import llm
        llm.configure(FakeConfig({}))
        result = llm.complete('polish', [{'role': 'user', 'content': 'hi'}])
        self.assertFalse(result.ok)
        self.assertIn('Settings › AI', result.error)

    def test_strip_reasoning(self):
        from koli.llm import strip_reasoning
        self.assertEqual(strip_reasoning('<think>hmm\nlong</think>\nHello.'), 'Hello.')
        self.assertEqual(strip_reasoning('<THINKING>x</THINKING>Hi'), 'Hi')
        self.assertEqual(strip_reasoning('<think>cut off before the answer'), '')
        self.assertEqual(strip_reasoning('Plain answer'), 'Plain answer')


# ── Local providers end to end ───────────────────────────────────────────────

class LocalProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = TempAppData()
        self.temp.__enter__()

    def tearDown(self):
        self.temp.__exit__(None, None, None)

    def test_ollama_chat_and_models(self):
        from koli import llm
        server = FakeServer({
            '/api/chat': as_json({'message': {'content': 'Polished.'}, 'prompt_eval_count': 9, 'eval_count': 2}),
            '/api/tags': as_json({'models': [{'name': 'llama3.2:latest'}]}),
        })
        try:
            llm.configure(FakeConfig({'ai.model': 'ollama/llama3.2', 'ai.ollama.address': server.url}))
            result = llm.complete('polish', [{'role': 'user', 'content': 'polish me'}], json=True)
            self.assertEqual((result.text, result.provider, result.tokens_in), ('Polished.', 'ollama', 9))
            sent = json.loads(server.requests[0]['body'])
            self.assertEqual((sent['model'], sent['format'], sent['stream']), ('llama3.2', 'json', False))
            self.assertEqual(llm.list_models('ollama'), [{'id': 'llama3.2:latest', 'label': 'llama3.2:latest'}])
            self.assertTrue(llm.provider_status('ollama')[0]['connected'])
        finally:
            server.close()

    def test_lmstudio_native_model_list_and_streamed_answer(self):
        from koli import llm
        server = FakeServer({
            '/api/v1/models': as_json({'models': [
                {'type': 'llm', 'key': 'qwen/qwen3-8b', 'display_name': 'Qwen3 8B', 'loaded_instances': [{'id': 'x'}]},
                {'type': 'embedding', 'key': 'nomic-embed'}]}),
            '/v1/chat/completions': as_sse([
                {'choices': [{'delta': {'content': '<think>plan</think>Hel'}}]},
                {'choices': [{'delta': {'content': 'lo'}}]}, '[DONE]']),
        })
        try:
            # A pasted /v1 is trimmed.
            llm.configure(FakeConfig({'ai.model': 'lmstudio/qwen/qwen3-8b', 'ai.lmstudio.address': server.url + '/v1'}))
            models = llm.list_models('lmstudio')
            self.assertEqual(models, [{'id': 'qwen/qwen3-8b', 'label': 'Qwen3 8B', 'loaded': True}])
            pieces = []
            result = llm.complete('rewrite', [{'role': 'user', 'content': 'x'}], on_text=pieces.append, json=True)
            self.assertEqual(result.text, 'Hello')
            self.assertEqual(''.join(pieces), '<think>plan</think>Hello')
            sent = json.loads(server.requests[-1]['body'])
            self.assertTrue(sent['stream'])
            self.assertNotIn('response_format', sent)       # LM Studio gets JSON asked in words
            self.assertIn('JSON', sent['messages'][0]['content'])
        finally:
            server.close()

    def test_falls_back_when_the_first_model_is_down(self):
        from koli import llm
        server = FakeServer({'/v1/chat/completions': as_json({'choices': [{'message': {'content': 'from LM Studio'}}]})})
        try:
            llm.configure(FakeConfig({'ai.model': 'ollama/llama3.2', 'ai.ollama.address': 'http://127.0.0.1:9',
                                      'ai.fallbacks': ['lmstudio/qwen3'], 'ai.lmstudio.address': server.url}))
            result = llm.complete('rewrite', [{'role': 'user', 'content': 'x'}])
            self.assertEqual((result.text, result.provider), ('from LM Studio', 'lmstudio'))
        finally:
            server.close()

    def test_deadline_covers_the_whole_call(self):
        from koli import llm

        def slow(handler, body):
            time.sleep(1.5)
            return as_json({'message': {'content': 'late'}})
        server = FakeServer({'/api/chat': slow})
        try:
            llm.configure(FakeConfig({'ai.model': 'ollama/a', 'ai.fallbacks': ['ollama/b'],
                                      'ai.ollama.address': server.url, 'ai.polish.timeout': 1}))
            started = time.monotonic()
            result = llm.complete('polish', [{'role': 'user', 'content': 'x'}])
            self.assertFalse(result.ok)
            self.assertLess(time.monotonic() - started, 1.6)
            self.assertEqual(len(server.requests), 1)   # no time left for the fallback
        finally:
            server.close()


# ── Wire formats ─────────────────────────────────────────────────────────────

class WireFormatTests(unittest.TestCase):
    def test_sse_events(self):
        from koli.llm.sse import iter_events
        raw = [b'event: a\n', b'data: {"x": 1}\n', b'\n', b': comment\n', b'data: line1\n', b'data: line2\n', b'\n',
               b'data: [DONE]\n', b'\n', b'data: after\n', b'\n']
        self.assertEqual(list(iter_events(raw)), [('a', '{"x": 1}'), (None, 'line1\nline2')])

    def test_codex_stream_text_usage_and_fallback(self):
        from koli.llm.transports.codex import parse_stream
        events = [(None, json.dumps({'type': 'response.output_text.delta', 'delta': 'Hi '})),
                  (None, json.dumps({'type': 'response.output_text.delta', 'delta': 'there'})),
                  (None, json.dumps({'type': 'response.completed',
                                     'response': {'usage': {'input_tokens': 7, 'output_tokens': 2}}}))]
        got = []
        self.assertEqual(parse_stream(events, got.append), ('Hi there', 7, 2))
        self.assertEqual(got, ['Hi ', 'there'])
        # No deltas: the completed output is used.
        done_only = [(None, json.dumps({'type': 'response.completed', 'response': {'output': [
            {'type': 'reasoning'}, {'type': 'message', 'content': [{'type': 'output_text', 'text': 'Whole'}]}]}}))]
        self.assertEqual(parse_stream(done_only)[0], 'Whole')

    def test_codex_stream_failure_raises(self):
        from koli.llm.transports.codex import parse_stream
        from koli.llm.types import ProviderError
        events = [(None, json.dumps({'type': 'response.failed', 'response': {'error': {'message': 'usage limit'}}}))]
        with self.assertRaises(ProviderError) as ctx:
            parse_stream(events)
        self.assertIn('usage limit', str(ctx.exception))

    def test_codex_input_mapping(self):
        from koli.llm.transports.codex import to_responses_input
        instructions, items = to_responses_input([
            {'role': 'system', 'content': 'A'}, {'role': 'system', 'content': 'B'},
            {'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': 'a'}])
        self.assertEqual(instructions, 'A\n\nB')
        self.assertEqual(items, [{'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': 'a'}])
        self.assertTrue(to_responses_input([{'role': 'user', 'content': 'q'}])[0])   # never empty

    def test_gemini_mapping(self):
        from koli.llm.transports.gemini import to_gemini
        body = to_gemini([{'role': 'system', 'content': 'Be brief'}, {'role': 'user', 'content': 'q'},
                          {'role': 'assistant', 'content': 'a'}])
        self.assertEqual(body['systemInstruction'], {'parts': [{'text': 'Be brief'}]})
        self.assertEqual([c['role'] for c in body['contents']], ['user', 'model'])


# ── Subscriptions ────────────────────────────────────────────────────────────

class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TempAppData()
        self.temp.__enter__()

    def tearDown(self):
        self.temp.__exit__(None, None, None)

    def _account(self, token_url):
        from koli.llm.oauth import Grok

        class TestAccount(Grok):
            name = 'test-sub'
            label = 'Test'

            def user_agent(self):
                return 'test'
        account = TestAccount()
        account.token_url = token_url
        return account

    def _save(self, **fields):
        from koli.llm import credentials
        rec = {'access': 'A1', 'refresh': 'R1', 'expires': time.time() + 3600, 'obtained_at': time.time(),
               'last_refresh': time.time(), **fields}
        credentials.save('test-sub', rec)

    def test_fresh_token_is_used_without_refreshing(self):
        server = FakeServer({})
        try:
            account = self._account(server.url + '/token')
            self._save()
            self.assertEqual(account.bearer()[0], 'A1')
            self.assertEqual(server.requests, [])
        finally:
            server.close()

    def test_expiring_token_refreshes_and_keeps_old_refresh_when_none_returned(self):
        server = FakeServer({'/token': as_json({'access_token': 'A2', 'expires_in': 3600})})
        try:
            account = self._account(server.url + '/token')
            self._save(expires=time.time() + 60)
            self.assertEqual(account.bearer()[0], 'A2')
            self.assertEqual(account.record()['refresh'], 'R1')
            self.assertIn(b'grant_type=refresh_token', server.requests[0]['body'])
        finally:
            server.close()

    def test_stale_bearer_already_replaced_is_not_refreshed_again(self):
        server = FakeServer({'/token': as_json({'access_token': 'A3', 'refresh_token': 'R3'})})
        try:
            account = self._account(server.url + '/token')
            self._save(access='A2')    # someone else already rotated A1 → A2
            self.assertEqual(account.bearer(stale='A1')[0], 'A2')
            self.assertEqual(server.requests, [])
            # The server refused A2 itself: now it refreshes, once.
            self.assertEqual(account.bearer(stale='A2')[0], 'A3')
            self.assertEqual(account.record()['refresh'], 'R3')
        finally:
            server.close()

    def test_dead_refresh_token_needs_reconnect(self):
        from koli.llm.types import ReconnectNeeded
        server = FakeServer({'/token': as_json({'error': 'invalid_grant'}, status=400)})
        try:
            account = self._account(server.url + '/token')
            self._save(expires=0)
            with self.assertRaises(ReconnectNeeded):
                account.bearer()
            self.assertTrue(account.status()['reconnect'])
            with self.assertRaises(ReconnectNeeded):   # no second network call
                account.bearer()
            self.assertEqual(len(server.requests), 1)
        finally:
            server.close()

    def test_codex_identity_from_jwt(self):
        import base64
        from koli.llm.oauth import CODEX

        def jwt(claims):
            body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
            return f"h.{body}.s"
        ident = CODEX._identity({'id_token': jwt({'email': 'a@b.c'}),
                                 'access_token': jwt({'https://api.openai.com/auth': {'chatgpt_account_id': 'acc-1'}})}, {})
        self.assertEqual(ident, {'account_id': 'acc-1', 'email': 'a@b.c'})


class ClientVersionTests(unittest.TestCase):
    def test_highest_and_426_minimum(self):
        from koli.llm import client_version as cv
        self.assertEqual(cv.highest('0.160.0', 'v0.162.1', None, 'junk'), 'v0.162.1')
        self.assertEqual(cv.minimum_from_426('Please update to version 1.0.60 or later.'), '1.0.60')
        self.assertEqual(cv.minimum_from_426('nope'), '')

    def test_refresh_never_steps_backwards(self):
        from koli.llm import client_version as cv
        with TempAppData(), mock.patch('koli.llm.http.get_json', side_effect=OSError('offline')):
            self.assertEqual(cv.refresh('grok', minimum='9.9.9', reason='426'), '9.9.9')
            self.assertEqual(cv.refresh('grok'), '9.9.9')
            with mock.patch.object(cv, 'refresh_in_background'):
                self.assertEqual(cv.current('grok'), '9.9.9')


class CredentialTests(unittest.TestCase):
    def test_round_trip_and_delete(self):
        from koli.llm import credentials
        with TempAppData() as folder:
            credentials.save('gemini', {'key': 'secret-key-123'})
            if sys.platform == 'win32':   # DPAPI: the key isn't readable in the file
                self.assertNotIn(b'secret-key-123', (folder / 'gemini.bin').read_bytes())
            self.assertEqual(credentials.load('gemini'), {'key': 'secret-key-123'})
            credentials.delete('gemini')
            self.assertEqual(credentials.load('gemini'), {})

    def test_schema_validates_model_names(self):
        from koli.settings import schema
        setting = schema.get('ai.model')
        self.assertEqual(schema.coerce(setting, ' codex/gpt-5.5 '), 'codex/gpt-5.5')
        self.assertEqual(schema.coerce(setting, ''), '')
        with self.assertRaises(ValueError):
            schema.coerce(setting, 'llama3.2')
        self.assertEqual(schema.coerce(schema.get('ai.fallbacks'), ['a/b', 'a/b', '']), ['a/b'])


if __name__ == '__main__':
    unittest.main()
