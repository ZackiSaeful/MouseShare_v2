"""macOS input-provider backend for Mousehare.

The backend captures local input only while Windows owns control.  Its state
machine is deliberately small: disconnected, connected with Mac active, and
connected with Windows active.  Every exit path restores the local cursor.
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import socket
import sys
import threading
import time
from pathlib import Path

try:
    from Quartz import (
        CGAssociateMouseAndMouseCursorPosition, CGDisplayBounds, CGDisplayHideCursor,
        CGDisplayShowCursor, CGEventGetFlags, CGEventGetIntegerValueField,
        CGEventGetLocation, CGEventMaskBit, CGEventTapCreate, CGEventTapEnable,
        CGMainDisplayID, CGPreflightListenEventAccess, CGPreflightPostEventAccess,
        CGRequestListenEventAccess, CGRequestPostEventAccess, CGWarpMouseCursorPosition,
        kCGEventFlagMaskAlphaShift, kCGEventFlagMaskAlternate, kCGEventFlagMaskCommand,
        kCGEventFlagMaskControl, kCGEventFlagMaskShift, kCGEventFlagsChanged,
        kCGEventKeyDown, kCGEventKeyUp, kCGEventLeftMouseDown, kCGEventLeftMouseDragged,
        kCGEventLeftMouseUp, kCGEventMouseMoved, kCGEventOtherMouseDown,
        kCGEventOtherMouseDragged, kCGEventOtherMouseUp, kCGEventRightMouseDown,
        kCGEventRightMouseDragged, kCGEventRightMouseUp, kCGEventScrollWheel,
        kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput,
        kCGEventTapOptionDefault, kCGHeadInsertEventTap, kCGKeyboardEventKeycode,
        kCGMouseEventDeltaX, kCGMouseEventDeltaY, kCGScrollWheelEventDeltaAxis1,
        kCGScrollWheelEventDeltaAxis2, kCGSessionEventTap,
    )
    from CoreFoundation import (CFMachPortCreateRunLoopSource, CFRunLoopAddSource,
                                CFRunLoopGetCurrent, CFRunLoopRun, CFRunLoopStop,
                                kCFRunLoopCommonModes)
    QUARTZ_AVAILABLE = True
    QUARTZ_ERROR = ""
except ImportError as exc:  # enables protocol tests on Windows
    QUARTZ_AVAILABLE = False
    QUARTZ_ERROR = str(exc)

try:
    from .protocol import recv_message, send_message
    from ..clipboard_sync import ClipboardSynchronizer, valid_clipboard_text
    from ..input_coalescer import OrderedInputQueue
    from ..secure_transport import HandshakeError, client_handshake, opposite_edge
except ImportError:  # direct contributor execution
    from protocol import recv_message, send_message
    from mouseshare.clipboard_sync import ClipboardSynchronizer, valid_clipboard_text
    from mouseshare.input_coalescer import OrderedInputQueue
    from mouseshare.secure_transport import HandshakeError, client_handshake, opposite_edge


log = logging.getLogger(__name__)
CONFIG: dict = {}
_status_callback = None
_backend: "MacBackend | None" = None
_VALID_EDGES = frozenset(("left", "right", "top", "bottom"))


def diagnostic_log_path() -> Path:
    path = Path.home() / "Library" / "Application Support" / "Mousehare"
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    except OSError:
        pass
    return path / "Mousehare.log"


def _setup_logging(debug: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    if not root.handlers:
        logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not any(getattr(item, "name", "") == "mousehare-log" for item in root.handlers):
        try:
            handler = RotatingFileHandler(diagnostic_log_path(), maxBytes=512 * 1024,
                                          backupCount=2, encoding="utf-8")
            handler.name = "mousehare-log"
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            root.addHandler(handler)
        except OSError:
            pass


def _running_app_path() -> str:
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        for parent in executable.parents:
            if parent.suffix == ".app":
                return str(parent)
    return str(executable)


def input_permission_diagnostic(*, request_access: bool = False) -> dict[str, object]:
    """Read TCC state for the exact bundle that owns this running process."""
    report: dict[str, object] = {"app_path": _running_app_path(), "log_path": str(diagnostic_log_path()),
                                 "accessibility": None, "input_monitoring": None}
    if not QUARTZ_AVAILABLE:
        report["error"] = QUARTZ_ERROR
        return report
    try:
        accessibility = bool(CGPreflightPostEventAccess())
        monitoring = bool(CGPreflightListenEventAccess())
        if request_access:
            if not accessibility:
                CGRequestPostEventAccess()
            if not monitoring:
                CGRequestListenEventAccess()
            accessibility = bool(CGPreflightPostEventAccess())
            monitoring = bool(CGPreflightListenEventAccess())
        report["accessibility"] = accessibility
        report["input_monitoring"] = monitoring
    except Exception as exc:
        report["error"] = str(exc)
    return report


def _notify_status(status: str, message: str) -> None:
    if _status_callback is not None:
        try:
            _status_callback(status, message)
        except Exception:
            log.exception("Status callback failed")


# Mac virtual keycode to a portable semantic key. Command becomes Ctrl on Windows.
KEYS = {
    0x00: ("char", "a"), 0x01: ("char", "s"), 0x02: ("char", "d"), 0x03: ("char", "f"),
    0x04: ("char", "h"), 0x05: ("char", "g"), 0x06: ("char", "z"), 0x07: ("char", "x"),
    0x08: ("char", "c"), 0x09: ("char", "v"), 0x0B: ("char", "b"), 0x0C: ("char", "q"),
    0x0D: ("char", "w"), 0x0E: ("char", "e"), 0x0F: ("char", "r"), 0x10: ("char", "y"),
    0x11: ("char", "t"), 0x12: ("char", "1"), 0x13: ("char", "2"), 0x14: ("char", "3"),
    0x15: ("char", "4"), 0x16: ("char", "6"), 0x17: ("char", "5"), 0x18: ("char", "="),
    0x19: ("char", "9"), 0x1A: ("char", "7"), 0x1B: ("char", "-"), 0x1C: ("char", "8"),
    0x1D: ("char", "0"), 0x1E: ("char", "]"), 0x1F: ("char", "o"), 0x20: ("char", "u"),
    0x21: ("char", "["), 0x22: ("char", "i"), 0x23: ("char", "p"), 0x24: ("special", "enter"),
    0x25: ("char", "l"), 0x26: ("char", "j"), 0x27: ("char", "'"), 0x28: ("char", "k"),
    0x29: ("char", ";"), 0x2A: ("char", "\\"), 0x2B: ("char", ","), 0x2C: ("char", "/"),
    0x2D: ("char", "n"), 0x2E: ("char", "m"), 0x2F: ("char", "."), 0x30: ("special", "tab"),
    0x31: ("special", "space"), 0x33: ("special", "backspace"), 0x35: ("special", "escape"),
    0x37: ("special", "cmd"), 0x38: ("special", "shift"), 0x39: ("special", "caps_lock"),
    0x3A: ("special", "alt"), 0x3B: ("special", "ctrl"), 0x3C: ("special", "shift"),
    0x3D: ("special", "alt"), 0x3E: ("special", "ctrl"), 0x73: ("special", "home"),
    0x74: ("special", "page_up"), 0x75: ("special", "delete"), 0x77: ("special", "end"),
    0x78: ("special", "f2"), 0x79: ("special", "page_down"), 0x7A: ("special", "f1"),
    0x7B: ("special", "left"), 0x7C: ("special", "right"), 0x7D: ("special", "down"),
    0x7E: ("special", "up"), 0x60: ("special", "f5"), 0x61: ("special", "f6"),
    0x62: ("special", "f7"), 0x63: ("special", "f3"), 0x64: ("special", "f8"),
    0x65: ("special", "f9"), 0x67: ("special", "f11"), 0x6D: ("special", "f10"),
    0x6F: ("special", "f12"),
}


class MacBackend:
    def __init__(self, config: dict):
        self.config = dict(config)
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.sock: socket.socket | None = None
        self.channel = None
        self.connected = False
        self.remote_active = False
        self.returning = False
        self.cursor_hidden = False
        self.mouse_disassociated = False
        self.event_tap = None
        self.run_loop = None
        self.tap_ready = threading.Event()
        self.tap_failed = threading.Event()
        self.modifier_flags = 0
        self.last_return = 0.0
        self.queue = OrderedInputQueue(self._deliver, self._queue_failed)
        self.clipboard: ClipboardSynchronizer | None = None

    def start(self) -> bool:
        if not QUARTZ_AVAILABLE:
            _notify_status("error", f"Quartz is unavailable: {QUARTZ_ERROR}")
            return False
        report = input_permission_diagnostic(request_access=True)
        missing = [name for name, key in (("Accessibility", "accessibility"),
                                         ("Input Monitoring", "input_monitoring")) if report.get(key) is not True]
        if missing:
            _notify_status("error", f"macOS has not granted {' and '.join(missing)} to {report['app_path']}. "
                           "Open the required settings, allow this exact app, then quit and reopen it. "
                           f"Diagnostic log: {report['log_path']}")
            return False
        self.queue.start()
        threading.Thread(target=self._run_event_tap, name="MousehareEventTap", daemon=True).start()
        threading.Thread(target=self._connect_loop, name="MousehareConnect", daemon=True).start()
        return True

    def stop(self) -> None:
        self.stop_event.set()
        self._stop_clipboard()
        self.queue.stop()
        self._stop_event_tap()
        self._close_connection()
        self._restore_cursor()
        _notify_status("disconnected", "Mac client stopped.")

    def _display_geometry(self) -> tuple[float, float, float, float]:
        bounds = CGDisplayBounds(CGMainDisplayID())
        return (float(bounds.origin.x), float(bounds.origin.y), float(bounds.size.width), float(bounds.size.height))

    def _run_event_tap(self) -> None:
        mask = 0
        for event in (kCGEventKeyDown, kCGEventKeyUp, kCGEventFlagsChanged, kCGEventMouseMoved,
                      kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGEventRightMouseDown,
                      kCGEventRightMouseUp, kCGEventOtherMouseDown, kCGEventOtherMouseUp,
                      kCGEventLeftMouseDragged, kCGEventRightMouseDragged, kCGEventOtherMouseDragged,
                      kCGEventScrollWheel):
            mask |= CGEventMaskBit(event)
        tap = CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap, kCGEventTapOptionDefault,
                               mask, self._event_callback, None)
        if tap is None:
            self.tap_failed.set()
            _notify_status("error", "macOS refused Mousehare input monitoring. Reapprove Mousehare in Accessibility and Input Monitoring, then restart it.")
            return
        self.event_tap = tap
        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        self.run_loop = CFRunLoopGetCurrent()
        CFRunLoopAddSource(self.run_loop, source, kCFRunLoopCommonModes)
        CGEventTapEnable(tap, True)
        self.tap_ready.set()
        CFRunLoopRun()
        self.event_tap = self.run_loop = None

    def _stop_event_tap(self) -> None:
        if self.event_tap is not None:
            CGEventTapEnable(self.event_tap, False)
        if self.run_loop is not None:
            CFRunLoopStop(self.run_loop)

    def _event_callback(self, _proxy, kind: int, event, _refcon):
        if kind in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
            if self.event_tap is not None:
                CGEventTapEnable(self.event_tap, True)
            return event
        with self.lock:
            remote, returning = self.remote_active, self.returning
        if returning:
            return None
        if kind in (kCGEventKeyDown, kCGEventKeyUp):
            if not remote:
                return event
            keycode = int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode))
            key = KEYS.get(keycode)
            if key is not None:
                self._enqueue({"type": "key", "action": "press" if kind == kCGEventKeyDown else "release",
                               "key": {"kind": key[0], "value": key[1]}})
            return None
        if kind == kCGEventFlagsChanged:
            flags = int(CGEventGetFlags(event))
            previous, self.modifier_flags = self.modifier_flags, flags
            if not remote:
                return event
            for flag, name in ((kCGEventFlagMaskShift, "shift"), (kCGEventFlagMaskControl, "ctrl"),
                               (kCGEventFlagMaskAlternate, "alt"), (kCGEventFlagMaskCommand, "cmd"),
                               (kCGEventFlagMaskAlphaShift, "caps_lock")):
                if bool(previous & flag) != bool(flags & flag):
                    self._enqueue({"type": "key", "action": "press" if flags & flag else "release",
                                   "key": {"kind": "special", "value": name}})
            return None
        motion = (kCGEventMouseMoved, kCGEventLeftMouseDragged, kCGEventRightMouseDragged,
                  kCGEventOtherMouseDragged)
        if kind in motion:
            if remote:
                dx = int(CGEventGetIntegerValueField(event, kCGMouseEventDeltaX))
                dy = int(CGEventGetIntegerValueField(event, kCGMouseEventDeltaY))
                if dx or dy:
                    self._enqueue({"type": "mouse_move", "dx": dx, "dy": dy})
                return None
            return None if self._maybe_handoff(event) else event
        buttons = {kCGEventLeftMouseDown: ("left", True), kCGEventLeftMouseUp: ("left", False),
                   kCGEventRightMouseDown: ("right", True), kCGEventRightMouseUp: ("right", False),
                   kCGEventOtherMouseDown: ("middle", True), kCGEventOtherMouseUp: ("middle", False)}
        if kind in buttons:
            if not remote:
                return event
            button, pressed = buttons[kind]
            self._enqueue({"type": "mouse_click", "button": button, "pressed": pressed})
            return None
        if kind == kCGEventScrollWheel:
            if not remote:
                return event
            self._enqueue({"type": "mouse_scroll",
                           "dx": int(CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis2)),
                           "dy": int(CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis1))})
            return None
        return None if remote else event

    def _maybe_handoff(self, event) -> bool:
        with self.lock:
            if not self.connected or self.remote_active or time.monotonic() - self.last_return < 0.35:
                return False
            session = self.channel
            edge = opposite_edge(self.config.get("mac_position", "left"))
        point = CGEventGetLocation(event)
        left, top, width, height = self._display_geometry()
        threshold = max(1, int(self.config.get("edge_threshold", 3)))
        reached = {"left": point.x <= left + threshold, "right": point.x >= left + width - threshold,
                   "top": point.y <= top + threshold, "bottom": point.y >= top + height - threshold}[edge]
        if not reached:
            return False
        coordinate = point.y if edge in ("left", "right") else point.x
        origin, extent = (top, height) if edge in ("left", "right") else (left, width)
        fraction = max(0.0, min(1.0, (coordinate - origin) / max(1, extent - 1)))
        with self.lock:
            if self.remote_active or session is not self.channel:
                return False
            self.remote_active = True
        if not self.queue.enqueue(session, {"type": "activate_windows", "position": fraction}):
            with self.lock:
                self.remote_active = False
            return False
        self._hide_and_dissociate(edge, coordinate, left, top, width, height)
        _notify_status("connected", "Windows control is active.")
        return True

    def _hide_and_dissociate(self, edge, coordinate, left, top, width, height) -> None:
        if edge in ("left", "right"):
            point = (left + 12 if edge == "left" else left + width - 13,
                     max(top, min(coordinate, top + height - 1)))
        else:
            point = (max(left, min(coordinate, left + width - 1)),
                     top + 12 if edge == "top" else top + height - 13)
        CGWarpMouseCursorPosition(point)
        CGAssociateMouseAndMouseCursorPosition(False)
        self.mouse_disassociated = True
        CGDisplayHideCursor(CGMainDisplayID())
        self.cursor_hidden = True

    def _return_to_mac(self, edge: str, position) -> None:
        if edge != self.config.get("mac_position", "left"):
            return
        normalized = float(position) if isinstance(position, (int, float)) and 0 <= position <= 1 else 0.5
        with self.lock:
            if not self.remote_active:
                return
            self.returning = True
            session = self.channel
        self.queue.enqueue(session, {"type": "deactivate_windows"})
        entry = opposite_edge(edge)
        left, top, width, height = self._display_geometry()
        if entry in ("left", "right"):
            point = (left + 18 if entry == "left" else left + width - 19,
                     top + round(normalized * max(0, height - 1)))
        else:
            point = (left + round(normalized * max(0, width - 1)),
                     top + 18 if entry == "top" else top + height - 19)
        self._restore_cursor()
        CGWarpMouseCursorPosition(point)
        with self.lock:
            self.remote_active = self.returning = False
            self.last_return = time.monotonic()
        _notify_status("connected", "Mac control is active.")

    def _restore_cursor(self) -> None:
        if self.mouse_disassociated:
            CGAssociateMouseAndMouseCursorPosition(True)
            self.mouse_disassociated = False
        if self.cursor_hidden:
            CGDisplayShowCursor(CGMainDisplayID())
            self.cursor_hidden = False

    def _enqueue(self, message: dict) -> bool:
        """Queue one message on the authenticated session's ordered stream."""
        with self.lock:
            session = self.channel
            connected = self.connected
        return bool(connected and session is not None and self.queue.enqueue(session, message))

    def _close_connection(self) -> None:
        with self.lock:
            sock, session = self.sock, self.channel
            self.sock = self.channel = None
            self.connected = self.remote_active = self.returning = False
        if session is not None:
            self.queue.discard_session(session)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _connect_loop(self) -> None:
        while not self.stop_event.is_set() and not self.tap_ready.wait(0.1):
            if self.tap_failed.is_set():
                return
        host, port, token = self.config["windows_host"], int(self.config["port"]), self.config["token"]
        while not self.stop_event.is_set():
            sock = None
            try:
                _notify_status("connecting", f"Connecting to {host}:{port}…")
                sock = socket.create_connection((host, port), timeout=5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                channel, clipboard_enabled = client_handshake(
                    sock, token, self.config.get("mac_position", "left"),
                    bool(self.config.get("clipboard_sync", False)), send_message, recv_message)
                sock.settimeout(None)
                with self.lock:
                    self.sock, self.channel, self.connected = sock, channel, True
                if clipboard_enabled:
                    self._start_clipboard()
                _notify_status("connected", "Connected securely. Mac control is active.")
                while not self.stop_event.is_set():
                    message = channel.receive(sock, recv_message)
                    if message is None:
                        break
                    if message.get("type") == "edge":
                        self._return_to_mac(message.get("edge"), message.get("position"))
                    elif message.get("type") == "activation_ack":
                        positioned = message.get("cursor_positioned") is True
                        if not positioned:
                            log.warning("Windows could not place the cursor at the requested entry point")
                            _notify_status("connected", "Windows control is active, but its cursor could not be positioned reliably.")
                    elif message.get("type") == "clipboard" and self.clipboard is not None and valid_clipboard_text(message.get("text")):
                        self.clipboard.apply_remote(message["text"])
            except (OSError, HandshakeError, KeyError) as exc:
                if not self.stop_event.is_set():
                    _notify_status("connecting", f"Waiting for Windows: {exc}")
            finally:
                self._disconnect(sock)
            self.stop_event.wait(2)

    def _disconnect(self, expected) -> None:
        with self.lock:
            if expected is not None and self.sock is not None and self.sock is not expected:
                return
            sock, session = self.sock, self.channel
            self.sock = self.channel = None
            self.connected = self.remote_active = self.returning = False
        if session is not None:
            self.queue.discard_session(session)
        self._stop_clipboard()
        self._restore_cursor()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _deliver(self, session, event: dict) -> bool:
        with self.lock:
            sock, channel = self.sock, self.channel
        return bool(session is channel and sock is not None and channel.send(sock, event, send_message))

    def _queue_failed(self, _session) -> None:
        self._disconnect(None)

    def _start_clipboard(self) -> None:
        self._stop_clipboard()
        self.clipboard = ClipboardSynchronizer(lambda text: self._deliver(self.channel, {"type": "clipboard", "text": text}))
        self.clipboard.start()

    def _stop_clipboard(self) -> None:
        clipboard, self.clipboard = self.clipboard, None
        if clipboard is not None:
            clipboard.stop()


def _handle_incoming(message: dict) -> None:
    if _backend is not None and message.get("type") == "edge":
        _backend._return_to_mac(message.get("edge"), message.get("position"))


def start_backend(config: dict, status_callback=None) -> bool:
    global CONFIG, _status_callback, _backend
    stop_backend()
    CONFIG = dict(config)
    _status_callback = status_callback
    _setup_logging(bool(CONFIG.get("debug", False)))
    _backend = MacBackend(CONFIG)
    return _backend.start()


def stop_backend() -> None:
    global _backend
    backend, _backend = _backend, None
    if backend is not None:
        backend.stop()


def main() -> None:
    config_path = Path(__file__).with_name("config.json")
    start_backend(json.loads(config_path.read_text(encoding="utf-8")))
    try:
        while _backend is not None and not _backend.stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        stop_backend()


if __name__ == "__main__":
    main()
