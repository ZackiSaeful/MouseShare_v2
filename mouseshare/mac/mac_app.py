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
        CGDisplayHideCursor,
        CGDisplayPixelsHigh,
        CGDisplayPixelsWide,
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
    from ..secure_transport import HandshakeError, client_handshake, opposite_edge
    from ..input_coalescer import OrderedInputQueue
    from ..clipboard_sync import ClipboardSynchronizer, valid_clipboard_text
except ImportError:  # Direct legacy execution: python mac_app.py
    from protocol import recv_message, send_message
    from mouseshare.secure_transport import HandshakeError, client_handshake, opposite_edge
    from mouseshare.input_coalescer import OrderedInputQueue
    from mouseshare.clipboard_sync import ClipboardSynchronizer, valid_clipboard_text


# ── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )


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
        message = "Connected to the Windows host."
        if clipboard_enabled:
            message += " Plain-text clipboard sync is active."
        else:
            message += " Clipboard text sharing is off; both devices must enable it."
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
        sw = int(CGDisplayPixelsWide(CGMainDisplayID()))
        sh = int(CGDisplayPixelsHigh(CGMainDisplayID()))
        extent = sh if mac_exit_edge in ("left", "right") else sw
        fraction = _last_edge_coord / max(1, extent - 1)
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
        sw = int(CGDisplayPixelsWide(CGMainDisplayID()))
        sh = int(CGDisplayPixelsHigh(CGMainDisplayID()))
        if mac_exit_edge in ("left", "right"):
            safe = max(0, min(_last_edge_coord, sh - 1))
            x = 5 if mac_exit_edge == "left" else sw - 6
            point = (x, safe)
        else:
            safe = max(0, min(_last_edge_coord, sw - 1))
            y = 5 if mac_exit_edge == "top" else sh - 6
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
    with _transition_lock:
        with STATE._lock:
            if STATE.dev != DevState.WINDOWS_ACTIVE:
                return

        mac_entry_edge = opposite_edge(windows_edge)
        log.info("[EDGE] Windows %s edge -- switching to Mac %s edge",
                 windows_edge, mac_entry_edge)

        # Step 1: update state (stops forwarding in event tap callback)
        STATE.set_mac_active()
        # Step 2: tell Windows to stop after every previously queued input
        # event has been delivered.
        if not _queue_remote_input({"type": "deactivate_windows"}):
            _on_conn_lost()
        # Warp to the Mac edge facing Windows, preserving the cross-edge position.
        sw = int(CGDisplayPixelsWide(CGMainDisplayID()))
        sh = int(CGDisplayPixelsHigh(CGMainDisplayID()))
        global _last_edge_coord
        if isinstance(position, (int, float)) and not isinstance(position, bool) \
                and 0.0 <= position <= 1.0:
            extent = sh if mac_entry_edge in ("left", "right") else sw
            _last_edge_coord = round(position * max(0, extent - 1))
        if mac_entry_edge in ("left", "right"):
            safe = max(0, min(_last_edge_coord, sh - 1))
            x = 5 if mac_entry_edge == "left" else sw - 6
            point = (x, safe)
        else:
            safe = max(0, min(_last_edge_coord, sw - 1))
            y = 5 if mac_entry_edge == "top" else sh - 6
            point = (safe, y)
        CGWarpMouseCursorPosition(point)
        log.debug("[CURSOR] Warped to Mac %s edge at %s", mac_entry_edge, point)
        # Step 4: re-attach physical mouse to cursor
        _associate_mouse()
        # Step 5: make cursor visible at its new left-edge position
        _show_cursor()


# ═══════════════════════════════════════════════════════════════════════════════
#  CGEventTap callback
# ═══════════════════════════════════════════════════════════════════════════════

def _handle_event(proxy, event_type: int, event, refcon):
    """CGEventTap callback.
    Return *event* to pass through, or *None* to suppress from Mac apps."""
    global _prev_flags, _last_edge_coord, _switching_to_win

    win = STATE.is_windows_active   # read once per event (atomic)

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
        return event      # MAC_ACTIVE: pass through normally

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
        return event

    # ── Mouse movement and button-drag ────────────────────────────────────────
    if event_type in (
        kCGEventMouseMoved,
        kCGEventLeftMouseDragged,
        kCGEventRightMouseDragged,
        kCGEventOtherMouseDragged,
    ):
        # Suppress if WINDOWS_ACTIVE **or** if we just detected the edge and
        # are about to switch (the async thread hasn't run yet).
        if win or _switching_to_win:
            if win:
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
        sw     = int(CGDisplayPixelsWide(CGMainDisplayID()))
        sh     = int(CGDisplayPixelsHigh(CGMainDisplayID()))
        thresh = int(CONFIG.get("edge_threshold", 3))
        mac_exit_edge = opposite_edge(CONFIG.get("mac_position", "left"))
        at_edge = {
            "left": pt.x <= thresh,
            "right": pt.x >= sw - thresh,
            "top": pt.y <= thresh,
            "bottom": pt.y >= sh - thresh,
        }[mac_exit_edge]
        if at_edge:
            _last_edge_coord = int(pt.y if mac_exit_edge in ("left", "right") else pt.x)
            _switching_to_win = True   # immediate suppression for next events
            log.info("[EDGE] Mac %s edge at x=%.0f y=%.0f -- triggering switch",
                     mac_exit_edge, pt.x, pt.y)
            # Spawn a new thread so we don't block the event tap run loop
            threading.Thread(target=switch_to_windows, daemon=True).start()
            return None   # suppress this edge-triggering event too
        return event

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
        return event

    # ── Scroll wheel ──────────────────────────────────────────────────────────
    if event_type == kCGEventScrollWheel:
        if win:
            dy = int(CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis1))
            dx = int(CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis2))
            if not _queue_remote_input({"type": "mouse_scroll", "dx": dx, "dy": dy}):
                threading.Thread(target=_on_conn_lost, daemon=True).start()
            return None
        return event

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

    # Ask macOS to present its native permission prompt when this app copy has
    # not yet been approved. Preflight again because the user may approve from
    # that prompt before the tap is created.
    try:
        post_access = bool(CGPreflightPostEventAccess())
        listen_access = bool(CGPreflightListenEventAccess())
        log.info("[PERMISSION] Accessibility/event posting=%s; Input Monitoring=%s",
                 post_access, listen_access)
        if not post_access:
            CGRequestPostEventAccess()
        if not listen_access:
            CGRequestListenEventAccess()
        post_access = bool(CGPreflightPostEventAccess())
        listen_access = bool(CGPreflightListenEventAccess())
    except Exception:
        log.exception("[PERMISSION] Could not query macOS input permissions")
        post_access = listen_access = None

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
                s, token, mac_position, clipboard_preference, send_message, recv_message)

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


def start_backend(config: dict, status_callback=None) -> None:
    """Start the macOS client backend without its legacy menu-bar UI."""
    global CONFIG, _status_callback, _backend_generation, _event_tap_thread, _network_thread
    CONFIG = dict(config)
    _status_callback = status_callback
    _backend_generation += 1
    generation = _backend_generation
    _quit_event.clear()
    _event_tap_ready.clear()
    _event_tap_failed.clear()
    _setup_logging(CONFIG.get("debug", False))
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
