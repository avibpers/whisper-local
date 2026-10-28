# cloud_stt/ws.py
# A small WebSocket client (RFC 6455) on the standard library, so the Cloud
# engine needs no extra package in the app build. Enough for a speech
# stream: text and binary messages, continuation frames, ping/pong and close.
# Client frames are always masked, as the protocol requires.

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
from typing import Callable, Optional
from urllib.parse import urlsplit

TEXT, BINARY, CLOSE, PING, PONG, CONTINUATION = 1, 2, 8, 9, 10, 0
_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def encode_frame(opcode: int, payload: bytes, mask_key: bytes) -> bytes:
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack('>H', n)
    else:
        head += bytes([0x80 | 127]) + struct.pack('>Q', n)
    return head + mask_key + _mask(payload, mask_key)


def _mask(payload: bytes, key: bytes) -> bytes:
    if not payload:
        return b''
    repeated = (key * (len(payload) // 4 + 1))[:len(payload)]
    return (int.from_bytes(payload, 'big') ^ int.from_bytes(repeated, 'big')).to_bytes(len(payload), 'big')


# One frame: (opcode, fin, payload), unmasked if the sender masked it.
def read_frame(read_exact: Callable[[int], bytes]) -> tuple:
    b1, b2 = read_exact(2)
    fin, opcode = bool(b1 & 0x80), b1 & 0x0f
    masked, n = bool(b2 & 0x80), b2 & 0x7f
    if n == 126:
        n = struct.unpack('>H', read_exact(2))[0]
    elif n == 127:
        n = struct.unpack('>Q', read_exact(8))[0]
    key = read_exact(4) if masked else b''
    payload = read_exact(n) if n else b''
    return opcode, fin, (_mask(payload, key) if masked else payload)


def _check_accept(key: str, accept: str) -> bool:
    expected = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
    return accept.strip() == expected


class WebSocket:
    def __init__(self, read_exact: Callable[[int], bytes], write: Callable[[bytes], None],
                 close_socket: Optional[Callable[[], None]] = None):
        self._read_exact = read_exact
        self._write = write
        self._close_socket = close_socket or (lambda: None)
        self._send_lock = threading.Lock()
        self.closed = False

    # Opens `url` (ws:// or wss://) with extra handshake `headers`.
    @classmethod
    def connect(cls, url: str, headers: dict, timeout: float = 10) -> 'WebSocket':
        parts = urlsplit(url)
        secure = parts.scheme == 'wss'
        port = parts.port or (443 if secure else 80)
        raw = socket.create_connection((parts.hostname, port), timeout=timeout)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parts.hostname) if secure else raw
        key = base64.b64encode(os.urandom(16)).decode()
        path = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
        lines = [f'GET {path} HTTP/1.1', f'Host: {parts.hostname}', 'Upgrade: websocket', 'Connection: Upgrade',
                 f'Sec-WebSocket-Key: {key}', 'Sec-WebSocket-Version: 13']
        lines += [f'{k}: {v}' for k, v in (headers or {}).items()]
        sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
        reader = sock.makefile('rb')
        status = reader.readline().decode('latin-1').strip()
        response = {}
        while True:
            line = reader.readline().decode('latin-1').strip()
            if not line:
                break
            name, _, value = line.partition(':')
            response[name.strip().lower()] = value.strip()
        if ' 101 ' not in f'{status} ' or not _check_accept(key, response.get('sec-websocket-accept', '')):
            sock.close()
            raise ConnectionError(f'WebSocket handshake refused: {status or "no answer"}')
        sock.settimeout(None)

        def read_exact(n):
            data = reader.read(n)
            if data is None or len(data) < n:
                raise ConnectionError('WebSocket closed')
            return data
        return cls(read_exact, sock.sendall, sock.close)

    def _send(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            if self.closed and opcode != CLOSE:
                raise ConnectionError('WebSocket closed')
            self._write(encode_frame(opcode, payload, os.urandom(4)))

    def send_text(self, text: str) -> None:
        self._send(TEXT, text.encode('utf-8'))

    # The next message: ('text', str) or ('binary', bytes); None once closed.
    def recv(self):
        parts, kind = [], None
        while True:
            try:
                opcode, fin, payload = read_frame(self._read_exact)
            except (ConnectionError, OSError):
                self.closed = True
                return None
            if opcode == PING:
                try:
                    self._send(PONG, payload)
                except (ConnectionError, OSError):
                    pass
                continue
            if opcode == PONG:
                continue
            if opcode == CLOSE:
                if not self.closed:
                    try:
                        self._send(CLOSE, payload[:2])
                    except (ConnectionError, OSError):
                        pass
                self.closed = True
                return None
            if opcode in (TEXT, BINARY):
                kind = opcode
            parts.append(payload)
            if fin:
                data = b''.join(parts)
                return ('text', data.decode('utf-8', 'replace')) if kind == TEXT else ('binary', data)

    def close(self) -> None:
        if self.closed:
            self._close_socket()
            return
        try:
            self._send(CLOSE, struct.pack('>H', 1000))
        except (ConnectionError, OSError):
            pass
        self.closed = True
        try:
            self._close_socket()
        except OSError:
            pass
