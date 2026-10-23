#!/usr/bin/env python3
"""
MouseShare — Protocol helpers
Framed TCP: 4-byte big-endian unsigned int (message length) + JSON UTF-8 body.
"""
import json
import struct

HEADER_SIZE   = 4
MAX_MSG_BYTES = 10 * 1024 * 1024  # 10 MB sanity cap


def send_message(sock, msg_dict: dict) -> bool:
    """Serialise msg_dict as JSON and send it over sock.
    Returns True on success, False if the socket is broken."""
    try:
        payload = json.dumps(msg_dict, separators=(",", ":")).encode("utf-8")
        header  = struct.pack(">I", len(payload))
        sock.sendall(header + payload)
        return True
    except OSError:
        return False


def recv_message(sock):
    """Receive one framed message from sock.
    Returns the decoded dict, or None if the connection is closed/broken."""
    raw_len = _recv_exact(sock, HEADER_SIZE)
    if raw_len is None:
        return None
    msg_len = struct.unpack(">I", raw_len)[0]
    if msg_len == 0 or msg_len > MAX_MSG_BYTES:
        return None
    payload = _recv_exact(sock, msg_len)
    if payload is None:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _recv_exact(sock, n: int):
    """Read exactly n bytes from sock. Returns None on EOF/error."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)
