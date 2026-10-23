"""Authenticated, encrypted transport for paired Mousehare endpoints.

The pairing key authenticates an ephemeral X25519 exchange. AES-GCM protects
each framed control message and monotonically increasing sequence numbers
reject replayed, duplicated, and reordered messages.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# Keep the established wire version so the current macOS build can pair with
# Windows installations already in use. The encrypted message format and the
# handshake have not changed; only the local pairing-key validation changed.
PROTOCOL_VERSION = 3
ALLOWED_MAC_POSITIONS = frozenset(("left", "right", "top", "bottom"))
MAX_PLAINTEXT_BYTES = 64 * 1024


class HandshakeError(ValueError):
    """Pairing or protocol negotiation failed."""


def opposite_edge(edge: str) -> str:
    return {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}[edge]


def _token_bytes(token: str) -> bytes:
    if not isinstance(token, str) or not token:
        raise HandshakeError("Pairing key is required.")
    return token.encode("utf-8")


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value, expected_size: int) -> bytes:
    if not isinstance(value, str):
        raise HandshakeError("Invalid pairing handshake.")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise HandshakeError("Invalid pairing handshake.") from exc
    if len(decoded) != expected_size:
        raise HandshakeError("Invalid pairing handshake.")
    return decoded


def _transcript(client_nonce: bytes, client_public: bytes, server_nonce: bytes,
                server_public: bytes, mac_position: str,
                client_clipboard: bool, clipboard_enabled: bool) -> bytes:
    return (b"Mousehare pairing v3\0" + client_nonce + client_public
            + server_nonce + server_public + mac_position.encode("ascii")
            + bytes((client_clipboard, clipboard_enabled)))


def _proof(token: bytes, label: bytes, transcript: bytes) -> bytes:
    return hmac.digest(token, label + b"\0" + transcript, "sha256")


def _new_channel(shared_secret: bytes, client_nonce: bytes, server_nonce: bytes,
                 transcript: bytes, *, is_client: bool) -> "SecureChannel":
    root = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=client_nonce + server_nonce,
        info=b"Mousehare encrypted session v3\0" + transcript,
    ).derive(shared_secret)
    c2s = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"Mousehare client to server v3").derive(root)
    s2c = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"Mousehare server to client v3").derive(root)
    return SecureChannel(
        send_key=c2s if is_client else s2c,
        receive_key=s2c if is_client else c2s,
    )


def client_handshake(sock, token: str, mac_position: str, clipboard_sync: bool,
                     send_message, recv_message) -> tuple["SecureChannel", bool]:
    """Authenticate the server and establish an encrypted client channel."""
    secret = _token_bytes(token)
    if mac_position not in ALLOWED_MAC_POSITIONS:
        raise HandshakeError("Choose a valid Mac position.")
    if type(clipboard_sync) is not bool:
        raise HandshakeError("Invalid clipboard sharing preference.")

    client_nonce = os.urandom(32)
    client_private = X25519PrivateKey.generate()
    client_public = client_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    initial = _transcript(client_nonce, client_public, b"", b"", mac_position,
                          clipboard_sync, False)
    hello = {
        "type": "hello",
        "version": PROTOCOL_VERSION,
        "client_nonce": _b64(client_nonce),
        "client_key": _b64(client_public),
        "mac_position": mac_position,
        "clipboard_sync": clipboard_sync,
        "proof": _b64(_proof(secret, b"client", initial)),
    }
    if not send_message(sock, hello):
        raise HandshakeError("Could not send the pairing request.")
    reply = recv_message(sock)
    if not isinstance(reply, dict) or reply.get("type") != "hello_ok":
        if isinstance(reply, dict) and reply.get("type") == "hello_error" \
                and reply.get("version") != PROTOCOL_VERSION:
            raise HandshakeError(
                "The Windows server uses an older protocol. Update Mousehare on both computers."
            )
        raise HandshakeError("Pairing key rejected or server protocol is incompatible.")
    if reply.get("version") != PROTOCOL_VERSION:
        raise HandshakeError("The Windows server uses an incompatible protocol version.")
    clipboard_enabled = reply.get("clipboard_sync")
    if type(clipboard_enabled) is not bool or (clipboard_enabled and not clipboard_sync):
        raise HandshakeError("Invalid clipboard sharing negotiation.")

    server_nonce = _unb64(reply.get("server_nonce"), 32)
    server_public = _unb64(reply.get("server_key"), 32)
    transcript = _transcript(client_nonce, client_public, server_nonce,
                             server_public, mac_position, clipboard_sync,
                             clipboard_enabled)
    server_proof = _unb64(reply.get("proof"), 32)
    if not hmac.compare_digest(server_proof, _proof(secret, b"server", transcript)):
        raise HandshakeError("Server identity could not be verified. Check the pairing key.")

    try:
        shared = client_private.exchange(X25519PublicKey.from_public_bytes(server_public))
    except ValueError as exc:
        raise HandshakeError("Invalid server key exchange.") from exc
    channel = _new_channel(shared, client_nonce, server_nonce, transcript, is_client=True)
    if not channel.send(sock, {"type": "finish"}, send_message):
        raise HandshakeError("Could not finish the secure pairing exchange.")
    return channel, clipboard_enabled


def server_handshake(sock, token: str, allow_clipboard_sync: bool,
                     send_message, recv_message):
    """Return (secure channel, Mac position, negotiated clipboard setting)."""
    secret = _token_bytes(token)
    hello = recv_message(sock)
    if not isinstance(hello, dict) or hello.get("type") != "hello" \
            or hello.get("version") != PROTOCOL_VERSION:
        _send_handshake_error(sock, send_message)
        raise HandshakeError("Client protocol is incompatible.")

    mac_position = hello.get("mac_position")
    client_clipboard = hello.get("clipboard_sync")
    if mac_position not in ALLOWED_MAC_POSITIONS:
        _send_handshake_error(sock, send_message)
        raise HandshakeError("Invalid Mac screen position.")
    if type(client_clipboard) is not bool or type(allow_clipboard_sync) is not bool:
        _send_handshake_error(sock, send_message)
        raise HandshakeError("Invalid clipboard sharing preference.")

    try:
        client_nonce = _unb64(hello.get("client_nonce"), 32)
        client_public = _unb64(hello.get("client_key"), 32)
        client_proof = _unb64(hello.get("proof"), 32)
    except HandshakeError:
        _send_handshake_error(sock, send_message)
        raise

    initial = _transcript(client_nonce, client_public, b"", b"", mac_position,
                          client_clipboard, False)
    if not hmac.compare_digest(client_proof, _proof(secret, b"client", initial)):
        _send_handshake_error(sock, send_message)
        raise HandshakeError("Pairing key rejected.")

    server_nonce = os.urandom(32)
    server_private = X25519PrivateKey.generate()
    server_public = server_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    transcript = _transcript(client_nonce, client_public, server_nonce,
                             server_public, mac_position, client_clipboard,
                             bool(client_clipboard and allow_clipboard_sync))
    clipboard_enabled = bool(client_clipboard and allow_clipboard_sync)
    response = {
        "type": "hello_ok",
        "version": PROTOCOL_VERSION,
        "server_nonce": _b64(server_nonce),
        "server_key": _b64(server_public),
        "clipboard_sync": clipboard_enabled,
        "proof": _b64(_proof(secret, b"server", transcript)),
    }
    if not send_message(sock, response):
        raise HandshakeError("Could not finish the pairing exchange.")
    try:
        shared = server_private.exchange(X25519PublicKey.from_public_bytes(client_public))
    except ValueError as exc:
        raise HandshakeError("Invalid client key exchange.") from exc
    channel = _new_channel(shared, client_nonce, server_nonce, transcript, is_client=False)
    finish = channel.receive(sock, recv_message)
    if not isinstance(finish, dict) or finish != {"type": "finish"}:
        raise HandshakeError("Client did not finish secure pairing.")
    return channel, mac_position, clipboard_enabled


def _send_handshake_error(sock, send_message) -> None:
    try:
        send_message(sock, {"type": "hello_error", "version": PROTOCOL_VERSION})
    except Exception:
        pass


class SecureChannel:
    """AES-GCM protected JSON message stream with strict sequence validation."""

    def __init__(self, *, send_key: bytes, receive_key: bytes):
        self._send_cipher = AESGCM(send_key)
        self._recv_cipher = AESGCM(receive_key)
        self._send_sequence = 0
        self._receive_sequence = 0
        self._send_lock = threading.Lock()

    @staticmethod
    def _nonce(sequence: int) -> bytes:
        return b"\0\0\0\0" + sequence.to_bytes(8, "big")

    @staticmethod
    def _aad(sequence: int) -> bytes:
        return b"Mousehare encrypted message v3\0" + sequence.to_bytes(8, "big")

    def send(self, sock, message: dict, send_message) -> bool:
        if not isinstance(message, dict):
            return False
        try:
            plaintext = json.dumps(message, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            return False
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            return False
        with self._send_lock:
            sequence = self._send_sequence
            if sequence >= (1 << 64) - 1:
                return False
            ciphertext = self._send_cipher.encrypt(
                self._nonce(sequence), plaintext, self._aad(sequence))
            envelope = {
                "type": "encrypted",
                "sequence": sequence,
                "data": _b64(ciphertext),
            }
            if not send_message(sock, envelope):
                return False
            self._send_sequence += 1
            return True

    def receive(self, sock, recv_message) -> dict | None:
        envelope = recv_message(sock)
        if not isinstance(envelope, dict) or envelope.get("type") != "encrypted":
            return None
        sequence = envelope.get("sequence")
        if type(sequence) is not int or sequence != self._receive_sequence \
                or sequence >= (1 << 64) - 1:
            return None
        try:
            ciphertext = base64.b64decode(envelope.get("data", ""), validate=True)
            plaintext = self._recv_cipher.decrypt(
                self._nonce(sequence), ciphertext, self._aad(sequence))
            if len(plaintext) > MAX_PLAINTEXT_BYTES:
                return None
            message = json.loads(plaintext.decode("utf-8"))
        except (InvalidTag, UnicodeDecodeError, ValueError, TypeError,
                json.JSONDecodeError):
            return None
        if not isinstance(message, dict):
            return None
        self._receive_sequence += 1
        return message
