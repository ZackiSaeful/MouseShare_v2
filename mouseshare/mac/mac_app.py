#!/usr/bin/env python3
"""
MouseShare -- Mac Agent
=======================
* Captures Mac keyboard + mouse via CGEventTap (kCGEventTapOptionDefault).
* MAC_ACTIVE  : input passes through to Mac apps normally.
* WINDOWS_ACTIVE: every event is forwarded to Windows and suppressed on Mac.
* The selected peer-facing edge triggers MAC -> WINDOWS transition.
* An authenticated Windows edge event returns control to the matching Mac edge.
* On disconnect: always restores mouse association and cursor visibility.
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import socket
import sys
import threading
import time
from enum import Enum, auto
from pathlib import Path

# ── Quartz / CoreFoundation ──────────────────────────────────────────────────
try:
    import Quartz
    from Quartz import (
        CGAssociateMouseAndMouseCursorPosition,
        CGDisplayBounds,
        CGDisplayHideCursor,
        CGDisplayShowCursor,
        CGEventGetFlags,
        CGEventGetIntegerValueField,
        CGEventGetLocation,
        CGEventMaskBit,
        CGEventTapCreate,
        CGEventTapEnable,
        CGPreflightListenEventAccess,
        CGPreflightPostEventAccess,
        CGRequestListenEventAccess,
        CGRequestPostEventAccess,
        CGMainDisplayID,
        CGWarpMouseCursorPosition,
        kCGEventFlagMaskAlphaShift,
        kCGEventFlagMaskAlternate,
        kCGEventFlagMaskCommand,
        kCGEventFlagMaskControl,
        kCGEventFlagMaskShift,
        kCGEventFlagsChanged,
        kCGEventKeyDown,
        kCGEventKeyUp,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseDragged,
        kCGEventLeftMouseUp,
        kCGEventMouseMoved,
        kCGEventOtherMouseDown,
        kCGEventOtherMouseDragged,
        kCGEventOtherMouseUp,
        kCGEventRightMouseDown,
        kCGEventRightMouseDragged,
        kCGEventRightMouseUp,
        kCGEventScrollWheel,
        kCGEventTapOptionDefault,
        kCGEventTapDisabledByTimeout,
        kCGEventTapDisabledByUserInput,
        kCGHeadInsertEventTap,
        kCGKeyboardEventKeycode,
        kCGMouseEventDeltaX,
        kCGMouseEventDeltaY,
        kCGScrollWheelEventDeltaAxis1,
        kCGScrollWheelEventDeltaAxis2,
        kCGSessionEventTap,
    )
    from CoreFoundation import (
        CFMachPortCreateRunLoopSource,
        CFRunLoopAddSource,
        CFRunLoopGetCurrent,
        CFRunLoopRun,
        CFRunLoopStop,
        kCFRunLoopCommonModes,
    )
except ImportError as _e:
    print(f"[FATAL] Cannot import Quartz/CoreFoundation: {_e}")
    print("        pip install pyobjc-framework-Quartz pyobjc-framework-Cocoa")
    sys.exit(1)

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError as _e:
    print(f"[FATAL] Cannot import pystray/Pillow: {_e}")
    sys.exit(1)

try:
    from .protocol import recv_message, send_message
    from ..secure_transport import HandshakeError, LegacyChannel, client_handshake, opposite_edge
    from ..input_coalescer import OrderedInputQueue
    from ..clipboard_sync import ClipboardSynchronizer, valid_clipboard_text
except ImportError:  # Direct legacy execution: python mac_app.py
    from protocol import recv_message, send_message
    from mouseshare.secure_transport import HandshakeError, LegacyChannel, client_handshake, opposite_edge
    from mouseshare.input_coalescer import OrderedInputQueue
    from mouseshare.clipboard_sync import ClipboardSynchronizer, valid_clipboard_text


# ── Logging ──────────────────────────────────────────────────────────────────

def diagnostic_log_path() -> Path:
    """Return a user-private log path for the packaged macOS app."""
    directory = Path.home() / "Library" / "Application Support" / "Mousehare"
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    except OSError:
        # Logging must never stop input restoration or shutdown.
        pass
    return directory / "Mousehare.log"


def _setup_logging(debug: bool) -> None:
    """Configure concise console logging and a rotating private diagnostic log."""
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    log_path = diagnostic_log_path()
    if not any(getattr(handler, "name", "") == "mousehare-diagnostic-log"
               for handler in root.handlers):
        try:
            handler = RotatingFileHandler(
                log_path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
            )
            handler.name = "mousehare-diagnostic-log"
            handler.setLevel(level)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            root.addHandler(handler)
        except OSError:
            pass


def _running_app_path() -> str:
    """Return the exact app bundle whose TCC permissions macOS is checking."""
    try:
        executable = Path(sys.executable).resolve()
        if getattr(sys, "frozen", False):
            for parent in executable.parents:
                if parent.suffix == ".app":
                    return str(parent)
        return str(executable)
    except OSError:
        return sys.executable


def input_permission_diagnostic(*, request_access: bool = False) -> dict[str, object]:
    """Read macOS input permissions for this exact process.

    ``request_access`` is intended for a direct UI action on the application's
    main thread. Event-tap threads must only read this state: requesting TCC
    access from a worker thread can leave macOS without a usable prompt.
    """
    report: dict[str, object] = {
        "app_path": _running_app_path(),
        "accessibility": None,
        "input_monitoring": None,
        "log_path": str(diagnostic_log_path()),
    }
    try:
        post_access = bool(CGPreflightPostEventAccess())
        listen_access = bool(CGPreflightListenEventAccess())
        if request_access:
            if not post_access:
                CGRequestPostEventAccess()
            if not listen_access:
                CGRequestListenEventAccess()
            post_access = bool(CGPreflightPostEventAccess())
            listen_access = bool(CGPreflightListenEventAccess())
        report["accessibility"] = post_access
        report["input_monitoring"] = listen_access
    except Exception as exc:
        report["error"] = str(exc)
    return report


log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  State machine
# ═══════════════════════════════════════════════════════════════════════════════

class ConnState(Enum):
    DISCONNECTED = auto()
    CONNECTED    = auto()


class DevState(Enum):
    MAC_ACTIVE     = auto()
    WINDOWS_ACTIVE = auto()


class AppState:
    """Thread-safe application state container."""

    def __init__(self) -> None:
        self._lock       = threading.Lock()
        self.conn        = ConnState.DISCONNECTED
        self.dev         = DevState.MAC_ACTIVE
        self._sock       = None          # active TCP socket
        self._channel    = None          # authenticated encrypted stream
        self._cursor_hid = False         # True while cursor is hidden
        self._dissoc     = False         # True while mouse is dissociated

    # ── Properties ────────────────────────────────────────────────────────────
    @property
    def is_windows_active(self) -> bool:
        with self._lock:
            return (self.conn == ConnState.CONNECTED and
                    self.dev  == DevState.WINDOWS_ACTIVE)

    @property
    def sock(self):
        with self._lock:
            return self._sock

    @property
    def channel(self):
        with self._lock:
            return self._channel

    # ── Transitions ───────────────────────────────────────────────────────────
    def set_connected(self, s, channel, clipboard_enabled: bool = False) -> None:
        with self._lock:
            self.conn  = ConnState.CONNECTED
            self.dev   = DevState.MAC_ACTIVE
            self._sock = s
            self._channel = channel
        log.info("[STATE] CONNECTED + MAC_ACTIVE")
        _update_tray()
        encrypted = getattr(channel, "is_encrypted", True)
        if not encrypted:
            message = ("Connected to the Windows host (legacy protocol, no encryption). "
                       "Update the Windows app for encrypted sessions and clipboard sharing.")
        elif clipboard_enabled:
            message = "Connected to the Windows host. Plain-text clipboard sync is active."
        else:
            message = "Connected to the Windows host. Clipboard text sharing is off; both devices must enable it."
        _notify_status("connected", message)

    def set_disconnected(self) -> None:
        with self._lock:
            self.conn  = ConnState.DISCONNECTED
            self.dev   = DevState.MAC_ACTIVE
            self._sock = None
            self._channel = None
        log.info("[STATE] DISCONNECTED")
        _update_tray()
        _notify_status("disconnected", "Disconnected.")

    def set_windows_active(self) -> None:
        with self._lock:
            self.dev = DevState.WINDOWS_ACTIVE
        log.info("[STATE] WINDOWS_ACTIVE")
        _update_tray()

    def set_mac_active(self) -> None:
        with self._lock:
            self.dev = DevState.MAC_ACTIVE
        log.info("[STATE] MAC_ACTIVE")
        _update_tray()

    def close_socket(self) -> None:
        with self._lock:
            s, self._sock = self._sock, None
            self._channel = None
        if s:
            try:
                s.close()
            except OSError:
                pass


STATE  = AppState()
CONFIG: dict = {}
_status_callback = None
_clipboard_sync_lock = threading.Lock()
_clipboard_sync: ClipboardSynchronizer | None = None
_input_queue: OrderedInputQueue | None = None


def _notify_status(status: str, message: str = "") -> None:
    callback = _status_callback
    if callback is not None:
        try:
            callback(status, message)
        except Exception:
            log.exception("[GUI] Status callback failed")


# ═══════════════════════════════════════════════════════════════════════════════
#  macOS keycode -> semantic key
# ═══════════════════════════════════════════════════════════════════════════════
#
#  kind="char"    -> printable character forwarded as-is
#  kind="special" -> named key (enter, shift, cmd, etc.)
#
KEYCODE_MAP: dict[int, tuple[str, str]] = {
    # Row 1 - letters
    0x00: ("char", "a"),  0x01: ("char", "s"),  0x02: ("char", "d"),
    0x03: ("char", "f"),  0x04: ("char", "h"),  0x05: ("char", "g"),
    0x06: ("char", "z"),  0x07: ("char", "x"),  0x08: ("char", "c"),
    0x09: ("char", "v"),  0x0B: ("char", "b"),  0x0C: ("char", "q"),
    0x0D: ("char", "w"),  0x0E: ("char", "e"),  0x0F: ("char", "r"),
    0x10: ("char", "y"),  0x11: ("char", "t"),  0x1F: ("char", "o"),
    0x20: ("char", "u"),  0x22: ("char", "i"),  0x23: ("char", "p"),
    0x25: ("char", "l"),  0x26: ("char", "j"),  0x28: ("char", "k"),
    0x2D: ("char", "n"),  0x2E: ("char", "m"),
    # Digits row
    0x12: ("char", "1"),  0x13: ("char", "2"),  0x14: ("char", "3"),
    0x15: ("char", "4"),  0x17: ("char", "5"),  0x16: ("char", "6"),
    0x1A: ("char", "7"),  0x1C: ("char", "8"),  0x19: ("char", "9"),
    0x1D: ("char", "0"),
    # Symbols
    0x18: ("char", "="),  0x1B: ("char", "-"),  0x1E: ("char", "]"),
    0x21: ("char", "["),  0x27: ("char", "'"),  0x29: ("char", ";"),
    0x2A: ("char", "\\"), 0x2B: ("char", ","),  0x2C: ("char", "/"),
    0x2F: ("char", "."),  0x32: ("char", "`"),
    # Special
    0x24: ("special", "enter"),
    0x30: ("special", "tab"),
    0x31: ("special", "space"),
    0x33: ("special", "backspace"),
    0x35: ("special", "escape"),
    0x75: ("special", "delete"),
    # Modifiers
    0x37: ("special", "cmd"),
    0x38: ("special", "shift"),
    0x39: ("special", "caps_lock"),
    0x3A: ("special", "alt"),
    0x3B: ("special", "ctrl"),
    0x3C: ("special", "shift"),    # right shift
    0x3D: ("special", "alt"),      # right option
    0x3E: ("special", "ctrl"),     # right control
    # Arrow keys
    0x7B: ("special", "left"),   0x7C: ("special", "right"),
    0x7D: ("special", "down"),   0x7E: ("special", "up"),
    # Navigation
    0x73: ("special", "home"),   0x77: ("special", "end"),
    0x74: ("special", "page_up"),0x79: ("special", "page_down"),
    0x72: ("special", "insert"),
    # Function keys
    0x7A: ("special", "f1"),  0x78: ("special", "f2"),
    0x63: ("special", "f3"),  0x76: ("special", "f4"),
    0x60: ("special", "f5"),  0x61: ("special", "f6"),
    0x62: ("special", "f7"),  0x64: ("special", "f8"),
    0x65: ("special", "f9"),  0x6D: ("special", "f10"),
    0x67: ("special", "f11"), 0x6F: ("special", "f12"),
    # Numpad
    0x52: ("char", "0"), 0x53: ("char", "1"), 0x54: ("char", "2"),
    0x55: ("char", "3"), 0x56: ("char", "4"), 0x57: ("char", "5"),
    0x58: ("char", "6"), 0x59: ("char", "7"), 0x5B: ("char", "8"),
    0x5C: ("char", "9"), 0x41: ("char", "."), 0x43: ("char", "*"),
    0x45: ("char", "+"), 0x4E: ("char", "-"), 0x4B: ("char", "/"),
    0x51: ("char", "="), 0x4C: ("special", "enter"),
    0x47: ("special", "num_lock"),
}

# Modifier flags -> semantic name (for kCGEventFlagsChanged handling)
FLAG_TO_MOD: dict[int, str] = {
    kCGEventFlagMaskShift:     "shift",
    kCGEventFlagMaskControl:   "ctrl",
    kCGEventFlagMaskAlternate: "alt",
    kCGEventFlagMaskCommand:   "cmd",
    kCGEventFlagMaskAlphaShift:"caps_lock",
}

_mod_lock      = threading.Lock()
_prev_flags    = 0     # last known CGEventFlags bitmask

# Coordinate along the selected screen edge, reused on the return handoff.
_last_edge_coord: int = 500

# Set to True as soon as the selected edge is detected, before transition work.
# This ensures the event tap immediately starts suppressing mouse events without
# waiting for the async thread to call _dissociate_mouse() / _hide_cursor().
_switching_to_win: bool = False
_switching_to_mac: bool = False
_last_mac_activation_time: float = 0.0


def _main_display_geometry() -> tuple[float, float, float, float]:
    """Return main display bounds in Quartz points, matching event coordinates."""
    bounds = CGDisplayBounds(CGMainDisplayID())
    return (float(bounds.origin.x), float(bounds.origin.y),
            float(bounds.size.width), float(bounds.size.height))


# ═══════════════════════════════════════════════════════════════════════════════
#  Cursor management
# ═══════════════════════════════════════════════════════════════════════════════

def _hide_cursor() -> None:
    if not STATE._cursor_hid:
        CGDisplayHideCursor(CGMainDisplayID())
        STATE._cursor_hid = True
        log.debug("[CURSOR] Hidden")


def _show_cursor() -> None:
    if STATE._cursor_hid:
        CGDisplayShowCursor(CGMainDisplayID())
        STATE._cursor_hid = False
        log.debug("[CURSOR] Shown")


def _dissociate_mouse() -> None:
    """Detach physical mouse movement from the on-screen cursor position."""
    if not STATE._dissoc:
        CGAssociateMouseAndMouseCursorPosition(False)
        STATE._dissoc = True
        log.debug("[CURSOR] Mouse dissociated from cursor")


def _associate_mouse() -> None:
    """Re-attach physical mouse movement to the on-screen cursor."""
    if STATE._dissoc:
        CGAssociateMouseAndMouseCursorPosition(True)
        STATE._dissoc = False
        log.debug("[CURSOR] Mouse re-associated with cursor")


def restore_mac_safe() -> None:
    """Always-safe cleanup: re-associate mouse and show cursor.
    Call on disconnect, exception, or quit to avoid stuck invisible cursor."""
    _associate_mouse()
    _show_cursor()


# ═══════════════════════════════════════════════════════════════════════════════
#  Tray / Menu-bar icon
# ═══════════════════════════════════════════════════════════════════════════════

def _make_icon(color: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([8, 8, 56, 56], fill=color)
    return img


_ICON_GRAY  = _make_icon("#808080")   # disconnected
_ICON_GREEN = _make_icon("#00CC44")   # connected, mac active
_ICON_BLUE  = _make_icon("#0088FF")   # connected, windows active

_tray_icon = None


def _update_tray() -> None:
    ic = _tray_icon
    if ic is None:
        return
    with STATE._lock:
        conn = STATE.conn
        dev  = STATE.dev
    if conn == ConnState.DISCONNECTED:
        ic.icon  = _ICON_GRAY
        ic.title = "MouseShare -- Disconnected"
    elif dev == DevState.MAC_ACTIVE:
        ic.icon  = _ICON_GREEN
        ic.title = "MouseShare -- Mac Active"
    else:
        ic.icon  = _ICON_BLUE
        ic.title = "MouseShare -- Windows Active"


# ═══════════════════════════════════════════════════════════════════════════════
#  Network helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _net_send(msg: dict) -> bool:
    with STATE._lock:
        s, channel = STATE._sock, STATE._channel
    if s is None or channel is None:
        return False
    return channel.send(s, msg, send_message)


def _deliver_queued_input(channel, msg: dict) -> bool:
    """Send one ordered input message only on its original secure session."""
    with STATE._lock:
        if STATE._channel is not channel:
            return False
        s = STATE._sock
    if s is None:
        return False
    return channel.send(s, msg, send_message)


def _queued_input_delivery_failed(channel) -> None:
    """Restore local control if an asynchronous input send loses its session."""
    with STATE._lock:
        if STATE._channel is not channel:
            return
        s = STATE._sock
    _on_conn_lost(s)


def _queue_remote_input(msg: dict) -> bool:
    """Queue input without blocking the CoreGraphics event-tap callback."""
    with STATE._lock:
        channel = STATE._channel
    queue = _input_queue
    if channel is None or queue is None:
        return False
    return queue.enqueue(channel, msg)


def _start_input_queue() -> None:
    global _input_queue
    if _input_queue is None:
        _input_queue = OrderedInputQueue(
            _deliver_queued_input,
            _queued_input_delivery_failed,
        )
    _input_queue.start()


def _stop_input_queue() -> None:
    global _input_queue
    if _input_queue is not None:
        _input_queue.stop()
        _input_queue = None


def _send_clipboard_text(text: str) -> bool:
    log.info("[CLIPBOARD] -> Sending %d chars to Windows: %r", len(text), text[:30])
    return _net_send({"type": "clipboard", "text": text})


def _start_clipboard_sync() -> None:
    global _clipboard_sync
    synchronizer = ClipboardSynchronizer(_send_clipboard_text)
    with _clipboard_sync_lock:
        previous, _clipboard_sync = _clipboard_sync, synchronizer
    if previous is not None:
        previous.stop()
    synchronizer.start()
    log.info("[CLIPBOARD] Plain-text sync started (48 KiB maximum)")


def _stop_clipboard_sync() -> None:
    global _clipboard_sync
    with _clipboard_sync_lock:
        synchronizer, _clipboard_sync = _clipboard_sync, None
    if synchronizer is not None:
        synchronizer.stop()
        log.info("[CLIPBOARD] Sync stopped")


def _apply_remote_clipboard(msg: dict) -> None:
    text = msg.get("text")
    if not valid_clipboard_text(text):
        log.warning("[CLIPBOARD] Ignoring invalid or oversized clipboard message")
        return
    log.info("[CLIPBOARD] <- Received %d chars from Windows: %r", len(text), text[:30])
    with _clipboard_sync_lock:
        synchronizer = _clipboard_sync
    if synchronizer is not None:
        synchronizer.apply_remote(text)


# ═══════════════════════════════════════════════════════════════════════════════
#  State transitions
# ═══════════════════════════════════════════════════════════════════════════════

_transition_lock = threading.Lock()


def switch_to_windows() -> None:
    """Atomic transition from the selected Mac edge to Windows."""
    with _transition_lock:
        with STATE._lock:
            if STATE.dev  != DevState.MAC_ACTIVE:
                return
            if STATE.conn != ConnState.CONNECTED:
                return

        mac_exit_edge = opposite_edge(CONFIG.get("mac_position", "left"))
        log.info("[EDGE] Mac %s edge -- switching to Windows", mac_exit_edge)

        # Step 1: queue activation before accepting movement input.  The same
        # ordered worker forwards later deltas, so Windows always activates
        # before it receives its first mouse movement.
        left, top, width, height = _main_display_geometry()
        origin = top if mac_exit_edge in ("left", "right") else left
        extent = height if mac_exit_edge in ("left", "right") else width
        fraction = (_last_edge_coord - origin) / max(1, extent - 1)
        if not _queue_remote_input({
            "type": "activate_windows",
            "position": max(0.0, min(1.0, fraction)),
        }):
            _on_conn_lost()
            return
        STATE.set_windows_active()
        # Step 2: reset the early-suppression flag (state is now authoritative)
        global _switching_to_win
        _switching_to_win = False
        # Step 3: park the cursor on the selected edge before entering
        # delta-only mode.
        left, top, width, height = _main_display_geometry()
        if mac_exit_edge in ("left", "right"):
            safe = max(int(top), min(int(_last_edge_coord), int(top + height - 1)))
            x = left + 5 if mac_exit_edge == "left" else left + width - 6
            point = (x, safe)
        else:
            safe = max(int(left), min(int(_last_edge_coord), int(left + width - 1)))
            y = top + 5 if mac_exit_edge == "top" else top + height - 6
            point = (safe, y)
        CGWarpMouseCursorPosition(point)
        # Step 5: detach physical mouse from cursor (delta-only mode)
        _dissociate_mouse()
        # Step 6: hide cursor so it does not linger visibly on screen
        _hide_cursor()
        # Forwarding is now handled automatically by the event tap callback


def switch_to_mac(windows_edge: str, position: float | None = None) -> None:
    """Atomic transition: WINDOWS_ACTIVE -> MAC_ACTIVE.
    Triggered when Windows reports its selected edge."""
    global _switching_to_mac, _last_mac_activation_time
    with _transition_lock:
        with STATE._lock:
            if STATE.dev != DevState.WINDOWS_ACTIVE:
                return

        _switching_to_mac = True
        try:
            _last_mac_activation_time = time.monotonic()
            mac_entry_edge = opposite_edge(windows_edge)
            log.info("[EDGE] Windows %s edge -- switching to Mac %s edge",
                     windows_edge, mac_entry_edge)

            # Keep local events suppressed until its cursor has been restored.
            if not _queue_remote_input({"type": "deactivate_windows"}):
                _on_conn_lost()
                return

            # Warp to the matching edge and preserve the relative coordinate.
            left, top, width, height = _main_display_geometry()
            if isinstance(position, (int, float)) and not isinstance(position, bool) \
                    and 0.0 <= position <= 1.0:
                extent = height if mac_entry_edge in ("left", "right") else width
                origin = top if mac_entry_edge in ("left", "right") else left
                _last_edge_coord = round(origin + position * max(0, extent - 1))
            if mac_entry_edge in ("left", "right"):
                safe = max(int(top), min(int(_last_edge_coord), int(top + height - 1)))
                x = left + 14 if mac_entry_edge == "left" else left + width - 15
                point = (x, safe)
            else:
                safe = max(int(left), min(int(_last_edge_coord), int(left + width - 1)))
                y = top + 14 if mac_entry_edge == "top" else top + height - 15
                point = (safe, y)
            CGWarpMouseCursorPosition(point)
            log.debug("[CURSOR] Warped to Mac %s edge at %s", mac_entry_edge, point)
            _associate_mouse()
            _show_cursor()
            STATE.set_mac_active()
        except Exception:
            log.exception("[EDGE] Could not complete Windows-to-Mac handoff")
            restore_mac_safe()
            STATE.set_mac_active()
        finally:
            _switching_to_mac = False


# ═══════════════════════════════════════════════════════════════════════════════
#  CGEventTap callback
# ═══════════════════════════════════════════════════════════════════════════════

def _handle_event(proxy, event_type: int, event, refcon):
    """CGEventTap callback.
    Return *event* to pass through, or *None* to suppress from Mac apps."""
    global _prev_flags, _last_edge_coord, _switching_to_win

    if event_type in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
        # macOS can disable a tap after a timeout. Re-enable it immediately so
        # captured input does not start leaking to local applications.
        if _event_tap_ref is not None:
            log.warning("[EVENT TAP] macOS disabled the tap (type=%s); re-enabling", event_type)
            CGEventTapEnable(_event_tap_ref, True)
        return event

    win = STATE.is_windows_active   # read once per event (atomic)
    switching = _switching_to_win or _switching_to_mac
    if _switching_to_mac:
        # Do not enqueue events after deactivate_windows in the ordered queue.
        return None

    # ── Key down / key up ─────────────────────────────────────────────────────
    if event_type in (kCGEventKeyDown, kCGEventKeyUp):
        kc     = int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode))
        action = "press" if event_type == kCGEventKeyDown else "release"
        kind, value = KEYCODE_MAP.get(kc, ("char", f"kc{kc:02X}"))

        if win:
            log.debug("[KEY] %s %s -> Windows", value, action)
            ok = _queue_remote_input({"type": "key", "action": action,
                                      "key": {"kind": kind, "value": value}})
            if not ok:
                threading.Thread(target=_on_conn_lost, daemon=True).start()
            return None   # suppress: key must NOT reach Mac apps
        return None if switching else event

    # ── Modifier flags changed ─────────────────────────────────────────────────
    if event_type == kCGEventFlagsChanged:
        flags = int(CGEventGetFlags(event))
        with _mod_lock:
            prev, _prev_flags = _prev_flags, flags

        if win:
            for flag, mod_name in FLAG_TO_MOD.items():
                was = bool(prev  & flag)
                now = bool(flags & flag)
                if was != now:
                    act = "press" if now else "release"
                    log.debug("[KEY] %s %s -> Windows", mod_name, act)
                    if not _queue_remote_input({
                        "type": "key",
                        "action": act,
                        "key": {"kind": "special", "value": mod_name},
                    }):
                        threading.Thread(target=_on_conn_lost, daemon=True).start()
            return None   # suppress modifier events from Mac
        return None if switching else event

    # ── Mouse movement and button-drag ────────────────────────────────────────
    if event_type in (
        kCGEventMouseMoved,
        kCGEventLeftMouseDragged,
        kCGEventRightMouseDragged,
        kCGEventOtherMouseDragged,
    ):
        # Suppress if WINDOWS_ACTIVE **or** if we just detected the edge and
        # are about to switch (the async thread hasn't run yet).
        if win or switching:
            if win and not _switching_to_mac:
                # Use delta fields (cursor is dissociated, absolute pos is frozen)
                dx = int(CGEventGetIntegerValueField(event, kCGMouseEventDeltaX))
                dy = int(CGEventGetIntegerValueField(event, kCGMouseEventDeltaY))
                if dx or dy:
                    log.debug("[MOUSE] dx=%d dy=%d -> Windows", dx, dy)
                    if not _queue_remote_input({"type": "mouse_move", "dx": dx, "dy": dy}):
                        threading.Thread(target=_on_conn_lost, daemon=True).start()
            return None   # suppress: cursor must not move on Mac

        # MAC_ACTIVE: detect the edge facing the Windows computer.
        pt     = CGEventGetLocation(event)
        left, top, width, height = _main_display_geometry()
        thresh = int(CONFIG.get("edge_threshold", 3))
        mac_exit_edge = opposite_edge(CONFIG.get("mac_position", "left"))
        at_edge = {
            "left": pt.x <= left + thresh,
            "right": pt.x >= left + width - thresh,
            "top": pt.y <= top + thresh,
            "bottom": pt.y >= top + height - thresh,
        }[mac_exit_edge]
        if at_edge and (time.monotonic() - _last_mac_activation_time >= 0.35):
            _last_edge_coord = int(pt.y if mac_exit_edge in ("left", "right") else pt.x)
            _switching_to_win = True   # immediate suppression for next events
            log.info("[EDGE] Mac %s edge at x=%.0f y=%.0f -- triggering switch",
                     mac_exit_edge, pt.x, pt.y)
            # Spawn a new thread so we don't block the event tap run loop
            threading.Thread(target=switch_to_windows, daemon=True).start()
            return None   # suppress this edge-triggering event too
        return None if switching else event

    # ── Mouse buttons ─────────────────────────────────────────────────────────
    _BTN_MAP = {
        kCGEventLeftMouseDown:  ("left",   True),
        kCGEventLeftMouseUp:    ("left",   False),
        kCGEventRightMouseDown: ("right",  True),
        kCGEventRightMouseUp:   ("right",  False),
        kCGEventOtherMouseDown: ("middle", True),
        kCGEventOtherMouseUp:   ("middle", False),
    }
    if event_type in _BTN_MAP:
        btn, pressed = _BTN_MAP[event_type]
        if win:
            if not _queue_remote_input({
                "type": "mouse_click", "button": btn, "pressed": pressed,
            }):
                threading.Thread(target=_on_conn_lost, daemon=True).start()
            return None
        return None if switching else event

    # ── Scroll wheel ──────────────────────────────────────────────────────────
    if event_type == kCGEventScrollWheel:
        if win:
            dy = int(CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis1))
            dx = int(CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis2))
            if not _queue_remote_input({"type": "mouse_scroll", "dx": dx, "dy": dy}):
                threading.Thread(target=_on_conn_lost, daemon=True).start()
            return None
        return None if switching else event

    return event


# ═══════════════════════════════════════════════════════════════════════════════
#  Event tap -- runs on its own thread with its own CFRunLoop
# ═══════════════════════════════════════════════════════════════════════════════

_run_loop_ref  = None
_event_tap_ref = None
_event_tap_ready = threading.Event()
_event_tap_failed = threading.Event()
_event_tap_thread = None
_network_thread = None
_backend_generation = 0


def _run_event_tap(generation: int | None = None) -> None:
    global _run_loop_ref, _event_tap_ref

    # Permission prompts are requested by start_backend on the UI thread.
    # This worker thread only observes the result before installing the tap.
    permission_report = input_permission_diagnostic()
    post_access = permission_report["accessibility"]
    listen_access = permission_report["input_monitoring"]
    log.info("[PERMISSION] Accessibility/event posting=%s; Input Monitoring=%s",
             post_access, listen_access)

    event_mask = (
        CGEventMaskBit(kCGEventKeyDown)            |
        CGEventMaskBit(kCGEventKeyUp)              |
        CGEventMaskBit(kCGEventFlagsChanged)       |
        CGEventMaskBit(kCGEventMouseMoved)         |
        CGEventMaskBit(kCGEventLeftMouseDown)      |
        CGEventMaskBit(kCGEventLeftMouseUp)        |
        CGEventMaskBit(kCGEventRightMouseDown)     |
        CGEventMaskBit(kCGEventRightMouseUp)       |
        CGEventMaskBit(kCGEventScrollWheel)        |
        CGEventMaskBit(kCGEventOtherMouseDown)     |
        CGEventMaskBit(kCGEventOtherMouseUp)       |
        CGEventMaskBit(kCGEventLeftMouseDragged)   |
        CGEventMaskBit(kCGEventRightMouseDragged)  |
        CGEventMaskBit(kCGEventOtherMouseDragged)
    )

    tap = CGEventTapCreate(
        kCGSessionEventTap,
        kCGHeadInsertEventTap,
        kCGEventTapOptionDefault,   # NOT ListenOnly -- we need to suppress events
        event_mask,
        _handle_event,
        None,
    )

    if tap is None:
        if generation is not None and generation != _backend_generation:
            return
        log.error("=" * 62)
        log.error("[FATAL] CGEventTapCreate returned None.")
        log.error("  Permission check: Accessibility/event posting=%s; Input Monitoring=%s",
                  post_access, listen_access)
        app_path = _running_app_path()
        log.error("=" * 62)
        _event_tap_failed.set()
        if post_access is False or listen_access is False:
            missing = []
            if post_access is False:
                missing.append("Accessibility")
            if listen_access is False:
                missing.append("Input Monitoring")
            message = (
                f"macOS denies { ' and '.join(missing) }. Running copy: {app_path}. "
                "Enable this exact copy under System Settings → Privacy & Security → "
                "Accessibility and Input Monitoring, then retry. Ad-hoc builds need "
                "approval again after each rebuild."
            )
        elif post_access is True and listen_access is True:
            message = (
                "macOS reports both input permissions enabled, but refused Mousehare's event tap. "
                f"Running copy: {app_path}. Quit duplicate copies, remove stale Mousehare entries "
                "from Accessibility and Input Monitoring, add this exact copy, and retry."
            )
        else:
            message = (
                "macOS refused Mousehare's input event tap and its permission status could not be read. "
                f"Running copy: {app_path}. Check Accessibility and Input Monitoring for this exact copy."
            )
        _notify_status("error", message)
        return

    if generation is not None and generation != _backend_generation:
        CGEventTapEnable(tap, False)
        return

    _event_tap_ref = tap
    source = CFMachPortCreateRunLoopSource(None, tap, 0)
    loop   = CFRunLoopGetCurrent()
    _run_loop_ref  = loop

    CFRunLoopAddSource(loop, source, kCFRunLoopCommonModes)
    CGEventTapEnable(tap, True)
    if generation is None or generation == _backend_generation:
        _event_tap_ready.set()
    log.info("[EVENT TAP] Started -- intercepting keyboard + mouse")
    CFRunLoopRun()    # blocks this thread until CFRunLoopStop() is called
    log.info("[EVENT TAP] Stopped")
    if generation is None or generation == _backend_generation:
        _run_loop_ref = None
        _event_tap_ref = None


def _stop_event_tap() -> None:
    if _event_tap_ref is not None:
        CGEventTapEnable(_event_tap_ref, False)
    if _run_loop_ref is not None:
        CFRunLoopStop(_run_loop_ref)


# ═══════════════════════════════════════════════════════════════════════════════
#  Network -- receive loop
# ═══════════════════════════════════════════════════════════════════════════════

_quit_event = threading.Event()


def _on_conn_lost(expected_sock=None) -> None:
    """Safe-fallback for unexpected TCP disconnect."""
    if expected_sock is not None and STATE.sock is not expected_sock:
        return
    with STATE._lock:
        channel = STATE._channel
    if channel is not None and _input_queue is not None:
        _input_queue.discard_session(channel)
    global _switching_to_win
    _switching_to_win = False
    log.info("[NETWORK] Connection lost")
    log.info("[STATE]   Resetting to MAC_ACTIVE (safe fallback)")
    restore_mac_safe()     # show cursor, re-associate mouse -- always safe
    _stop_clipboard_sync()
    STATE.close_socket()
    STATE.set_disconnected()
    _notify_status("connecting", "Connection lost. Reconnecting…")


def _recv_loop(sock, channel) -> None:
    """Daemon thread: reads framed messages from Windows until connection closes."""
    log.info("[NETWORK] Receive loop started")
    while not _quit_event.is_set():
        msg = channel.receive(sock, recv_message)
        if msg is None:
            _on_conn_lost(sock)
            return
        try:
            _handle_incoming(msg)
        except Exception as exc:
            # One bad message must NOT kill the connection
            log.error("[NETWORK] Error handling message: %s", exc)
    log.info("[NETWORK] Receive loop ended")


def _handle_incoming(msg: dict) -> None:
    msg_type = msg.get("type")
    log.debug("[NETWORK] <- %s", msg_type)

    if msg_type == "edge":
        edge = msg.get("edge")
        if edge in ("left", "right", "top", "bottom"):
            log.info("[EDGE] Received edge=%s from Windows", edge)
            if STATE.is_windows_active and edge == CONFIG.get("mac_position", "left"):
                switch_to_mac(edge, msg.get("position"))
            else:
                log.debug("[EDGE] Ignored (unexpected edge or not WINDOWS_ACTIVE)")

    elif msg_type == "clipboard":
        _apply_remote_clipboard(msg)

    elif msg_type == "activation_ack":
        active = msg.get("active", False)
        positioned = msg.get("cursor_positioned", False)
        edge = msg.get("edge", "")
        pos = msg.get("position", 0.0)
        log.debug("[STATE] Windows activation ACK: active=%s, cursor_positioned=%s at %s (%s)",
                  active, positioned, edge, pos)

    elif msg_type == "hello_error":
        log.error("[NETWORK] The Windows server rejected pairing.")
        _stop_clipboard_sync()
        with STATE._lock:
            channel = STATE._channel
        if channel is not None and _input_queue is not None:
            _input_queue.discard_session(channel)
        STATE.close_socket()
        STATE.set_disconnected()

    elif msg_type == "hello_ok":
        pass   # handled inline in _connect_loop

    else:
        log.debug("[NETWORK] Unknown message type: %s", msg_type)


# ═══════════════════════════════════════════════════════════════════════════════
#  Network -- connect / reconnect loop
# ═══════════════════════════════════════════════════════════════════════════════

def _connect_loop(generation: int | None = None) -> None:
    """Daemon thread: endlessly tries to connect to the Windows agent."""
    host  = CONFIG["windows_host"]
    port  = CONFIG["port"]
    token = CONFIG["token"]
    mac_position = CONFIG.get("mac_position", "left")

    if generation is None:
        generation = _backend_generation

    def still_current() -> bool:
        return generation == _backend_generation and not _quit_event.is_set()

    while still_current() and not _event_tap_ready.is_set():
        if _event_tap_failed.is_set():
            return
        _event_tap_ready.wait(0.1)

    while still_current():
        log.info("[NETWORK] Connecting to %s:%d ...", host, port)
        _notify_status("connecting", f"Connecting to {host}:{port}…")
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(5.0)
            s.connect((host, port))
            if not still_current():
                s.close()
                return
            log.info("[NETWORK] Connected")

            # Pairing-key authentication and ephemeral encrypted session setup.
            clipboard_preference = CONFIG.get("clipboard_sync", False)
            channel, clipboard_enabled = client_handshake(
                s, token, mac_position, clipboard_preference, send_message, recv_message,
                allow_legacy_protocol=bool(CONFIG.get("allow_legacy_protocol", False)),
            )

            if not still_current():
                s.close()
                return

            s.settimeout(None)   # blocking reads after a bounded handshake

            log.info("[NETWORK] Handshake OK")
            STATE.set_connected(s, channel, clipboard_enabled)
            if clipboard_enabled:
                _start_clipboard_sync()

            # Run the receive loop and wait until it exits (connection lost)
            recv_t = threading.Thread(
                target=_recv_loop, args=(s, channel), daemon=True, name="RecvLoop"
            )
            recv_t.start()
            recv_t.join()   # blocks until connection is lost

        except HandshakeError as exc:
            log.error("[NETWORK] Secure pairing failed: %s", exc)
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
            if still_current():
                _notify_status("error", str(exc))
        except (OSError, ConnectionRefusedError, TimeoutError) as exc:
            log.info("[NETWORK] Cannot connect: %s", exc)
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
            if still_current():
                _notify_status("error", f"Could not connect to {host}:{port}: {exc}")

        if still_current():
            log.info("[NETWORK] Reconnecting in 3 s...")
            _quit_event.wait(3.0)


# ═══════════════════════════════════════════════════════════════════════════════
#  Quit / cleanup
# ═══════════════════════════════════════════════════════════════════════════════

def _do_quit(icon=None, _item=None) -> None:
    log.info("[SHUTDOWN] Quitting MouseShare Mac Agent...")
    _quit_event.set()
    _stop_event_tap()
    restore_mac_safe()   # cursor MUST be shown/associated before exit
    _stop_clipboard_sync()
    _stop_input_queue()
    STATE.close_socket()
    if icon is not None:
        icon.stop()
    log.info("[SHUTDOWN] Done -- goodbye!")


def start_backend(config: dict, status_callback=None) -> bool:
    """Start the macOS client backend without its legacy menu-bar UI.

    Returns ``False`` when macOS has not authorised this *installed app copy*.
    The caller must not report a live backend in that case: no event tap can be
    installed, so opening a TCP connection would only look successful while
    mouse and keyboard sharing cannot work.
    """
    global CONFIG, _status_callback, _backend_generation, _event_tap_thread, _network_thread
    CONFIG = dict(config)
    _status_callback = status_callback
    _backend_generation += 1
    generation = _backend_generation
    _quit_event.clear()
    _event_tap_ready.clear()
    _event_tap_failed.clear()
    _setup_logging(CONFIG.get("debug", False))
    permission_report = input_permission_diagnostic(request_access=True)
    post_access = permission_report["accessibility"]
    listen_access = permission_report["input_monitoring"]
    if post_access is not True or listen_access is not True:
        missing = []
        if post_access is not True:
            missing.append("Accessibility")
        if listen_access is not True:
            missing.append("Input Monitoring")
        _notify_status(
            "error",
            f"macOS has not granted {' and '.join(missing)} to this copy: "
            f"{permission_report['app_path']}. Approve it in Privacy & Security, "
            "quit Mousehare, then reopen it. "
            f"Diagnostic log: {permission_report['log_path']}",
        )
        return False
    _start_input_queue()
    _notify_status("connecting", "Checking macOS input permissions…")
    _event_tap_thread = threading.Thread(
        target=_run_event_tap_guarded, args=(generation,), daemon=True, name="EventTap"
    )
    _event_tap_thread.start()
    _network_thread = threading.Thread(
        target=_connect_loop, args=(generation,), daemon=True, name="Network"
    )
    _network_thread.start()
    return True


def stop_backend() -> None:
    """Stop network activity and restore the local mouse before returning."""
    global _backend_generation
    _backend_generation += 1
    _quit_event.set()
    _stop_event_tap()
    if _event_tap_thread is not None and _event_tap_thread is not threading.current_thread():
        _event_tap_thread.join(timeout=1.0)
    restore_mac_safe()
    _stop_clipboard_sync()
    _stop_input_queue()
    STATE.close_socket()
    STATE.set_disconnected()


def _run_event_tap_guarded(generation: int | None = None) -> None:
    if generation is not None and generation != _backend_generation:
        return
    try:
        _run_event_tap(generation)
    except Exception as exc:
        log.exception("[EVENT TAP] Startup failed")
        _event_tap_failed.set()
        _notify_status("error", f"Could not start macOS input monitoring: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    global CONFIG, _tray_icon

    cfg_path = Path(__file__).parent / "config.json"
    if not cfg_path.exists():
        print(f"[FATAL] config.json not found at {cfg_path}")
        sys.exit(1)
    with cfg_path.open() as fh:
        CONFIG = json.load(fh)

    _setup_logging(CONFIG.get("debug", False))
    log.info("MouseShare -- Mac Agent")
    log.info("Windows host   : %s:%d", CONFIG["windows_host"], CONFIG["port"])
    log.info("Edge threshold : %d px", CONFIG.get("edge_threshold", 3))

    start_backend(CONFIG)

    # pystray menu-bar icon MUST run on the main thread on macOS
    _tray_icon = pystray.Icon(
        "MouseShare",
        _ICON_GRAY,
        "MouseShare -- Disconnected",
        menu=pystray.Menu(
            pystray.MenuItem("MouseShare", lambda i, it: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", _do_quit),
        ),
    )
    _update_tray()
    log.info("[TRAY] Menu-bar icon running on main thread")
    _tray_icon.run()   # blocks until icon.stop() is called from _do_quit()


if __name__ == "__main__":
    main()
