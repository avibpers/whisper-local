# platform/macos/secrets.py
# Mirror of the Windows DPAPI module. Until a Keychain version exists the bytes
# pass through unchanged; llm/credentials.py makes the file readable only by
# you (mode 600).


def protect(data: bytes) -> bytes:
    return data


def unprotect(blob: bytes) -> bytes:
    return blob
