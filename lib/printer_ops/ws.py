"""ws.py — a minimal stdlib WebSocket client (RFC 6455) for printer control channels.

WHY its own client: Creality stock firmware speaks JSON over a plain `ws://host:9999`
socket, and `3d` command modules must stay stdlib-only (tests/test_imports.py). The
printer side needs only text frames, ping/pong and close, so a ~100-line client beats a
new dependency (websocket-client) for every `3d` install.

SCOPE: client role only, `ws://` only (no TLS, no extensions, no compression). Outgoing
frames are masked as RFC 6455 requires of clients; incoming fragmented messages are
reassembled. `recv()` returns None when the read timeout elapses, so callers can poll for
a bounded time without exceptions as control flow.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import time

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(OSError):
    """Handshake failure or protocol violation on the WebSocket channel."""


class WebSocketClosed(WebSocketError):
    """The peer closed the connection (close frame or EOF)."""


def encode_frame(opcode: int, payload: bytes, *, mask: bool) -> bytes:
    """One FIN frame. Clients mask (mask=True); the test-side fake server does not."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 1 << 16:
        head += bytes([mbit | 126]) + struct.pack(">H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack(">Q", n)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def parse_frame(buf: bytes) -> tuple[bool, int, bytes, int] | None:
    """Parse one frame from the head of `buf` -> (fin, opcode, unmasked payload, bytes used),
    or None when `buf` does not yet hold a complete frame."""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    n, pos = b1 & 0x7F, 2
    if n == 126:
        if len(buf) < 4:
            return None
        (n,) = struct.unpack(">H", buf[2:4])
        pos = 4
    elif n == 127:
        if len(buf) < 10:
            return None
        (n,) = struct.unpack(">Q", buf[2:10])
        pos = 10
    key = b""
    if b1 & 0x80:
        if len(buf) < pos + 4:
            return None
        key, pos = buf[pos:pos + 4], pos + 4
    if len(buf) < pos + n:
        return None
    data = buf[pos:pos + n]
    if key:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return bool(b0 & 0x80), b0 & 0x0F, data, pos + n


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()


class WebSocket:
    """A connected client socket. Use `connect()`; close with `close()` or a `with` block."""

    def __init__(self, sock: socket.socket, buffered: bytes = b"") -> None:
        self._sock = sock
        self._buf = buffered  # bytes received but not yet parsed into a whole frame

    @classmethod
    def connect(cls, host: str, port: int, path: str = "/", *, timeout: float = 5.0) -> WebSocket:
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            req = (
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(req.encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(1024)
                if not chunk:
                    raise WebSocketError(f"ws://{host}:{port}{path}: connection closed during handshake")
                head += chunk
                if len(head) > 65536:
                    raise WebSocketError("handshake response too large")
            status_line, _, rest = head.partition(b"\r\n")
            if b" 101 " not in status_line + b" ":
                raise WebSocketError(f"ws://{host}:{port}{path}: handshake refused: {status_line.decode(errors='replace')}")
            header_block, _, leftover = rest.partition(b"\r\n\r\n")
            headers = header_block.decode(errors="replace").lower()
            if accept_key(key).lower() not in headers:
                raise WebSocketError(f"ws://{host}:{port}{path}: bad Sec-WebSocket-Accept")
        except BaseException:
            sock.close()
            raise
        return cls(sock, leftover)

    def send_text(self, text: str) -> None:
        self._sock.sendall(encode_frame(OP_TEXT, text.encode(), mask=True))

    def recv(self, timeout: float) -> str | None:
        """Next text/binary message, or None when `timeout` elapses before one is complete.

        Partial frames stay buffered across calls, so a timeout never desynchronises the
        stream. Pings are answered transparently; a close frame or EOF raises WebSocketClosed."""
        deadline = time.monotonic() + max(timeout, 0.0)
        parts: list[bytes] = []
        while True:
            frame = parse_frame(self._buf)
            if frame is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    if parts:
                        raise WebSocketError("timed out inside a fragmented message")
                    return None
                self._sock.settimeout(left)
                try:
                    chunk = self._sock.recv(65536)
                except (socket.timeout, TimeoutError):
                    continue  # the deadline check above decides
                if not chunk:
                    raise WebSocketClosed("connection closed by peer")
                self._buf += chunk
                continue
            fin, op, data, used = frame
            self._buf = self._buf[used:]
            if op == OP_PING:
                self._sock.sendall(encode_frame(OP_PONG, data, mask=True))
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                raise WebSocketClosed("peer sent close")
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._sock.sendall(encode_frame(OP_CLOSE, b"", mask=True))
        except OSError:
            pass  # the firmware may already have dropped its side
        finally:
            self._sock.close()

    def __enter__(self) -> WebSocket:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
