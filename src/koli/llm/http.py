# llm/http.py
# The few HTTP calls every provider needs, on the standard library only (no
# SDKs, so the app build stays small). JSON in and out, plus a streaming POST
# that hands back the open response for server-sent events. Any status other
# than 2xx raises HttpError with the body, so callers can tell a dead login
# (401) from an outdated client (426) from a provider that is simply down.

import json
import socket
import urllib.error
import urllib.parse
import urllib.request


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str = ''):
        self.status = status
        self.body = body or ''
        self.url = url
        super().__init__(f"HTTP {status}: {self.body[:300]}")


# Network failures and timeouts, as opposed to an answer the server gave.
class NetworkError(Exception):
    pass


def _request(method: str, url: str, headers: dict = None, body: bytes = None, timeout: float = 30):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        return urllib.request.urlopen(req, timeout=max(0.5, float(timeout)))
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode('utf-8', errors='replace')
        except Exception:
            text = ''
        raise HttpError(e.code, text, url) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        reason = getattr(e, 'reason', None) or e
        raise NetworkError(str(reason) or e.__class__.__name__) from None


def _decode(resp) -> object:
    raw = resp.read()
    if not raw:
        return {}
    try:
        return json.loads(raw.decode('utf-8', errors='replace'))
    except ValueError:
        raise HttpError(resp.status, raw[:300].decode('utf-8', errors='replace'), resp.url) from None


def get_json(url: str, headers: dict = None, timeout: float = 15) -> object:
    with _request('GET', url, {'Accept': 'application/json', **(headers or {})}, None, timeout) as resp:
        return _decode(resp)


def post_json(url: str, payload: dict, headers: dict = None, timeout: float = 30) -> object:
    body = json.dumps(payload).encode('utf-8')
    hdrs = {'Content-Type': 'application/json', 'Accept': 'application/json', **(headers or {})}
    with _request('POST', url, hdrs, body, timeout) as resp:
        return _decode(resp)


# OAuth token endpoints take form fields, not JSON.
def post_form(url: str, fields: dict, headers: dict = None, timeout: float = 20) -> object:
    body = urllib.parse.urlencode(fields).encode('utf-8')
    hdrs = {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json', **(headers or {})}
    with _request('POST', url, hdrs, body, timeout) as resp:
        return _decode(resp)


# A POST whose answer is read as it arrives. The caller must close it
# (use it in a `with`). The timeout applies to each read, not the whole answer.
def post_stream(url: str, payload: dict, headers: dict = None, timeout: float = 60):
    body = json.dumps(payload).encode('utf-8')
    hdrs = {'Content-Type': 'application/json', 'Accept': 'text/event-stream', **(headers or {})}
    return _request('POST', url, hdrs, body, timeout)
