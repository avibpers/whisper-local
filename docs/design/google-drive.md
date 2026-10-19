# Google Drive connection

Status: implemented 2026-10-19 on `feat/google-drive` (plan:
`docs/plans/2026-10-19-google-drive-implementation.md`).

## Goal

Koli can sign in to one Google account and, from Python, write a file to
Google Drive, read a file back, and turn a file into a link anyone can open.
That is the whole deliverable of this step. What uses it (keeping meetings in
Drive, exporting a meeting, sharing one) comes in a later design and only
needs to import this package.

## Why now

Meetings produce files people want somewhere other than `%APPDATA%\koli`:
a shared folder, a link to send after the call. Drive is where this user's
other work (the CRM) already lives, so Koli joins the same account.

## Non-goals and anti-goals

- No meeting export or Drive browsing UI in this step.
- No Google SDK. Plain REST on the standard library, like the AI providers and
  like the CRM's PHP client (`F:\Dropbox\CRM_v2\api\api-google-drive-functions.php`).
- Never put a token, the client id or the client secret in `user_settings.yaml`,
  a settings export or a log bundle.
- Never a second Google account at once. Connecting again replaces the account.
- Koli does not create or change anything in Google Cloud Console. The user does
  that (one URI, once).

## The Google side (user steps, not code)

For now Koli borrows the CRM's OAuth client (a "Web application" client in the
Perspective Google Cloud project). Later the user makes a verified client of
their own and pastes the new id and secret in; nothing in the code changes.

1. In Google Cloud Console, open that client and add this exact address to
   **Authorized redirect URIs**: `http://127.0.0.1:52880/callback`.
   Google exempts loopback addresses from its HTTPS rule, but a web client's
   redirect must match character for character, so the port is fixed.
   Done by the user on 2026-10-19 (URI 3 on the client, next to the two CRM
   callbacks).
2. The consent screen is **Internal** (Google Workspace, perspective-media.me
   only). So there is no "Testing" 7-day token limit, and only an account on
   that domain can sign in; a Gmail or other-domain account is refused by
   Google with "access blocked". Koli shows Google's error as is.
3. In Koli: Settings › Google Drive, paste the client id and secret, press
   **Connect**, sign in with the account the files should belong to
   (`studio@perspective-media.me`, already connected to the CRM with the
   Drive scope).

Scopes asked: `https://www.googleapis.com/auth/drive openid email`. My call:
full Drive rather than `drive.file`, so Koli can read files it did not create
(a meeting folder someone else made) and so the consent screen matches what
these accounts already granted the CRM. When the user's own verified client
exists, `drive.file` is the narrower option to revisit.

## Where it lives

New package `src/koli/drive/` (proposed, see "Open for the user"):

| File | Holds |
|---|---|
| `drive/__init__.py` | 2-line header; re-exports the public calls below |
| `drive/auth.py` | client id/secret store, the browser login with a loopback catcher, refresh, status, disconnect, keep-alive |
| `drive/api.py` | Drive v3 calls: upload (resumable), download, metadata, list, create folder, ensure folder path, share link, unshare |

Not under `llm/`: Drive is not an AI provider. It reuses two `llm` modules as
shared helpers: `llm/credentials.py` (DPAPI-encrypted files, atomic writes,
the refresh lock) and `llm/http.py` (JSON and form calls, `HttpError`,
`NetworkError`). `http.py` gains one public helper, `raw(method, url, headers,
body, timeout) -> (status, headers, bytes)`, for the resumable upload PUT and
the `alt=media` download, which are not JSON.

### Secrets

Two encrypted records in `%APPDATA%\koli\credentials\`:

- `google_client.bin`: `{client_id, client_secret}`. Pasted in Settings. The
  UI shows the id and a masked secret (`GOCSPX-••••1234`), like the CRM.
- `google_drive.bin`: `{access, refresh, expires, obtained_at, last_refresh,
  email, scope, dead, last_error}`. Same shape as the Codex/Grok records so
  `--doctor` and the settings card read it the same way.

### Login (auth.py)

Google does not allow the Drive scope on a device-code login, so the login is
the web flow caught on this machine:

1. `start_login()` binds `127.0.0.1:52880` (a one-request `http.server` on a
   daemon thread), makes a `state` nonce and a PKCE verifier, and returns the
   consent URL (`accounts.google.com/o/oauth2/v2/auth` with `access_type=offline`,
   `prompt=consent`, the fixed redirect, the scopes). If the port is taken the
   error says so by name ("port 52880 is in use; close the other program or
   wait a minute").
2. The page polls `login_state()` every 2 s, as the AI logins do: `{pending,
   url, error, connected, email}`. No user code to type.
3. The callback checks `state`, trades the code for tokens (`client_secret`
   plus `code_verifier`), reads the email from `openidconnect.googleapis.com/v1/userinfo`,
   saves the record, and answers the browser with a plain page: "Connected
   <email>. You can close this tab." A missing refresh token is an error
   ("Google did not hand out a refresh token. Press Connect again."), and
   `prompt=consent` makes a repeat hand one out. The error never sends the
   user to myaccount.google.com/permissions: that page removes the whole
   Google project's access, which would also cut the CRM off from that
   account.
4. Give up after 15 minutes; `cancel_login()` closes the server.

**Disconnect** deletes the local record only; it does not call Google's
revoke endpoint. The client is shared with the CRM and the same accounts are
signed in there, so Koli never touches the grant at Google (my call).

**Changing the client** (`drive_set_client` with a different client id)
clears the saved login, because a refresh token is bound to the client that
issued it. The refresh dead-token rule covers `invalid_client` and
`unauthorized_client` as well as `invalid_grant`, so a stale pairing shows
"Connect again" rather than a bare HTTP error.

### Refresh and keep-alive

`bearer()` returns a fresh access token, refreshing when it expires within 5
minutes or was last refreshed over 6 days ago, under the thread lock and the
`credentials.file_lock`, exactly the AI `Subscription` logic. Google's refresh
needs `client_secret` in the form, which the `Subscription` base does not send,
so `auth.py` is a small standalone class that copies `_needs_refresh`, the
dead-token rule (`invalid_grant` → `dead: true`, status "connect again") and
`keep_alive()`, rather than subclassing. My call: a copy of ~40 lines beats a
hook on a class whose other half (device polling) does not apply.
`keep_alive()` is called next to the AI subscriptions' daily keep-alive in
`llm/__init__.py`.

`DriveNotConnected` (a `RuntimeError`) is what every call raises when there is
no account or the account needs reconnecting, with the Settings path in the
message.

### Files (api.py)

All calls take the one connected account, pass `supportsAllDrives=true`, and
retry once on 401 with a forced refresh (the `stale` pattern).

| Call | Does |
|---|---|
| `upload(path, name=None, mime=None, folder_id=None, file_id=None) -> {id, name, webViewLink}` | Resumable session then one PUT of the bytes (files are MB, not GB; chunking is not needed). `file_id` updates that file's content instead of creating one. |
| `upload_bytes(data, name, mime, folder_id=None)` | Same for text already in memory (a transcript). |
| `download(file_id) -> bytes` and `download_to(file_id, path)` | `alt=media`. Google Docs types are exported as `text/plain`. |
| `info(file_id) -> {id, name, mimeType, size, modifiedTime, webViewLink, parents, shared}` | One metadata call. |
| `list_files(folder_id='root', query='') -> [info…]` | Non-trashed children, 200 per page, paged to the end. |
| `create_folder(name, parent_id='root') -> {id, webViewLink}` | |
| `ensure_folder('Koli/Meetings') -> id` | Walks or creates each segment under My Drive. The later meeting design picks the path. |
| `share_link(file_id) -> url` | Adds the `anyone`/`reader` permission (idempotent) and returns `webViewLink`. |
| `unshare(file_id)` | Removes that permission. |
| `delete(file_id)` | Trash, not permanent delete. |

### Settings and UI

- A new Settings section `drive` titled "Google Drive", after AI (placement is
  the user's call, below). Its card: status chip (Not set up / Not connected /
  Connected as <email> / Connect again), the client id field, the masked
  secret with "Change…", and Connect / Disconnect. The connect popup is the
  AI device popup without the code: "Open the sign-in page" and "Waiting for
  Google…", closing itself on success.
- `settings/schema.py`: the section entry; no yaml keys (secrets are not
  settings). `simple_mode.py` does not list it.
- `ui/bridge.py`: `drive_status`, `drive_set_client(client_id, client_secret)`,
  `drive_login_start`, `drive_login_status`, `drive_login_open`,
  `drive_login_cancel`, `drive_disconnect`. `accounts.google.com` and
  `console.cloud.google.com` join the link allow list (the card links to the
  console page where the redirect URI is added).
- `ui/web/js/screens/settings.js`: the card; `mock-api.js` and
  `mock-data.json`: a connected and a not-connected sample.
- `doctor.py`: a "Google Drive" line under config: connected as <email>, not
  connected, or needs reconnecting (and why).
- `docs/project-index.md`: one row; `CHANGELOG.md`.

### Testing

- `tests/test_drive.py` with a fake `http` (monkeypatched `raw`/`post_form`):
  code exchange saves a record with the email; refresh sends the secret;
  `invalid_grant` marks the record dead and the next call raises
  `DriveNotConnected`; upload opens a session then PUTs; `share_link` adds the
  permission and returns the link; the loopback catcher answers a GET with the
  right `state` and rejects a wrong one.
- The browser mock (`index.html?mock=1`) for the card in both states.
- A real connect by the user before commit (project rule), then one upload,
  one download and one share link from a Python prompt.

## Decided with the user (2026-10-19)

- The card is its own "Google Drive" section after AI.
- The port is `52880`; it is registered on the client and must not change.
- Design approved in chat; next is the implementation plan.

## Later

Meeting files in Drive, Export to Drive and Share from the Meetings screen are
a separate design on top of `drive.upload`, `drive.ensure_folder` and
`drive.share_link`. The user's own verified Google client replaces the CRM's by
pasting the new id and secret and connecting again.
