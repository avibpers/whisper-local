# drive/docs.py
# The Google Docs API on the Drive account: read a document with its tabs and
# send a batch of edits. The Drive scope covers these calls, so no new login.
# Kept apart from drive/api.py because the Docs API refuses Drive's extra
# query parameters (supportsAllDrives).

import json
import urllib.parse

from ..llm import http
from .auth import ACCOUNT

DOCS_BASE = 'https://docs.googleapis.com/v1/documents'


# One call with the bearer; a 401 refreshes the token once and tries again.
def _call(method: str, path: str, params: dict = None, body: dict = None, timeout: float = 120) -> dict:
    url = f"{DOCS_BASE}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else '')
    payload = json.dumps(body).encode('utf-8') if body is not None else None
    stale = None
    for attempt in (1, 2):
        token = ACCOUNT.bearer(stale)
        headers = {'Authorization': f"Bearer {token}", 'Content-Type': 'application/json; charset=UTF-8'}
        try:
            _, _, data = http.raw(method, url, headers, payload, timeout)
            return json.loads(data.decode('utf-8')) if data else {}
        except http.HttpError as e:
            if e.status != 401 or attempt == 2:
                raise
            stale = token


# The document with every tab's content.
def get(doc_id: str) -> dict:
    return _call('GET', f"/{urllib.parse.quote(doc_id, safe='')}", params={'includeTabsContent': 'true'})


# Edits applied in order, all or nothing; the replies line up with the requests.
def batch_update(doc_id: str, requests: list) -> dict:
    return _call('POST', f"/{urllib.parse.quote(doc_id, safe='')}:batchUpdate", body={'requests': requests})
