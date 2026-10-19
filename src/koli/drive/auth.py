# drive/auth.py
# The Google account Koli keeps files in: the pasted OAuth client (id and
# secret), the sign-in through the browser caught on a fixed loopback port, and
# the token refresh. Secrets live in the encrypted credential files, never in
# the settings file. The refresh rules mirror the AI subscriptions (llm/oauth.py).

import logging
import threading
import time

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

    # ── Browser login (Task 2) ──────────────────────────────────────────────

    def cancel_login(self):
        self._login = None


ACCOUNT = GoogleAccount()
