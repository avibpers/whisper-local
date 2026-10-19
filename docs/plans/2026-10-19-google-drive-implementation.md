# Google Drive Connection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Koli signs in to one Google account through the browser and can upload, download, list and share-link Drive files from Python, with a Settings card to set it up.

**Architecture:** A new `src/koli/drive/` package: `auth.py` keeps the pasted OAuth client and the signed-in account (encrypted via `llm/credentials.py`), runs the loopback login on `127.0.0.1:52880` and refreshes tokens; `api.py` is the Drive v3 REST calls on `llm/http.py`. The bridge, Settings card, doctor line and daily keep-alive wire it in the same way the AI subscriptions are wired.

**Tech Stack:** Python stdlib (`urllib`, `http.server`, `secrets`, `hashlib`), Preact + htm settings page, `unittest` with the `FakeServer` from `tests/test_llm.py`.

**Spec:** `docs/design/google-drive.md`

## Global Constraints

- Redirect URI is exactly `http://127.0.0.1:52880/callback`; the port never changes.
- Scopes: `https://www.googleapis.com/auth/drive openid email`.
- Secrets live only in `%APPDATA%\koli\credentials\` (`google_client.bin`, `google_drive.bin`); nothing Google-related goes in `user_settings.yaml` or `config.defaults.yaml`.
- Every Drive call sends `supportsAllDrives=true`.
- Disconnect deletes the local record only; never call Google's revoke endpoint.
- No SDK, no new dependency. Every module starts with a 2–4 line header comment; non-trivial functions get a one-line intent comment (project rule).
- Tests never touch the real credentials folder (patch `koli.llm.credentials.folder`) and never reach the internet (patch the URL constants to a `FakeServer`).
- Work in `S:\Koli-google-drive` on `feat/google-drive`; run git as `git -C S:\Koli-google-drive …`; commit with explicit `git add <files>`.

## Review Focus

1. Port 52880 already taken (a stale login server, another app): `start_login()` must raise with the port's name, not hang. Test in Task 2.
2. A callback with the wrong `state` or no `code` (a stray browser tab, a refresh of the callback page): answered with an error page and the login keeps waiting; the record is never written. Test in Task 2.
3. Google answers 401 mid-call because the access token died early: one forced refresh, then the call is retried once, not looped. Test in Task 3.
4. Pasting a different client id while an account is connected: the saved login is cleared so a dead pairing never shows as "Connected". Test in Task 1.
5. A file name with quotes or non-ASCII (Hebrew meeting titles) in `ensure_folder`/`list_files` queries: the Drive `q` string escapes `'` and `\`, and names go through JSON, not the query, on create. Test in Task 3.

---

### Task 1: Client store, token refresh and `http.raw`

**Files:**
- Modify: `src/koli/llm/http.py` (add `raw`)
- Create: `src/koli/drive/__init__.py`, `src/koli/drive/auth.py`
- Test: `tests/test_drive.py`

**Interfaces:**
- Consumes: `koli.llm.credentials.load/save/delete/file_lock`, `koli.llm.http.post_form/HttpError/NetworkError`.
- Produces:
  - `http.raw(method: str, url: str, headers: dict = None, body: bytes = None, timeout: float = 60) -> tuple[int, dict, bytes]` (status, lower-cased response headers, body). Raises `HttpError` on non-2xx, `NetworkError` on failure.
  - In `drive/auth.py`: constants `PORT = 52880`, `REDIRECT_URI`, `SCOPES`, `AUTH_URL`, `TOKEN_URL`, `USERINFO_URL`; `CLIENT_RECORD = 'google_client'`, `ACCOUNT_RECORD = 'google_drive'`; `class DriveNotConnected(RuntimeError)`.
  - `get_client() -> dict` `{client_id, client_secret}` (empty strings when unset); `set_client(client_id: str, client_secret: str)` (a secret containing `•` keeps the old one; a changed client id deletes `ACCOUNT_RECORD`); `client_hint() -> str` masked secret `GOCSPX-••••` + last 4, or `''`.
  - `class GoogleAccount` with `record()`, `status() -> {configured, connected, reconnect, email, last_refresh, expires, error}`, `bearer(stale: str = None) -> str`, `refresh(stale=None) -> dict`, `keep_alive()`, `disconnect()`, `_store(tokens, previous=None) -> dict`; module singleton `ACCOUNT = GoogleAccount()`.
  - `drive/__init__.py` re-exports `ACCOUNT, DriveNotConnected, get_client, set_client, client_hint`.

- [ ] **Step 1: Write the failing tests** in `tests/test_drive.py` (reuse `FakeServer`, `as_json` and the credentials-folder patch from `tests/test_llm.py` by importing them)

```python
class ClientStoreTests(DriveCase):
    def test_set_client_keeps_secret_when_masked(self):
        auth.set_client('id-1', 'GOCSPX-abcd1234')
        auth.set_client('id-1', 'GOCSPX-••••1234')
        self.assertEqual(auth.get_client()['client_secret'], 'GOCSPX-abcd1234')
        self.assertEqual(auth.client_hint(), 'GOCSPX-••••1234')

    def test_changing_client_id_clears_saved_login(self):
        auth.set_client('id-1', 's'); credentials.save(auth.ACCOUNT_RECORD, {'access': 'A', 'refresh': 'R', 'expires': time.time() + 3600})
        auth.set_client('id-2', 's')
        self.assertEqual(credentials.load(auth.ACCOUNT_RECORD), {})

class RefreshTests(DriveCase):
    def test_refresh_sends_client_secret_and_keeps_refresh_token(self):  # /token → {'access_token': 'A2', 'expires_in': 3600}
        ... assert 'client_secret=s' in request body; record['refresh'] == 'R'; bearer() == 'A2'
    def test_invalid_grant_marks_dead_and_raises(self):      # /token → 400 {'error': 'invalid_grant'}
        ... assertRaises(auth.DriveNotConnected); status()['reconnect'] is True
    def test_invalid_client_marks_dead(self):                 # /token → 401 {'error': 'invalid_client'}
    def test_bearer_without_record_raises(self):
    def test_raw_returns_status_headers_bytes(self):          # FakeServer route returning b'abc' with a Location header
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_drive -v` (from `S:\Koli-google-drive`)
Expected: FAIL with `ModuleNotFoundError: koli.drive` / `AttributeError: raw`

- [ ] **Step 3: Implement `raw()` in `llm/http.py`**, `drive/__init__.py` and `drive/auth.py`

`GoogleAccount` copies `Subscription._needs_refresh`, `bearer`, `refresh`, `_store`, `keep_alive` from `llm/oauth.py` with two changes: the form adds `client_secret`, and "dead" is `invalid_grant`, `invalid_client` or `unauthorized_client` in the body. `status()['configured']` is whether a client id is set. `DriveNotConnected` messages name "Settings › Google Drive".

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_drive -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C S:\Koli-google-drive add src/koli/llm/http.py src/koli/drive/__init__.py src/koli/drive/auth.py tests/test_drive.py
git -C S:\Koli-google-drive commit -m "feat(drive): Google client store and token refresh"
```

---

### Task 2: Browser login caught on the loopback port

**Files:**
- Modify: `src/koli/drive/auth.py`
- Test: `tests/test_drive.py`

**Interfaces:**
- Produces on `GoogleAccount`: `start_login(port: int = None) -> dict` (`None` means `PORT`; tests pass a free port), `login_state() -> {pending, url, error, connected, email}`, `cancel_login()`. Module constant `LOGIN_GIVE_UP_SECONDS = 15 * 60`. The listener binds `('127.0.0.1', port)`; the consent URL always carries `REDIRECT_URI` (the registered one), so a test on another port reads the `state` from the URL and sends its own GET to `127.0.0.1:<port>/callback`.

- [ ] **Step 1: Write the failing tests**

```python
class LoginTests(DriveCase):
    def test_start_login_returns_consent_url_with_pkce_and_state(self):
        st = auth.ACCOUNT.start_login(port=free_port)
        q = parse_qs(urlparse(st['url']).query)
        self.assertEqual(q['redirect_uri'], [auth.REDIRECT_URI]); self.assertEqual(q['access_type'], ['offline'])
        self.assertEqual(q['prompt'], ['consent']); self.assertEqual(q['code_challenge_method'], ['S256'])
        self.assertEqual(q['scope'], [auth.SCOPES]); self.assertTrue(st['pending'])
    def test_callback_exchanges_code_and_saves_email(self):
        # /token → {'access_token': 'A', 'refresh_token': 'R', 'expires_in': 3600, 'scope': auth.SCOPES}; /userinfo → {'email': 'studio@perspective-media.me'}
        # GET http://127.0.0.1:<port>/callback?code=C&state=<state> → 200 page containing 'Connected studio@'
        # token request body has code=C, code_verifier=<v>, client_secret=s; login_state()['connected'] is True; record email saved
    def test_wrong_state_is_refused_and_login_keeps_waiting(self):   # 400 page; pending still True; no record
    def test_missing_refresh_token_is_an_error(self):                 # error 'Google did not hand out a refresh token. Press Connect again.'
    def test_port_in_use_names_the_port(self):                         # bind the port first; assertRaisesRegex(RuntimeError, '52880|in use')
    def test_cancel_frees_the_port(self):
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_drive.LoginTests -v`
Expected: FAIL with `AttributeError: start_login`

- [ ] **Step 3: Implement the login in `drive/auth.py`**

A `_Listener` (`http.server.HTTPServer` on a daemon thread, `handle_request()` in a loop until done or cancelled) answers `/callback`: wrong or missing `state` → 400 plain page "This sign-in link is stale. Go back to Koli and press Connect again."; good `state` + `code` → exchange with `http.post_form(TOKEN_URL, {code, client_id, client_secret, redirect_uri, grant_type: 'authorization_code', code_verifier})`, then `http.get_json(USERINFO_URL, {'Authorization': 'Bearer …'})`, `_store(tokens)` with `email`, 200 page "Connected <email>. You can close this tab."; the HTML echoes the email through `html.escape`. Port in use → `RuntimeError(f"Port {port} is in use; close the other program or wait a minute, then press Connect again")`. Deadline `LOGIN_GIVE_UP_SECONDS` → error "The sign-in timed out. Press Connect to start again." `state` is `secrets.token_hex(16)`, verifier `secrets.token_urlsafe(64)`, challenge `base64url(sha256(verifier))` without padding.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_drive -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C S:\Koli-google-drive add src/koli/drive/auth.py tests/test_drive.py
git -C S:\Koli-google-drive commit -m "feat(drive): sign in with Google through the browser on a loopback port"
```

---

### Task 3: Drive file calls

**Files:**
- Create: `src/koli/drive/api.py`
- Modify: `src/koli/drive/__init__.py`
- Test: `tests/test_drive.py`

**Interfaces:**
- Consumes: `ACCOUNT.bearer(stale)`, `http.raw`, `http.get_json`, `http.post_json`.
- Produces in `drive/api.py` (constants `API_BASE = 'https://www.googleapis.com/drive/v3'`, `UPLOAD_BASE = 'https://www.googleapis.com/upload/drive/v3'`, `FIELDS = 'id,name,mimeType,size,modifiedTime,webViewLink,parents,shared'`):
  - `_call(method: str, path: str, params: dict = None, json_body: dict = None, body: bytes = None, headers: dict = None, base: str = None) -> tuple[int, dict, bytes]`: adds the bearer and `supportsAllDrives=true`; on `HttpError` 401 refreshes once with `stale=` and retries once.
  - `upload(path, name: str = None, mime: str = None, folder_id: str = None, file_id: str = None) -> dict`
  - `upload_bytes(data: bytes, name: str, mime: str, folder_id: str = None, file_id: str = None) -> dict`
  - `download(file_id: str) -> bytes`; `download_to(file_id: str, path) -> Path`
  - `info(file_id: str) -> dict`; `list_files(folder_id: str = 'root', query: str = '') -> list[dict]`
  - `create_folder(name: str, parent_id: str = 'root') -> dict`; `ensure_folder(path: str, parent_id: str = 'root') -> str`
  - `share_link(file_id: str) -> str`; `unshare(file_id: str)`; `delete(file_id: str)`
  - `escape_query(value: str) -> str` (backslash-escapes `\` and `'`).
  - `drive/__init__.py` re-exports all of these.

- [ ] **Step 1: Write the failing tests**

```python
class ApiTests(DriveCase):      # setUp: connected record, api.API_BASE/UPLOAD_BASE → FakeServer, auth.TOKEN_URL → FakeServer
    def test_upload_bytes_opens_session_then_puts(self):
        # POST /upload/drive/v3/files?uploadType=resumable… → 200 with Location: <server>/session; PUT /session → {'id': 'F1', 'name': 'a.txt', 'webViewLink': 'https://drive/x'}
        # first request json body == {'name': 'a.txt', 'parents': ['folder9']}; header X-Upload-Content-Type == 'text/plain'; PUT body == b'hello'; result id 'F1'
    def test_upload_with_file_id_patches_instead_of_creating(self):      # PATCH /upload/drive/v3/files/F1?uploadType=resumable, no parents
    def test_download_uses_alt_media(self):                               # GET /drive/v3/files/F1?alt=media → b'bytes'
    def test_download_exports_google_docs_as_text(self):                  # info mimeType application/vnd.google-apps.document → GET /files/F1/export?mimeType=text/plain
    def test_list_files_pages_to_the_end(self):                           # two pages via nextPageToken; q == "'root' in parents and trashed=false"
    def test_ensure_folder_walks_and_creates(self):                        # 'Koli/Meetings': Koli exists (list returns it), Meetings created under it; returns new id
    def test_query_escapes_quotes(self):                                   # ensure_folder("Avi's") sends q containing "name='Avi\\'s'"
    def test_share_link_adds_anyone_reader_and_returns_link(self):        # POST /files/F1/permissions body {'role': 'reader', 'type': 'anyone'}; returns webViewLink from info
    def test_401_refreshes_once_and_retries(self):                         # first GET 401, /token → A2, second GET 200 with Bearer A2; exactly 2 GETs
    def test_not_connected_raises_before_any_request(self):
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_drive.ApiTests -v`
Expected: FAIL with `ModuleNotFoundError: koli.drive.api`

- [ ] **Step 3: Implement `drive/api.py`**

`upload` reads the file, guesses `mime` with `mimetypes.guess_type` (fallback `application/octet-stream`), name from the path. Resumable: POST (or PATCH when `file_id`) with `uploadType=resumable` and `fields=FIELDS`, headers `X-Upload-Content-Type`, `X-Upload-Content-Length`; then one PUT of all bytes to the `location` header with `Content-Type: mime`. `share_link` ignores a 400 saying the permission already exists. `delete` is PATCH `{'trashed': true}`. `list_files` filters `trashed=false`, `pageSize=200`, `fields=nextPageToken,files(FIELDS)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_drive -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C S:\Koli-google-drive add src/koli/drive/api.py src/koli/drive/__init__.py tests/test_drive.py
git -C S:\Koli-google-drive commit -m "feat(drive): upload, download, list, folders and share links on Drive v3"
```

---

### Task 4: Bridge, keep-alive and doctor

**Files:**
- Modify: `src/koli/ui/bridge.py` (after `ai_login_cancel`, ~line 828; `_URL_ALLOW` line 22), `src/koli/llm/__init__.py:216-225`, `src/koli/doctor.py:337` and after `_check_ai`
- Test: `tests/test_drive.py`

**Interfaces:**
- Produces bridge methods (all `@_api`): `drive_status() -> ACCOUNT.status() + {client_id, client_hint, redirect_uri}`, `drive_set_client(client_id, client_secret)`, `drive_login_start()`, `drive_login_status()`, `drive_login_open()` (opens `login_state()['url']`, same rule as `ai_login_open`), `drive_login_cancel()`, `drive_disconnect()`.
- `_URL_ALLOW` gains `'https://console.cloud.google.com'`.
- `llm.start_background` loop also calls `drive.ACCOUNT.keep_alive()` when `drive.ACCOUNT.status()['connected']`.
- `doctor._check_drive() -> int`: `Check("Google Drive")`: `info("not set up")` when no client, `warn("connect in Settings › Google Drive")` when configured but not connected, `warn("needs connecting again: <error>")` when dead, `ok("connected as <email>")`. Never refreshes. Called right after `_check_ai(cfg)`.

- [ ] **Step 1: Write the failing tests**

```python
class WiringTests(DriveCase):
    def test_drive_status_includes_client_and_redirect(self):   # Bridge instance built the way tests/test_smoke.py builds one; data['redirect_uri'] == auth.REDIRECT_URI
    def test_doctor_line_for_each_state(self):                   # capture stdout of doctor._check_drive(); 'not set up' / 'connect in Settings' / 'connected as studio@'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_drive.WiringTests -v`
Expected: FAIL with `AttributeError: drive_status` / `_check_drive`

- [ ] **Step 3: Implement the bridge methods, the upkeep call and the doctor check**

- [ ] **Step 4: Run the full test file and the smoke tests**

Run: `python -m unittest tests.test_drive tests.test_smoke -v 2>&1 | tail -5`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git -C S:\Koli-google-drive add src/koli/ui/bridge.py src/koli/llm/__init__.py src/koli/doctor.py tests/test_drive.py
git -C S:\Koli-google-drive commit -m "feat(drive): bridge calls, daily keep-alive and a doctor line"
```

---

### Task 5: Settings › Google Drive card, mock and docs

**Files:**
- Modify: `src/koli/settings/schema.py:56` (SECTIONS, after `ai`), `src/koli/ui/web/js/screens/settings.js` (new `GoogleDrive` block near `AiProviders`; `SECTION_EXTRAS` ~line 453; render ~line 503), `src/koli/ui/web/js/mock-api.js` (`drive_*` entries next to `ai_*`), `docs/project-index.md:44` (a row after AI Providers), `CHANGELOG.md` (Unreleased › Added), `docs/design/google-drive.md` (status line → implemented)
- Test: browser mock

**Interfaces:**
- Section: `{'id': 'drive', 'title': 'Google Drive', 'desc': 'One Google account Koli can keep files in and share them from. The login is stored encrypted, never in the settings file.'}`.
- `SECTION_EXTRAS.drive = { drive: 'Google Drive connect client id secret share upload account' }`.
- `GoogleDrive()` component: status chip (tones as `chipTone`: `reconnect` → warn, `connected` → ok, else plain) with "Not set up" / "Not connected" / `Connected as <email>` / "Connect again"; `TextField` "Client id" committing `drive_set_client(id, '')`; secret row showing `client_hint` and a "Change…" button using `promptModal({secret: true})` → `drive_set_client(client_id, secret)`; a `small` line "Add `http://127.0.0.1:52880/callback` to the client's Authorized redirect URIs" with an `open_link` to `https://console.cloud.google.com/auth/clients`; buttons Connect / Connect again (`openDriveLogin`) and Disconnect (`confirmModal` then `drive_disconnect`). Connect is disabled until `configured`.
- `openDriveLogin(reload)`: `openDeviceLogin` without the code block: calls `drive_login_start`, `drive_login_open`, polls `drive_login_status` every `LOGIN_POLL_MS`; body text "Sign in with the Google account Koli should use. Waiting for Google…".
- Mock: `drive_status` returns `{configured: true, connected: true, email: 'studio@perspective-media.me', client_id: '1234-abc.apps.googleusercontent.com', client_hint: 'GOCSPX-••••1234', redirect_uri: 'http://127.0.0.1:52880/callback', reconnect: false, error: ''}`; `drive_set_client`, `drive_disconnect`, `drive_login_*` return `ok(null)` / `ok({pending: true, url: 'https://accounts.google.com/o/oauth2/v2/auth', error: '', connected: false})`.

- [ ] **Step 1: Add the section, extras entry, component, render line and mock entries**

- [ ] **Step 2: Check the card in the browser mock**

Run: `python -m http.server 8765 --directory src/koli/ui/web` then open `http://localhost:8765/index.html?mock=1`, Settings › Google Drive.
Expected: the card shows "Connected as studio@perspective-media.me", the client id, the masked secret, Disconnect; the search term "drive" finds the section; Connect popup opens from the mock with "Waiting for Google…" and Cancel closes it.

- [ ] **Step 3: Add the project-index row, the CHANGELOG entry and flip the design doc status**

CHANGELOG, under Added: "**Google Drive.** Settings › Google Drive signs Koli in to one Google account (paste a Google OAuth client id and secret, press Connect). Koli can now put files in Drive, read them back and make a link anyone can open; the first use is meeting files, coming next."

- [ ] **Step 4: Run all tests**

Run: `python -m unittest discover tests 2>&1 | tail -3`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git -C S:\Koli-google-drive add src/koli/settings/schema.py src/koli/ui/web/js/screens/settings.js src/koli/ui/web/js/mock-api.js docs/project-index.md CHANGELOG.md docs/design/google-drive.md docs/plans/2026-10-19-google-drive-implementation.md
git -C S:\Koli-google-drive commit -m "feat(drive): Settings › Google Drive card, mock, docs"
```

---

### Task 6: Real connect (user), then merge

Not code. Launch Koli from the worktree (`/test-from-wsl` or `run-koli.py` with `PYTHONUTF8=1`), paste the CRM client id and secret, press Connect, sign in as `studio@perspective-media.me`. Then from a Python prompt in the worktree:

```python
from koli import drive
f = drive.upload_bytes(b'hello from Koli', 'koli-test.txt', 'text/plain', drive.ensure_folder('Koli'))
print(drive.download(f['id']), drive.share_link(f['id']))
drive.delete(f['id'])
```

Expected: the bytes come back, the link opens in a private window, the file lands in Drive's bin. `koli --doctor` prints "Google Drive … connected as studio@…". Only then: merge to master per the task registry protocol.
