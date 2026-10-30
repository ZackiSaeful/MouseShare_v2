"""Opt-in, plain-text clipboard synchronization for paired Mousehare peers.

Clipboard contents are limited to 48 KiB and are only sent over the authenticated
encrypted channel after both users enable sharing. Existing clipboard contents
are used as the initial comparison baseline and are not sent when connecting.
"""
from __future__ import annotations

import ctypes
import json
import logging
import sys
import threading
import time

from .secure_transport import MAX_PLAINTEXT_BYTES

log = logging.getLogger(__name__)
MAX_CLIPBOARD_BYTES = 48 * 1024
CLIPBOARD_POLL_SECONDS = 0.25
_CLIPBOARD_MESSAGE_OVERHEAD = 1024


def valid_clipboard_text(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        normalized = value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        encoded = normalized.encode("utf-8")
        serialized = json.dumps(
            {"type": "clipboard", "text": normalized},
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError, TypeError, ValueError):
        return False
    return (len(encoded) <= MAX_CLIPBOARD_BYTES
            and len(serialized) <= MAX_PLAINTEXT_BYTES - _CLIPBOARD_MESSAGE_OVERHEAD)


def read_clipboard_text() -> str | None:
    """Read plain text only; return None for empty, unsupported, or busy clipboard."""
    try:
        if sys.platform == "darwin":
            import AppKit

            pool = AppKit.NSAutoreleasePool.alloc().init()
            try:
                value = AppKit.NSPasteboard.generalPasteboard().stringForType_(
                    AppKit.NSStringPboardType)
                text = str(value) if value is not None else ""
            finally:
                del pool
            return text if text and valid_clipboard_text(text) else None
        if sys.platform == "win32":
            return _read_windows_clipboard()
    except Exception as exc:
        log.debug("Clipboard read unavailable: %s", exc)
    return None


def write_clipboard_text(text: str) -> bool:
    """Write a validated plain-text value to the local system clipboard."""
    if not valid_clipboard_text(text):
        return False
    try:
        if sys.platform == "darwin":
            import AppKit

            pool = AppKit.NSAutoreleasePool.alloc().init()
            try:
                pasteboard = AppKit.NSPasteboard.generalPasteboard()
                pasteboard.clearContents()
                return bool(pasteboard.setString_forType_(
                    text, AppKit.NSStringPboardType))
            finally:
                del pool
        if sys.platform == "win32":
            return _write_windows_clipboard(text)
    except Exception as exc:
        log.warning("Clipboard write failed: %s", exc)
    return False


def _win_api():
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    return user32, kernel32


def _open_windows_clipboard(user32) -> bool:
    for _attempt in range(20):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.025)
    log.warning("[CLIPBOARD] Windows OpenClipboard busy or locked after 20 attempts")
    return False


def _read_windows_clipboard() -> str | None:
    CF_UNICODETEXT = 13
    user32, kernel32 = _win_api()
    if not _open_windows_clipboard(user32):
        return None
    handle = None
    pointer = None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        size = kernel32.GlobalSize(handle)
        if not size or size > MAX_CLIPBOARD_BYTES * 2 + 2:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        units = size // ctypes.sizeof(ctypes.c_wchar)
        raw = ctypes.wstring_at(pointer, units)
        text = raw.split("\0", 1)[0]
        try:
            text = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        except Exception:
            pass
        return text if text and valid_clipboard_text(text) else None
    finally:
        if pointer:
            kernel32.GlobalUnlock(handle)
        user32.CloseClipboard()


def _write_windows_clipboard(text: str) -> bool:
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    user32, kernel32 = _win_api()
    encoded = text.encode("utf-16-le") + b"\0\0"
    memory = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
    if not memory:
        return False
    transferred = False
    pointer = kernel32.GlobalLock(memory)
    if not pointer:
        kernel32.GlobalFree(memory)
        return False
    try:
        ctypes.memmove(pointer, encoded, len(encoded))
    finally:
        kernel32.GlobalUnlock(memory)
    if not _open_windows_clipboard(user32):
        kernel32.GlobalFree(memory)
        return False
    try:
        if not user32.EmptyClipboard() or not user32.SetClipboardData(CF_UNICODETEXT, memory):
            return False
        transferred = True
        return True
    finally:
        user32.CloseClipboard()
        if memory and not transferred:
            kernel32.GlobalFree(memory)


class ClipboardSynchronizer:
    """Poll local plain text and apply peer updates without echoing them back."""

    def __init__(self, send_text, *, on_error=None):
        self._send_text = send_text
        self._on_error = on_error
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_text = read_clipboard_text()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="ClipboardSync"
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=0.8)

    def apply_remote(self, text) -> bool:
        if not valid_clipboard_text(text):
            log.warning("[CLIPBOARD] Invalid clipboard text received")
            return False
        with self._lock:
            try:
                current = read_clipboard_text()
                if current != text:
                    success = write_clipboard_text(text)
                    log.info("[CLIPBOARD] Applied remote text to local system (%d chars, success=%s)", len(text), success)
                    if not success:
                        if self._on_error is not None:
                            self._on_error("Could not write received text to the system clipboard.")
                        return False
                self._last_text = text
                return True
            except Exception as exc:
                log.warning("[CLIPBOARD] Exception applying remote clipboard text: %s", exc)
                return False

    def _run(self) -> None:
        while not self._stop.wait(CLIPBOARD_POLL_SECONDS):
            with self._lock:
                try:
                    text = read_clipboard_text()
                except Exception as exc:
                    log.debug("Clipboard poll read error: %s", exc)
                    continue
                if text is None or text == self._last_text:
                    continue
                # Keep the monitor lock while sending so a simultaneous remote
                # update cannot make an old clipboard value appear newest.
                try:
                    if self._send_text(text):
                        self._last_text = text
                    else:
                        self._last_text = text
                except Exception as exc:
                    log.warning("[CLIPBOARD] Error sending clipboard text: %s", exc)
