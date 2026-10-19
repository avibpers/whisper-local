# drive/__init__.py
# Google Drive for the rest of the app: one signed-in Google account (auth.py)
# and the file calls on it (api.py). Design: docs/design/google-drive.md.

from .auth import ACCOUNT, DriveNotConnected, client_hint, get_client, set_client  # noqa: F401
