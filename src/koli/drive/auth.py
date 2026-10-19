# drive/auth.py
# The Google account Koli keeps files in: the pasted OAuth client (id and
# secret), the sign-in through the browser caught on a fixed loopback port, and
# the token refresh. Secrets live in the encrypted credential files, never in
# the settings file. The refresh rules mirror the AI subscriptions (llm/oauth.py).

import base64
import hashlib
import html
import http.server as http_server
import logging
import secrets
import threading
import time
import urllib.parse

from ..llm import credentials, http

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

PORT = 52880                                    # registered on the Google client; never changes
REDIRECT_URI = f"http://127.0.0.1:{PORT}/callback"
SCOPES = 'https://www.googleapis.com/auth/drive openid email'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
USERINFO_URL = 'https://openidconnect.googleapis.com/v1/userinfo'

CLIENT_RECORD = 'google_client'                 # {client_id, client_secret}
ACCOUNT_RECORD = 'google_drive'                 # the signed-in account's tokens

REFRESH_SKEW_SECONDS = 5 * 60                   # refresh when the token expires within this
STALE_REFRESH_SECONDS = 6 * 24 * 3600           # or when it was last refreshed this long ago
LOGIN_GIVE_UP_SECONDS = 15 * 60
LISTENER_TIMEOUT = 5                            # a browser preconnect that sends nothing is dropped after this
DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive'

SETTINGS_HINT = 'Settings › Google Drive'
DEAD_ERRORS = ('invalid_grant', 'invalid_client', 'unauthorized_client')


class DriveNotConnected(RuntimeError):
    pass


# ── The pasted OAuth client ──────────────────────────────────────────────────

def get_client() -> dict:
    rec = credentials.load(CLIENT_RECORD)
    return {'client_id': str(rec.get('client_id') or ''), 'client_secret': str(rec.get('client_secret') or '')}


# A secret shown masked in the UI ("GOCSPX-••••1234") means "keep the old one".
# A different client id drops the saved login: a refresh token only works
# with the client that issued it.
def set_client(client_id: str, client_secret: str):
    current = get_client()
    client_id = (client_id or '').strip()
    client_secret = (client_secret or '').strip()
    if not client_secret or '•' in client_secret:
        client_secret = current['client_secret']
    if client_id != current['client_id']:
        credentials.delete(ACCOUNT_RECORD)
    credentials.save(CLIENT_RECORD, {'client_id': client_id, 'client_secret': client_secret})


def client_hint() -> str:
    secret = get_client()['client_secret']
    if not secret:
        return ''
    prefix = 'GOCSPX-' if secret.startswith('GOCSPX-') else ''
    return f"{prefix}••••{secret[-4:]}"


# ── The signed-in account ────────────────────────────────────────────────────

class GoogleAccount:
    label = 'Google Drive'

    def __init__(self):
        self._refresh_lock = threading.Lock()
        self._login = None
        self._login_lock = threading.Lock()

    def record(self) -> dict:
        return credentials.load(ACCOUNT_RECORD)

    def status(self) -> dict:
        rec = self.record()
        return {
            'configured': bool(get_client()['client_id']),
            'connected': bool(rec.get('access')) and not rec.get('dead'),
            'reconnect': bool(rec.get('dead')),
            'email': rec.get('email') or '',
            'last_refresh': rec.get('last_refresh') or rec.get('obtained_at'),
            'expires': rec.get('expires'),
            'error': rec.get('last_error') or '',
        }

    # Local only: the client is shared with the CRM, so the grant at Google
    # is never revoked from here.
    def disconnect(self):
        self.cancel_login()
        credentials.delete(ACCOUNT_RECORD)

    @staticmethod
    def _needs_refresh(rec: dict, now: float = None) -> bool:
        now = now or time.time()
        last = float(rec.get('last_refresh') or rec.get('obtained_at') or 0)
        return float(rec.get('expires') or 0) - now < REFRESH_SKEW_SECONDS or now - last > STALE_REFRESH_SECONDS

    # A fresh access token, refreshed first if it is due. `stale` is a bearer
    # Google just refused (401): refresh it once.
    def bearer(self, stale: str = None) -> str:
        rec = self.record()
        if not rec.get('access'):
            raise DriveNotConnected(f"Google Drive isn't connected. Connect it in {SETTINGS_HINT}.")
        if rec.get('dead'):
            raise DriveNotConnected(f"Google Drive needs to be connected again ({SETTINGS_HINT}).")
        if stale or self._needs_refresh(rec):
            rec = self.refresh(stale)
        return rec['access']

    def refresh(self, stale: str = None) -> dict:
        with self._refresh_lock, credentials.file_lock(ACCOUNT_RECORD):
            rec = self.record()
            if not rec.get('refresh') or rec.get('dead'):
                raise DriveNotConnected(f"Google Drive needs to be connected again ({SETTINGS_HINT}).")
            # Someone refreshed while we waited for the locks: use theirs.
            if stale and rec.get('access') != stale and not self._needs_refresh(rec):
                return rec
            if not stale and not self._needs_refresh(rec):
                return rec
            client = get_client()
            try:
                tokens = http.post_form(TOKEN_URL, {
                    'grant_type': 'refresh_token', 'refresh_token': rec['refresh'],
                    'client_id': client['client_id'], 'client_secret': client['client_secret'],
                })
            except http.HttpError as e:
                body = e.body.lower()
                rec['last_error'] = f"Refresh failed (HTTP {e.status})"
                if any(err in body for err in DEAD_ERRORS):
                    rec['dead'] = True
                    credentials.save(ACCOUNT_RECORD, rec)
                    logger.warning("Google Drive: the saved login can't be refreshed any more; connect again")
                    raise DriveNotConnected(f"Google Drive needs to be connected again ({SETTINGS_HINT}).") from None
                credentials.save(ACCOUNT_RECORD, rec)
                raise
            if not tokens.get('access_token'):
                raise DriveNotConnected('Google Drive refresh returned no token; connect again.')
            rec = self._store(tokens, previous=rec)
            logger.info('Google Drive: login refreshed')
            return rec

    # Save a token answer. Google's refresh answers carry no new refresh
    # token, so the old one stays.
    def _store(self, tokens: dict, previous: dict = None) -> dict:
        previous = previous or {}
        now = time.time()
        rec = {
            'access': tokens['access_token'],
            'refresh': tokens.get('refresh_token') or previous.get('refresh'),
            'expires': now + float(tokens.get('expires_in') or 3600),
            'obtained_at': previous.get('obtained_at') or now,
            'last_refresh': now,
            'email': tokens.get('email') or previous.get('email') or '',
            'scope': tokens.get('scope') or previous.get('scope') or '',
        }
        credentials.save(ACCOUNT_RECORD, rec)
        return rec

    # At start and once a day: refresh a login that hasn't been for 6 days.
    def keep_alive(self):
        rec = self.record()
        if rec.get('access') and not rec.get('dead') and self._needs_refresh(rec):
            try:
                self.refresh()
            except Exception as e:
                logger.warning(f"Google Drive: keep-alive refresh failed: {e}")

    # ── Browser login ───────────────────────────────────────────────────────
    # Google doesn't allow the Drive scope on a device-code login, so the
    # sign-in is the web flow: the browser opens the consent page and Google
    # sends the code back to a one-page server on 127.0.0.1:PORT. The page
    # polls login_state() like the AI logins; there's no code to type.

    # `port` is for tests; the consent URL always carries the registered
    # REDIRECT_URI.
    def start_login(self, port: int = None) -> dict:
        client = get_client()
        if not client['client_id'] or not client['client_secret']:
            raise RuntimeError(f"Paste the Google client id and secret first ({SETTINGS_HINT}).")
        with self._login_lock:
            if self._login and not self._login['done']:
                return self.login_state()
            self._login = None
            port = PORT if port is None else port
            state = secrets.token_hex(16)
            verifier = secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            url = AUTH_URL + '?' + urllib.parse.urlencode({
                'client_id': client['client_id'], 'redirect_uri': REDIRECT_URI, 'response_type': 'code',
                'scope': SCOPES, 'access_type': 'offline', 'prompt': 'consent', 'state': state,
                'code_challenge': challenge, 'code_challenge_method': 'S256',
            })
            login = {'url': url, 'state': state, 'verifier': verifier, 'started': time.time(),
                     'done': False, 'error': '', 'email': '', 'listener': None}
            login['listener'] = _Listener(port, login, self._finish_login)
            self._login = login
        return self.login_state()

    def login_state(self) -> dict:
        login = self._login
        if not login:
            return {'pending': False, 'url': '', 'error': '', 'connected': False, 'email': ''}
        if not login['done'] and time.time() - login['started'] > LOGIN_GIVE_UP_SECONDS:
            self._end_login(login, 'The sign-in timed out. Press Connect to start again.')
        return {'pending': not login['done'], 'url': login['url'], 'error': login['error'],
                'connected': login['done'] and not login['error'], 'email': login['email']}

    def cancel_login(self):
        login = self._login
        if login:
            self._end_login(login, login['error'] if login['done'] else 'Cancelled.')
        self._login = None

    def _end_login(self, login: dict, error: str):
        login['error'] = error
        login['done'] = True
        login['listener'].close()

    # The callback arrived with a good state: trade the code for tokens and
    # read who signed in. Raises with the text the page should show.
    def _finish_login(self, login: dict, query: dict) -> str:
        if query.get('error'):
            raise RuntimeError(f"Google refused the sign-in ({query['error']}).")
        if not query.get('code'):
            raise RuntimeError('Google sent no code. Press Connect again.')
        client = get_client()
        tokens = http.post_form(TOKEN_URL, {
            'code': query['code'], 'client_id': client['client_id'], 'client_secret': client['client_secret'],
            'redirect_uri': REDIRECT_URI, 'grant_type': 'authorization_code', 'code_verifier': login['verifier'],
        })
        if not tokens.get('access_token') or not tokens.get('refresh_token'):
            raise RuntimeError('Google did not hand out a refresh token. Press Connect again.')
        # Google's consent screen lets you untick Drive and still sign in; a
        # login without it would look connected while every call fails.
        if DRIVE_SCOPE not in str(tokens.get('scope') or '').split():
            raise RuntimeError("Google Drive access wasn't granted. Press Connect and keep the Drive box ticked.")
        info = http.get_json(USERINFO_URL, {'Authorization': f"Bearer {tokens['access_token']}"})
        tokens['email'] = str(info.get('email') or '')
        if login['done']:   # cancelled (or timed out) while Google was answering
            raise RuntimeError('Cancelled.')
        self._store(tokens)
        login['email'] = tokens['email']
        logger.info(f"Google Drive: connected as {tokens['email']}")
        return tokens['email']


# One-request HTTP server on the loopback port, serving only /callback.
# A wrong or missing state (a stale tab) gets a 400 and the login keeps
# waiting; a good state ends the login, for better or worse.
class _Listener:
    def __init__(self, port: int, login: dict, finish):
        outer = self
        self.login = login
        self.finish = finish

        class Handler(http_server.BaseHTTPRequestHandler):
            timeout = LISTENER_TIMEOUT   # the server is single-threaded: an idle connection must not hold it

            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                stale = (parsed.path != '/callback' or query.get('state') != outer.login['state'] or outer.login['done']
                         or not (query.get('code') or query.get('error')))
                if stale:
                    outer._reply(self, 400, 'This sign-in link is stale. Go back to Koli and press Connect again.')
                    return
                try:
                    email = outer.finish(outer.login, query)
                    outer._reply(self, 200, f"Connected {email}. You can close this tab.")
                    outer.login['error'] = ''
                except Exception as e:
                    outer.login['error'] = str(e) or e.__class__.__name__
                    logger.warning(f"Google Drive sign-in failed: {outer.login['error']}")
                    outer._reply(self, 200, f"Koli couldn't finish the sign-in: {outer.login['error']}")
                outer.login['done'] = True
                outer.close()

        try:
            self.httpd = http_server.HTTPServer(('127.0.0.1', port), Handler)
        except OSError as e:
            raise RuntimeError(f"Port {port} is in use; close the other program or wait a minute, "
                               f"then press Connect again ({e.strerror or e})") from None
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.2},
                                       daemon=True, name='google-login')
        self.thread.start()

    @staticmethod
    def _reply(handler, status: int, text: str):
        body = (f"<!doctype html><meta charset='utf-8'><p style='font-family:sans-serif;padding:2em'>"
                f"{html.escape(text)}</p>").encode('utf-8')
        handler.send_response(status)
        handler.send_header('Content-Type', 'text/html; charset=utf-8')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    # Safe to call twice and from the handler's own thread (shutdown is
    # deferred to another thread there, or serve_forever would deadlock).
    def close(self):
        httpd = getattr(self, 'httpd', None)
        if httpd is None:
            return
        self.httpd = None
        if threading.current_thread() is self.thread:
            threading.Thread(target=lambda: (httpd.shutdown(), httpd.server_close()), daemon=True).start()
        else:
            httpd.shutdown()
            httpd.server_close()


ACCOUNT = GoogleAccount()
