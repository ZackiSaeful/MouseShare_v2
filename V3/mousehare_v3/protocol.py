"""Small, strict, shared V3 framing and authenticated encrypted channel."""
from __future__ import annotations

import base64
import hmac
import json
import os
import socket
import struct
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

VERSION = 1
MAX_FRAME = 128 * 1024


class ProtocolError(ValueError):
    pass


def _read_exact(sock: socket.socket, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        data = sock.recv(count - len(chunks))
        if not data:
            raise ConnectionError("Peer closed the connection")
        chunks.extend(data)
    return bytes(chunks)


def send_frame(sock: socket.socket, value: dict) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    if not 0 < len(payload) <= MAX_FRAME:
        raise ProtocolError("Frame size is invalid")
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def receive_frame(sock: socket.socket) -> dict:
    size = struct.unpack("!I", _read_exact(sock, 4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ProtocolError("Frame size is invalid")
    try:
        result = json.loads(_read_exact(sock, size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("Frame is not valid UTF-8 JSON") from exc
    if not isinstance(result, dict):
        raise ProtocolError("Frame must contain a JSON object")
    return result


def _proof(token: str, label: bytes, transcript: bytes) -> bytes:
    return hmac.digest(token.encode("utf-8"), label + b"\0" + transcript, "sha256")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _decode_b64(value) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("Invalid handshake encoding") from exc
    return decoded


def _unb64(value, size: int) -> bytes:
    decoded = _decode_b64(value)
    if len(decoded) != size:
        raise ProtocolError("Invalid handshake field size")
    return decoded


def _derive(private, peer_public: bytes, client_nonce: bytes, server_nonce: bytes,
            transcript: bytes, *, client: bool) -> "SecureChannel":
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(peer_public))
    except ValueError as exc:
        raise ProtocolError("Invalid peer public key") from exc
    root = HKDF(algorithm=hashes.SHA256(), length=32,
                salt=client_nonce + server_nonce,
                info=b"Mousehare V3 session\0" + transcript).derive(shared)
    c2s = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"Mousehare V3 client to server").derive(root)
    s2c = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"Mousehare V3 server to client").derive(root)
    return SecureChannel(c2s if client else s2c, s2c if client else c2s,
                         b"CLNT" if client else b"SRVR",
                         b"SRVR" if client else b"CLNT")


class SecureChannel:
    def __init__(self, send_key: bytes, recv_key: bytes, send_prefix: bytes, recv_prefix: bytes):
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._send_prefix = send_prefix
        self._recv_prefix = recv_prefix
        self._send_seq = self._recv_seq = 0
        self._send_lock = threading.Lock()

    @staticmethod
    def _aad(sequence: int) -> bytes:
        return b"Mousehare V3 encrypted frame\0" + sequence.to_bytes(8, "big")

    def send(self, sock: socket.socket, message: dict) -> None:
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
        if len(raw) > MAX_FRAME - 128:
            raise ProtocolError("Encrypted message is too large")
        with self._send_lock:
            seq = self._send_seq
            nonce = self._send_prefix + seq.to_bytes(8, "big")
            ciphertext = self._send.encrypt(nonce, raw, self._aad(seq))
            send_frame(sock, {"type": "secure", "seq": seq, "ct": _b64(ciphertext)})
            self._send_seq += 1

    def receive(self, sock: socket.socket) -> dict:
        frame = receive_frame(sock)
        seq = frame.get("seq")
        if frame.get("type") != "secure" or type(seq) is not int or seq != self._recv_seq:
            raise ProtocolError("Encrypted message sequence is invalid")
        ciphertext = _decode_b64(frame.get("ct"))
        if not 16 <= len(ciphertext) <= MAX_FRAME:
            raise ProtocolError("Encrypted message size is invalid")
        try:
            raw = self._recv.decrypt(self._recv_prefix + seq.to_bytes(8, "big"),
                                     ciphertext, self._aad(seq))
            message = json.loads(raw.decode("utf-8"))
        except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("Encrypted message authentication failed") from exc
        if not isinstance(message, dict):
            raise ProtocolError("Encrypted message must be an object")
        self._recv_seq += 1
        return message


def client_handshake(sock: socket.socket, token: str, position: str) -> SecureChannel:
    if not token or position not in ("left", "right", "top", "bottom"):
        raise ProtocolError("Pairing key or Mac position is invalid")
    cn = os.urandom(32)
    private = X25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    transcript0 = b"Mousehare V3 hello\0" + cn + public + position.encode("ascii")
    send_frame(sock, {"type": "hello", "v": VERSION, "cn": _b64(cn), "pk": _b64(public),
                      "position": position, "proof": _b64(_proof(token, b"client", transcript0))})
    reply = receive_frame(sock)
    if reply.get("type") != "welcome" or reply.get("v") != VERSION:
        raise ProtocolError("Windows rejected pairing or uses an incompatible version")
    sn, peer_public = _unb64(reply.get("sn"), 32), _unb64(reply.get("pk"), 32)
    transcript = transcript0 + sn + peer_public
    if not hmac.compare_digest(_unb64(reply.get("proof"), 32), _proof(token, b"server", transcript)):
        raise ProtocolError("Pairing key or Windows identity is invalid")
    channel = _derive(private, peer_public, cn, sn, transcript, client=True)
    channel.send(sock, {"type": "finish"})
    return channel


def server_handshake(sock: socket.socket, token: str) -> tuple[SecureChannel, str]:
    hello = receive_frame(sock)
    if hello.get("type") != "hello" or hello.get("v") != VERSION:
        raise ProtocolError("Mac uses an incompatible protocol")
    position = hello.get("position")
    if position not in ("left", "right", "top", "bottom"):
        raise ProtocolError("Mac position is invalid")
    cn, client_public = _unb64(hello.get("cn"), 32), _unb64(hello.get("pk"), 32)
    transcript0 = b"Mousehare V3 hello\0" + cn + client_public + position.encode("ascii")
    if not hmac.compare_digest(_unb64(hello.get("proof"), 32), _proof(token, b"client", transcript0)):
        raise ProtocolError("Pairing key rejected")
    sn = os.urandom(32)
    private = X25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    transcript = transcript0 + sn + public
    send_frame(sock, {"type": "welcome", "v": VERSION, "sn": _b64(sn), "pk": _b64(public),
                      "proof": _b64(_proof(token, b"server", transcript))})
    channel = _derive(private, client_public, cn, sn, transcript, client=False)
    if channel.receive(sock) != {"type": "finish"}:
        raise ProtocolError("Secure pairing did not finish")
    return channel, position
