# meeting/meet_link.py
# The local door the Koli Chrome extension knocks on: a tiny HTTP server on
# 127.0.0.1:52881 that takes batches of Google Meet events (joined, people,
# captions, left) and answers with what Koli is doing, for the extension's
# popup. Only the extension may post: a web page can't send its Origin as
# chrome-extension:// nor the custom header without a preflight we refuse.

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

logger = logging.getLogger(__name__)

HOST = '127.0.0.1'
PORT = 52881                  # next to the Google login catcher's 52880 (drive/auth.py)
MAX_BODY = 1024 * 1024        # a batch is a few KB of captions; anything bigger is not ours
HEADER = 'X-Koli-Extension'   # the extension sends this on every request
EXTENSION_ORIGINS = ('chrome-extension://',)


# The server and its thread. `on_events(events) -> dict` handles one batch
# and returns Koli's status; `status() -> dict` answers the popup's hello.
class MeetLink:
    def __init__(self, on_events: Callable[[list], dict], status: Callable[[], dict],
                 host: str = HOST, port: int = PORT):
        self._on_events = on_events
        self._status = status
        self.host, self.port = host, int(port)
        self._httpd = None
        self.error = ''

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> bool:
        if self._httpd is not None:
            return True
        try:
            httpd = ThreadingHTTPServer((self.host, self.port), _make_handler(self._on_events, self._status))
        except OSError as e:
            self.error = f"The Meet extension's port {self.port} is busy: {e}"
            logger.warning(self.error)
            return False
        httpd.daemon_threads = True
        self._httpd, self.error = httpd, ''
        threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.5}, daemon=True,
                         name='meet-link').start()
        logger.info(f"Meet extension link listening on http://{self.host}:{self.port}")
        return True

    def stop(self):
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
        logger.info("Meet extension link stopped")


def _make_handler(on_events: Callable[[list], dict], status: Callable[[], dict]):

    class Handler(BaseHTTPRequestHandler):

        def log_message(self, fmt, *args):
            logger.debug(f"[meet-link] {fmt % args}")

        # The extension's service worker, and nothing a web page can fake.
        def _from_extension(self) -> bool:
            if self.headers.get(HEADER) != '1':
                return False
            origin = self.headers.get('Origin') or ''
            return not origin or origin.startswith(EXTENSION_ORIGINS)

        # A page's preflight gets no CORS headers, so the browser never sends its request.
        def do_OPTIONS(self):
            self._json(403, {'ok': False})

        def do_GET(self):
            if self.path.split('?')[0] != '/meet/hello' or not self._from_extension():
                self._json(404, {'ok': False})
                return
            self._json(200, {'ok': True, 'app': 'koli', **self._safe(status)})

        def do_POST(self):
            if self.path.split('?')[0] != '/meet/events' or not self._from_extension():
                self._json(404, {'ok': False})
                return
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                self._json(413, {'ok': False})
                return
            try:
                # Explicitly UTF-8: Hebrew captions must survive whatever the Windows code page is.
                body = json.loads(self.rfile.read(length).decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                self._json(400, {'ok': False})
                return
            events = body.get('events') if isinstance(body, dict) else None
            if not isinstance(events, list):
                self._json(400, {'ok': False})
                return
            self._json(200, {'ok': True, **self._safe(lambda: on_events(events))})

        @staticmethod
        def _safe(fn) -> dict:
            try:
                return fn() or {}
            except Exception as e:
                logger.warning(f"Meet extension event failed: {e}")
                return {'error': str(e)}

        def _json(self, code: int, payload: dict):
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler
