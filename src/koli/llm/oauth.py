# llm/oauth.py
# Logins for the two subscriptions: ChatGPT (Codex) and SuperGrok. Both sign in
# with a device code (you type a short code on the provider's page) and keep a
# refresh token. Codex refresh tokens are single-use: spending one twice kills
# the whole login. So refreshing is careful: one at a time per provider (a
# thread lock here, a lock file across processes), and a caller that waited
# re-reads the saved token instead of refreshing again.
# Ported from BaileysWhatsAPP providers/{codex,grok}/{auth,login}.ts.

import base64
import json
import logging
import threading
import time

from . import client_version, credentials, http
from .types import ReconnectNeeded

logger = logging.getLogger(__name__)

REFRESH_SKEW_SECONDS = 5 * 60          # refresh when the token expires within this
STALE_REFRESH_SECONDS = 6 * 24 * 3600  # or when it was last refreshed this long ago
LOGIN_GIVE_UP_SECONDS = 15 * 60


# The claims of a JWT, unverified: only used to show who is connected and to
# read the ChatGPT account id the backend wants in a header.
def jwt_claims(token: str) -> dict:
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


class Subscription:
    # Subclasses set these and implement _start_device(), _poll_device(),
    # _identity() and user_agent().
    name = ''
    label = ''
    token_url = ''
    client_id = ''

    def __init__(self):
        self._refresh_lock = threading.Lock()
        self._login = None             # the device login in progress, if any
        self._login_lock = threading.Lock()

    # ── Saved login ─────────────────────────────────────────────────────────

    def record(self) -> dict:
        return credentials.load(self.name)

    def status(self) -> dict:
        rec = self.record()
        return {
            'connected': bool(rec.get('access')) and not rec.get('dead'),
            'reconnect': bool(rec.get('dead')),
            'email': rec.get('email') or '',
            'last_refresh': rec.get('last_refresh') or rec.get('obtained_at'),
            'expires': rec.get('expires'),
            'error': rec.get('last_error') or '',
        }

    def disconnect(self):
        self.cancel_login()
        credentials.delete(self.name)

    @staticmethod
    def _needs_refresh(rec: dict, now: float = None) -> bool:
        now = now or time.time()
        last = float(rec.get('last_refresh') or rec.get('obtained_at') or 0)
        return float(rec.get('expires') or 0) - now < REFRESH_SKEW_SECONDS or now - last > STALE_REFRESH_SECONDS

    # (access token, account id) for a call, refreshed first if it is due.
    # `stale` is a bearer the server just refused (401): refresh it once.
    def bearer(self, stale: str = None) -> tuple:
        rec = self.record()
        if not rec.get('access'):
            raise ReconnectNeeded(f"{self.label} isn't connected. Connect it in Settings › AI.")
        if rec.get('dead'):
            raise ReconnectNeeded(f"{self.label} needs to be connected again (Settings › AI).")
        if stale or self._needs_refresh(rec):
            rec = self.refresh(stale)
        return rec['access'], rec.get('account_id')

    def refresh(self, stale: str = None) -> dict:
        with self._refresh_lock, credentials.file_lock(self.name):
            rec = self.record()
            if not rec.get('refresh') or rec.get('dead'):
                raise ReconnectNeeded(f"{self.label} needs to be connected again (Settings › AI).")
            # Someone refreshed while we waited for the locks: use theirs.
            if stale and rec.get('access') != stale and not self._needs_refresh(rec):
                return rec
            if not stale and not self._needs_refresh(rec):
                return rec
            try:
                tokens = http.post_form(self.token_url, {
                    'grant_type': 'refresh_token', 'refresh_token': rec['refresh'], 'client_id': self.client_id,
                }, {'User-Agent': self.user_agent()})
            except http.HttpError as e:
                body = e.body.lower()
                dead = (e.status == 400 and 'invalid_grant' in body) or \
                       (e.status == 401 and any(s in body for s in ('already been used', 'reused', 'invalid_grant')))
                rec['last_error'] = f"Refresh failed (HTTP {e.status})"
                if dead:
                    rec['dead'] = True
                    credentials.save(self.name, rec)
                    logger.warning(f"{self.label}: the saved login can't be refreshed any more; connect again")
                    raise ReconnectNeeded(f"{self.label} needs to be connected again (Settings › AI).") from None
                credentials.save(self.name, rec)
                raise
            if not tokens.get('access_token'):
                raise ReconnectNeeded(f"{self.label} refresh returned no token; connect again.")
            rec = self._store(tokens, previous=rec)
            logger.info(f"{self.label}: login refreshed")
            return rec

    # Save a token answer. A refresh answer without a new refresh token (Grok
    # often) keeps the old one.
    def _store(self, tokens: dict, previous: dict = None) -> dict:
        previous = previous or {}
        now = time.time()
        rec = {
            'access': tokens['access_token'],
            'refresh': tokens.get('refresh_token') or previous.get('refresh'),
            'expires': now + float(tokens.get('expires_in') or 3600),
            'obtained_at': previous.get('obtained_at') or now,
            'last_refresh': now,
            **self._identity(tokens, previous),
        }
        credentials.save(self.name, rec)
        return rec

    # At start and once a day: refresh a login that hasn't been for 6 days, so
    # a rarely used subscription doesn't lapse.
    def keep_alive(self):
        rec = self.record()
        if rec.get('access') and not rec.get('dead') and self._needs_refresh(rec):
            try:
                self.refresh()
            except Exception as e:
                logger.warning(f"{self.label}: keep-alive refresh failed: {e}")

    # ── Device login ────────────────────────────────────────────────────────

    def start_login(self) -> dict:
        with self._login_lock:
            if self._login and not self._login.get('done'):
                return self.login_state()
            start = self._start_device()
            self._login = {**start, 'started': time.time(), 'done': False, 'error': '', 'cancel': threading.Event()}
            login = self._login
        threading.Thread(target=self._poll_loop, args=(login,), daemon=True, name=f'{self.name}-login').start()
        return self.login_state()

    def login_state(self) -> dict:
        login = self._login
        if not login:
            return {'pending': False}
        return {'pending': not login['done'], 'user_code': login['user_code'], 'url': login['url'],
                'error': login['error'], 'connected': login['done'] and not login['error']}

    def cancel_login(self):
        login = self._login
        if login:
            login['cancel'].set()
        self._login = None

    def _poll_loop(self, login: dict):
        interval = login['interval']
        deadline = login['started'] + min(LOGIN_GIVE_UP_SECONDS, login.get('expires_in') or LOGIN_GIVE_UP_SECONDS)
        try:
            while not login['cancel'].wait(interval):
                if time.time() > deadline:
                    raise RuntimeError('The code expired. Press Connect to get a new one.')
                outcome = self._poll_device(login)
                if outcome == 'slow_down':
                    interval += 5
                elif isinstance(outcome, dict):
                    if not outcome.get('access_token') or not outcome.get('refresh_token'):
                        raise RuntimeError('The login returned no refresh token. Press Connect again.')
                    self._store(outcome)
                    logger.info(f"{self.label}: connected")
                    break
        except Exception as e:
            login['error'] = str(e)
            logger.warning(f"{self.label} login failed: {e}")
        login['done'] = True


# ── ChatGPT through Codex ────────────────────────────────────────────────────

class Codex(Subscription):
    name = 'codex'
    label = 'ChatGPT (Codex)'
    issuer = 'https://auth.openai.com'
    token_url = 'https://auth.openai.com/oauth/token'
    client_id = 'app_EMoamEEZ73f0CkXaXp7hrann'
    device_page = 'https://auth.openai.com/codex/device'

    def user_agent(self) -> str:
        return f"codex_cli_rs/{client_version.current('codex')}"

    def _start_device(self) -> dict:
        data = http.post_json(f"{self.issuer}/api/accounts/deviceauth/usercode", {'client_id': self.client_id},
                              {'User-Agent': self.user_agent()}, timeout=20)
        return {'device_auth_id': data['device_auth_id'], 'user_code': data['user_code'],
                'url': self.device_page, 'interval': max(float(data.get('interval') or 5), 1) + 3}

    # 403/404 = not approved yet. 200 = an authorization code and verifier,
    # traded for tokens.
    def _poll_device(self, login: dict):
        try:
            data = http.post_json(f"{self.issuer}/api/accounts/deviceauth/token",
                                  {'device_auth_id': login['device_auth_id'], 'user_code': login['user_code']},
                                  {'User-Agent': self.user_agent()}, timeout=20)
        except http.HttpError as e:
            if e.status in (403, 404):
                return None
            raise RuntimeError(f"ChatGPT login failed (HTTP {e.status})") from None
        except http.NetworkError:
            return None
        return http.post_form(self.token_url, {
            'grant_type': 'authorization_code', 'code': data['authorization_code'],
            'redirect_uri': f"{self.issuer}/deviceauth/callback", 'client_id': self.client_id,
            'code_verifier': data['code_verifier'],
        })

    def _identity(self, tokens: dict, previous: dict) -> dict:
        id_claims = jwt_claims(tokens.get('id_token') or '')
        access_claims = jwt_claims(tokens.get('access_token') or '')
        account = None
        for claims in (id_claims, access_claims):
            auth = claims.get('https://api.openai.com/auth') or {}
            account = account or auth.get('chatgpt_account_id') or claims.get('chatgpt_account_id')
        email = id_claims.get('email') or (access_claims.get('https://api.openai.com/profile') or {}).get('email')
        return {'account_id': account or previous.get('account_id'), 'email': email or previous.get('email')}


# ── SuperGrok through the Grok CLI proxy ─────────────────────────────────────

class Grok(Subscription):
    name = 'grok'
    label = 'SuperGrok'
    token_url = 'https://auth.x.ai/oauth2/token'
    device_url = 'https://auth.x.ai/oauth2/device/code'
    client_id = 'b1a00492-073a-47ea-816f-4c329264a828'
    scopes = 'openid profile email offline_access grok-cli:access api:access conversations:read conversations:write'

    def user_agent(self) -> str:
        return f"grok/{client_version.current('grok')}"

    def _start_device(self) -> dict:
        data = http.post_form(self.device_url, {'client_id': self.client_id, 'scope': self.scopes},
                              {'User-Agent': self.user_agent()})
        if not data.get('device_code') or not data.get('user_code'):
            raise RuntimeError("xAI didn't return a login code")
        return {'device_code': data['device_code'], 'user_code': data['user_code'],
                'url': data.get('verification_uri_complete') or data.get('verification_uri'),
                'interval': max(float(data.get('interval') or 5), 1),
                'expires_in': float(data.get('expires_in') or 300)}

    # "Not yet" usually comes as HTTP 400, but the error is read from a 200
    # body too (BaileysWhatsAPP does the same), so a pending answer is never
    # taken for a finished login.
    def _poll_device(self, login: dict):
        try:
            body = http.post_form(self.token_url, {
                'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                'client_id': self.client_id, 'device_code': login['device_code'],
            }, {'User-Agent': self.user_agent()})
            status = 200
        except http.NetworkError:
            return None
        except http.HttpError as e:
            try:
                body = json.loads(e.body)
            except ValueError:
                body = {}
            status = e.status
        if status == 200 and body.get('access_token'):
            return body
        error = body.get('error', '') if isinstance(body, dict) else ''
        if error == 'authorization_pending':
            return None
        if error == 'slow_down':
            return 'slow_down'
        if error in ('access_denied', 'authorization_denied'):
            raise RuntimeError('The login was declined.')
        if error == 'expired_token':
            raise RuntimeError('The code expired. Press Connect to get a new one.')
        raise RuntimeError(f"Grok login failed (HTTP {status} {error})".strip())

    def _identity(self, tokens: dict, previous: dict) -> dict:
        id_claims = jwt_claims(tokens.get('id_token') or '')
        access_claims = jwt_claims(tokens.get('access_token') or '')
        return {'account_id': access_claims.get('sub') or previous.get('account_id'),
                'email': id_claims.get('email') or access_claims.get('email') or previous.get('email')}


CODEX = Codex()
GROK = Grok()
SUBSCRIPTIONS = {'codex': CODEX, 'grok': GROK}
