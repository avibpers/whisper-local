# platform/windows/secrets.py
# Encrypts the AI logins and API keys with DPAPI, tied to your Windows account:
# another user on this PC, or the file copied to another PC, can't read them.
# llm/credentials.py stores what protect() returns.

import win32crypt

_DESCRIPTION = 'Koli AI credentials'


def protect(data: bytes) -> bytes:
    return win32crypt.CryptProtectData(data, _DESCRIPTION, None, None, None, 0)


def unprotect(blob: bytes) -> bytes:
    return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]
