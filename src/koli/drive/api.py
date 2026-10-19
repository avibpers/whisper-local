# drive/api.py
# The Drive v3 calls on the signed-in account: upload (resumable), download,
# metadata, listing, folders, share links. Plain REST like the CRM's PHP
# client; every call sends supportsAllDrives so shared drives work too.

import json
import mimetypes
import urllib.parse
from pathlib import Path

from ..llm import http
from .auth import ACCOUNT

API_BASE = 'https://www.googleapis.com/drive/v3'
UPLOAD_BASE = 'https://www.googleapis.com/upload/drive/v3'
FIELDS = 'id,name,mimeType,size,modifiedTime,webViewLink,parents,shared'
FOLDER_MIME = 'application/vnd.google-apps.folder'
# Google's own document types can't be downloaded as they are; they're
# exported as plain text.
EXPORT_AS_TEXT = ('application/vnd.google-apps.document', 'application/vnd.google-apps.spreadsheet',
                  'application/vnd.google-apps.presentation')


# ── One call, with a retry after a dead token ────────────────────────────────

# Adds the bearer and supportsAllDrives. A 401 means the access token died
# early: refresh it once and try again, never more.
def _call(method: str, path: str, params: dict = None, json_body: dict = None, body: bytes = None,
          headers: dict = None, base: str = None, timeout: float = 60) -> tuple:
    params = {'supportsAllDrives': 'true', **(params or {})}
    url = f"{base or API_BASE}{path}?{urllib.parse.urlencode(params)}"
    payload = body
    hdrs = dict(headers or {})
    if json_body is not None:
        payload = json.dumps(json_body).encode('utf-8')
        hdrs['Content-Type'] = 'application/json; charset=UTF-8'
    stale = None
    for attempt in (1, 2):
        token = ACCOUNT.bearer(stale)
        try:
            return http.raw(method, url, {'Authorization': f"Bearer {token}", **hdrs}, payload, timeout)
        except http.HttpError as e:
            if e.status != 401 or attempt == 2:
                raise
            stale = token


def _json(method: str, path: str, **kwargs) -> dict:
    _, _, data = _call(method, path, **kwargs)
    return json.loads(data.decode('utf-8')) if data else {}


# Drive's query language quotes with ', escaped by backslash.
def escape_query(value: str) -> str:
    return value.replace('\\', '\\\\').replace("'", "\\'")


# ── Files ────────────────────────────────────────────────────────────────────

def info(file_id: str) -> dict:
    return _json('GET', f"/files/{urllib.parse.quote(file_id, safe='')}", params={'fields': FIELDS})


# Non-trashed children of a folder, every page. `query` narrows further
# (Drive's `q` syntax, already escaped).
def list_files(folder_id: str = 'root', query: str = '') -> list:
    q = f"'{escape_query(folder_id)}' in parents and trashed=false"
    if query:
        q += f" and {query}"
    files, token = [], None
    while True:
        params = {'q': q, 'pageSize': 200, 'fields': f"nextPageToken,files({FIELDS})",
                  'includeItemsFromAllDrives': 'true', 'corpora': 'allDrives'}
        if token:
            params['pageToken'] = token
        page = _json('GET', '/files', params=params)
        files.extend(page.get('files') or [])
        token = page.get('nextPageToken')
        if not token:
            return files


# Resumable upload: open a session with the metadata, then send the bytes
# in one PUT. Files here are megabytes, so one PUT is enough. `file_id`
# replaces that file's content instead of creating a new one.
def upload_bytes(data: bytes, name: str, mime: str, folder_id: str = None, file_id: str = None) -> dict:
    meta = {'name': name}
    if folder_id and not file_id:
        meta['parents'] = [folder_id]
    method, path = ('PATCH', f"/files/{urllib.parse.quote(file_id, safe='')}") if file_id else ('POST', '/files')
    _, headers, _ = _call(method, path, params={'uploadType': 'resumable', 'fields': FIELDS}, json_body=meta,
                          headers={'X-Upload-Content-Type': mime, 'X-Upload-Content-Length': str(len(data))},
                          base=UPLOAD_BASE)
    session = headers.get('location')
    if not session:
        raise RuntimeError('Google Drive did not open an upload session')
    token = ACCOUNT.bearer()
    _, _, body = http.raw('PUT', session, {'Authorization': f"Bearer {token}", 'Content-Type': mime}, data, 300)
    return json.loads(body.decode('utf-8')) if body else {}


def upload(path, name: str = None, mime: str = None, folder_id: str = None, file_id: str = None) -> dict:
    path = Path(path)
    mime = mime or mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    return upload_bytes(path.read_bytes(), name or path.name, mime, folder_id, file_id)


def download(file_id: str) -> bytes:
    quoted = urllib.parse.quote(file_id, safe='')
    if info(file_id).get('mimeType') in EXPORT_AS_TEXT:
        _, _, data = _call('GET', f"/files/{quoted}/export", params={'mimeType': 'text/plain'})
        return data
    _, _, data = _call('GET', f"/files/{quoted}", params={'alt': 'media'})
    return data


def download_to(file_id: str, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(download(file_id))
    return path


# To the bin, not gone for good.
def delete(file_id: str):
    _json('PATCH', f"/files/{urllib.parse.quote(file_id, safe='')}", json_body={'trashed': True})


# ── Folders ──────────────────────────────────────────────────────────────────

def create_folder(name: str, parent_id: str = 'root') -> dict:
    return _json('POST', '/files', params={'fields': 'id,webViewLink'},
                 json_body={'name': name, 'mimeType': FOLDER_MIME, 'parents': [parent_id]})


# The id of "Koli/Meetings" under My Drive, creating what's missing. The
# first existing folder with that name wins when there are several.
def ensure_folder(path: str, parent_id: str = 'root') -> str:
    current = parent_id
    for segment in [s for s in path.replace('\\', '/').split('/') if s.strip()]:
        found = list_files(current, f"mimeType='{FOLDER_MIME}' and name='{escape_query(segment)}'")
        current = found[0]['id'] if found else create_folder(segment, current)['id']
    return current


# ── Sharing ──────────────────────────────────────────────────────────────────

# A link anyone can open: the "anyone / reader" permission, then the file's
# own link. Adding it again when it exists is harmless.
def share_link(file_id: str) -> str:
    quoted = urllib.parse.quote(file_id, safe='')
    _json('POST', f"/files/{quoted}/permissions", json_body={'role': 'reader', 'type': 'anyone'})
    return str(info(file_id).get('webViewLink') or '')


def unshare(file_id: str):
    quoted = urllib.parse.quote(file_id, safe='')
    page = _json('GET', f"/files/{quoted}/permissions", params={'fields': 'permissions(id,type)'})
    for perm in page.get('permissions') or []:
        if perm.get('type') == 'anyone':
            _call('DELETE', f"/files/{quoted}/permissions/{urllib.parse.quote(perm['id'], safe='')}")
