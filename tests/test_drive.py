# tests/test_drive.py
# The Google Drive connection: the pasted OAuth client, token refresh with the
# client secret, the browser login caught on the loopback port, and the Drive
# v3 calls (upload, download, list, folders, share links). A small local HTTP
# server stands in for Google; nothing reaches the internet, and no file is
# written to the real credentials folder.
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koli.drive import auth  # noqa: E402
from koli.llm import credentials, http  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────

def as_json(obj, status=200, headers=None):
    return status, json.dumps(obj), 'application/json', headers or {}


# Serves canned answers by path for any method. `routes[path]` is
# (status, body, content type, extra headers) or a function(request) -> that.
# Every request is recorded as {'method', 'path', 'query', 'body', 'headers'}.
class FakeGoogle:
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
                parsed = urlparse(self.path)
                req = {'method': self.command, 'path': parsed.path, 'query': parse_qs(parsed.query),
                       'body': body, 'headers': {k.lower(): v for k, v in self.headers.items()}}
                outer.requests.append(req)
                route = outer.routes.get(parsed.path)
                if route is None:
                    status, payload, ctype, extra = 404, '{"error": "not found"}', 'application/json', {}
                else:
                    status, payload, ctype, extra = route(req) if callable(route) else route
                data = payload.encode('utf-8') if isinstance(payload, str) else payload
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def of(self, method, path):
        return [r for r in self.requests if r['method'] == method and r['path'] == path]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


# Credentials go to a temporary folder; Google's endpoints point at a fake.
class DriveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name)
        self.patches = [mock.patch('koli.llm.credentials.folder', lambda: path)]
        for p in self.patches:
            p.start()
        self.google = None

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()
        if self.google:
            self.google.close()

    def serve(self, routes: dict) -> FakeGoogle:
        self.google = FakeGoogle(routes)
        for name in ('TOKEN_URL', 'USERINFO_URL'):
            self.patches.append(mock.patch.object(auth, name, f"{self.google.url}/{name.split('_')[0].lower()}"))
            self.patches[-1].start()
        return self.google

    def connected(self, **fields):
        rec = {'access': 'A1', 'refresh': 'R1', 'expires': time.time() + 3600, 'obtained_at': time.time(),
               'last_refresh': time.time(), 'email': 'studio@perspective-media.me', **fields}
        credentials.save(auth.ACCOUNT_RECORD, rec)
        return rec


def form(req) -> dict:
    return {k: v[0] for k, v in parse_qs(req['body'].decode()).items()}


# ── The pasted OAuth client ──────────────────────────────────────────────────

class ClientStoreTests(DriveCase):
    def test_set_client_keeps_secret_when_masked(self):
        auth.set_client('id-1', 'GOCSPX-abcd1234')
        auth.set_client('id-1', 'GOCSPX-••••1234')
        self.assertEqual(auth.get_client()['client_secret'], 'GOCSPX-abcd1234')
        self.assertEqual(auth.client_hint(), 'GOCSPX-••••1234')

    def test_unset_client_is_empty_strings(self):
        self.assertEqual(auth.get_client(), {'client_id': '', 'client_secret': ''})
        self.assertEqual(auth.client_hint(), '')

    def test_changing_client_id_clears_saved_login(self):
        auth.set_client('id-1', 's')
        self.connected()
        auth.set_client('id-2', 's')
        self.assertEqual(credentials.load(auth.ACCOUNT_RECORD), {})

    def test_same_client_id_keeps_saved_login(self):
        auth.set_client('id-1', 's')
        self.connected()
        auth.set_client('id-1', 'GOCSPX-••••')
        self.assertEqual(credentials.load(auth.ACCOUNT_RECORD)['refresh'], 'R1')


# ── Token refresh ────────────────────────────────────────────────────────────

class RefreshTests(DriveCase):
    def setUp(self):
        super().setUp()
        auth.set_client('id-1', 'secret-1')
        self.account = auth.GoogleAccount()

    def test_refresh_sends_client_secret_and_keeps_refresh_token(self):
        google = self.serve({'/token': as_json({'access_token': 'A2', 'expires_in': 3600})})
        self.connected(expires=time.time() + 60)   # due: expires within 5 minutes
        self.assertEqual(self.account.bearer(), 'A2')
        sent = form(google.of('POST', '/token')[0])
        self.assertEqual(sent['client_secret'], 'secret-1')
        self.assertEqual(sent['refresh_token'], 'R1')
        self.assertEqual(sent['grant_type'], 'refresh_token')
        self.assertEqual(credentials.load(auth.ACCOUNT_RECORD)['refresh'], 'R1')

    def test_fresh_token_is_returned_without_a_request(self):
        google = self.serve({})
        self.connected()
        self.assertEqual(self.account.bearer(), 'A1')
        self.assertEqual(google.requests, [])

    def test_stale_bearer_forces_one_refresh(self):
        self.serve({'/token': as_json({'access_token': 'A2', 'expires_in': 3600})})
        self.connected()
        self.assertEqual(self.account.bearer(stale='A1'), 'A2')

    def test_invalid_grant_marks_dead_and_raises(self):
        self.serve({'/token': as_json({'error': 'invalid_grant'}, status=400)})
        self.connected(expires=time.time() + 60)
        with self.assertRaises(auth.DriveNotConnected):
            self.account.bearer()
        status = self.account.status()
        self.assertTrue(status['reconnect'])
        self.assertFalse(status['connected'])

    def test_invalid_client_marks_dead(self):
        self.serve({'/token': as_json({'error': 'invalid_client'}, status=401)})
        self.connected(expires=time.time() + 60)
        with self.assertRaises(auth.DriveNotConnected):
            self.account.bearer()
        self.assertTrue(self.account.status()['reconnect'])

    def test_bearer_without_record_raises(self):
        with self.assertRaisesRegex(auth.DriveNotConnected, 'Google Drive'):
            self.account.bearer()

    def test_status_reports_configured_and_email(self):
        self.connected()
        status = self.account.status()
        self.assertTrue(status['configured'])
        self.assertTrue(status['connected'])
        self.assertEqual(status['email'], 'studio@perspective-media.me')

    def test_disconnect_deletes_only_the_local_record(self):
        google = self.serve({})
        self.connected()
        self.account.disconnect()
        self.assertEqual(credentials.load(auth.ACCOUNT_RECORD), {})
        self.assertEqual(google.requests, [])
        self.assertEqual(auth.get_client()['client_id'], 'id-1')


# ── Browser login on the loopback port ──────────────────────────────────────

def callback(port: int, query: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?{query}", timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def wait_until(predicate, seconds=5.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class LoginTests(DriveCase):
    def setUp(self):
        super().setUp()
        auth.set_client('id-1', 'secret-1')
        self.account = auth.GoogleAccount()
        self.port = free_port()

    def tearDown(self):
        self.account.cancel_login()
        super().tearDown()

    def test_start_login_returns_consent_url_with_pkce_and_state(self):
        st = self.account.start_login(port=self.port)
        q = parse_qs(urlparse(st['url']).query)
        self.assertTrue(st['url'].startswith(auth.AUTH_URL))
        self.assertEqual(q['client_id'], ['id-1'])
        self.assertEqual(q['redirect_uri'], [auth.REDIRECT_URI])
        self.assertEqual(q['access_type'], ['offline'])
        self.assertEqual(q['prompt'], ['consent'])
        self.assertEqual(q['code_challenge_method'], ['S256'])
        self.assertEqual(q['scope'], [auth.SCOPES])
        self.assertEqual(len(q['state'][0]), 32)
        self.assertTrue(st['pending'])

    def test_callback_exchanges_code_and_saves_email(self):
        google = self.serve({
            '/token': as_json({'access_token': 'A', 'refresh_token': 'R', 'expires_in': 3600, 'scope': auth.SCOPES}),
            '/userinfo': as_json({'email': 'studio@perspective-media.me'}),
        })
        st = self.account.start_login(port=self.port)
        state = parse_qs(urlparse(st['url']).query)['state'][0]
        status, page = callback(self.port, f"code=C1&state={state}")
        self.assertEqual(status, 200)
        self.assertIn('Connected studio@perspective-media.me', page)
        self.assertTrue(wait_until(lambda: self.account.login_state()['connected']))
        sent = form(google.of('POST', '/token')[0])
        self.assertEqual(sent['code'], 'C1')
        self.assertEqual(sent['client_secret'], 'secret-1')
        self.assertEqual(sent['redirect_uri'], auth.REDIRECT_URI)
        self.assertEqual(sent['grant_type'], 'authorization_code')
        self.assertTrue(sent['code_verifier'])
        self.assertEqual(google.of('GET', '/userinfo')[0]['headers']['authorization'], 'Bearer A')
        rec = credentials.load(auth.ACCOUNT_RECORD)
        self.assertEqual((rec['access'], rec['refresh'], rec['email']), ('A', 'R', 'studio@perspective-media.me'))
        self.assertEqual(self.account.login_state()['email'], 'studio@perspective-media.me')

    def test_wrong_state_is_refused_and_login_keeps_waiting(self):
        google = self.serve({})
        self.account.start_login(port=self.port)
        status, page = callback(self.port, 'code=C1&state=' + 'f' * 32)
        self.assertEqual(status, 400)
        self.assertIn('stale', page)
        self.assertTrue(self.account.login_state()['pending'])
        self.assertEqual(credentials.load(auth.ACCOUNT_RECORD), {})
        self.assertEqual(google.requests, [])

    def test_missing_refresh_token_is_an_error(self):
        self.serve({'/token': as_json({'access_token': 'A', 'expires_in': 3600}),
                    '/userinfo': as_json({'email': 'x@perspective-media.me'})})
        st = self.account.start_login(port=self.port)
        state = parse_qs(urlparse(st['url']).query)['state'][0]
        callback(self.port, f"code=C1&state={state}")
        self.assertTrue(wait_until(lambda: not self.account.login_state()['pending']))
        self.assertEqual(self.account.login_state()['error'],
                         'Google did not hand out a refresh token. Press Connect again.')
        self.assertEqual(credentials.load(auth.ACCOUNT_RECORD), {})

    def test_google_error_in_callback_is_shown(self):
        st = self.account.start_login(port=self.port)
        state = parse_qs(urlparse(st['url']).query)['state'][0]
        callback(self.port, f"error=access_denied&state={state}")
        self.assertTrue(wait_until(lambda: not self.account.login_state()['pending']))
        self.assertIn('access_denied', self.account.login_state()['error'])

    def test_port_in_use_names_the_port(self):
        with socket.socket() as taken:
            taken.bind(('127.0.0.1', self.port))
            taken.listen(1)
            with self.assertRaisesRegex(RuntimeError, f"{self.port}.*in use"):
                self.account.start_login(port=self.port)
        self.assertFalse(self.account.login_state()['pending'])

    def test_cancel_frees_the_port(self):
        self.account.start_login(port=self.port)
        self.account.cancel_login()
        self.assertFalse(self.account.login_state()['pending'])
        with socket.socket() as s:
            s.bind(('127.0.0.1', self.port))   # would raise if the listener still held it

    def test_start_login_without_client_raises(self):
        credentials.delete(auth.CLIENT_RECORD)
        with self.assertRaisesRegex(RuntimeError, 'client id'):
            self.account.start_login(port=self.port)


# ── Raw HTTP helper ──────────────────────────────────────────────────────────

class RawHttpTests(DriveCase):
    def test_raw_returns_status_headers_bytes(self):
        google = self.serve({'/blob': (200, b'abc', 'application/octet-stream', {'Location': 'http://x/session'})})
        status, headers, body = http.raw('PUT', f"{google.url}/blob", {'X-Test': '1'}, b'payload')
        self.assertEqual((status, body), (200, b'abc'))
        self.assertEqual(headers['location'], 'http://x/session')
        self.assertEqual(google.of('PUT', '/blob')[0]['body'], b'payload')

    def test_raw_raises_http_error_on_failure(self):
        google = self.serve({'/blob': as_json({'error': 'nope'}, status=403)})
        with self.assertRaises(http.HttpError) as caught:
            http.raw('GET', f"{google.url}/blob")
        self.assertEqual(caught.exception.status, 403)


if __name__ == '__main__':
    unittest.main()
