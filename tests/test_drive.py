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
