"""macOS input capture and authenticated client."""
from __future__ import annotations

import logging
import socket
import threading
import time

from .protocol import ProtocolError, client_handshake

log = logging.getLogger(__name__)

try:
    from Quartz import *
    from CoreFoundation import CFMachPortCreateRunLoopSource, CFRunLoopAddSource, CFRunLoopGetCurrent, CFRunLoopRun, CFRunLoopStop, kCFRunLoopCommonModes
except ImportError as exc:
    QUARTZ_ERROR = str(exc)
else:
    QUARTZ_ERROR = ""

KEYCODES = {
    0x00: "a", 0x01: "s", 0x02: "d", 0x03: "f", 0x04: "h", 0x05: "g",
    0x06: "z", 0x07: "x", 0x08: "c", 0x09: "v", 0x0B: "b", 0x0C: "q",
    0x0D: "w", 0x0E: "e", 0x0F: "r", 0x10: "y", 0x11: "t", 0x12: "1",
    0x13: "2", 0x14: "3", 0x15: "4", 0x16: "6", 0x17: "5", 0x18: "=",
    0x19: "9", 0x1A: "7", 0x1B: "-", 0x1C: "8", 0x1D: "0", 0x1E: "]",
    0x1F: "o", 0x20: "u", 0x21: "[", 0x22: "i", 0x23: "p", 0x24: "enter",
    0x25: "l", 0x26: "j", 0x27: "'", 0x28: "k", 0x29: ";", 0x2A: "\\",
    0x2B: ",", 0x2C: "/", 0x2D: "n", 0x2E: "m", 0x2F: ".", 0x30: "tab",
    0x31: "space", 0x33: "backspace", 0x35: "esc", 0x39: "caps_lock",
    0x60: "f5", 0x61: "f6", 0x62: "f7", 0x63: "f3", 0x64: "f8", 0x65: "f9",
    0x76: "f4",
    0x67: "f11", 0x6D: "f10", 0x6F: "f12", 0x73: "home", 0x74: "page_up",
    0x75: "delete", 0x77: "end", 0x78: "f2", 0x79: "page_down", 0x7A: "f1",
    0x7B: "left", 0x7C: "right", 0x7D: "down", 0x7E: "up",
}
class MacController:
    def __init__(self, host, port, token, position, on_status):
        self.host, self.port, self.token, self.position = host, int(port), token, position
        self.on_status = on_status
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.sock = self.channel = None
        self.connected = self.remote = self.returning = False
        self.tap = self.loop = None
        self.tap_ready = threading.Event()
        self.tap_failed = threading.Event()
        self.last_return = 0.0
        self.modifiers_pressed = set()
        self.buttons_pressed = set()

    def permissions(self, request=False):
        if QUARTZ_ERROR:
            return {"accessibility": False, "input": False, "error": QUARTZ_ERROR}
        access = bool(CGPreflightPostEventAccess())
        input_access = bool(CGPreflightListenEventAccess())
        if request:
            if not access: CGRequestPostEventAccess()
            if not input_access: CGRequestListenEventAccess()
            access, input_access = bool(CGPreflightPostEventAccess()), bool(CGPreflightListenEventAccess())
        return {"accessibility": access, "input": input_access}

    def start(self):
        report = self.permissions(request=True)
        if not report["accessibility"] or not report["input"]:
            raise PermissionError("Enable Accessibility and Input Monitoring for /Applications/MousehareV3.app, then quit and reopen that app.")
        threading.Thread(target=self._event_loop, daemon=True, name="V3EventTap").start()
        threading.Thread(target=self._connect_loop, daemon=True, name="V3Connect").start()

    def stop(self):
        self.stop_event.set()
        if self.loop: CFRunLoopStop(self.loop)
        with self.lock:
            sock, self.sock = self.sock, None
            self.channel = None
            self.connected = self.remote = False
        self._restore()
        if sock:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            sock.close()

    def _event_loop(self):
        kinds = (kCGEventKeyDown, kCGEventKeyUp, kCGEventFlagsChanged, kCGEventMouseMoved,
                 kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGEventRightMouseDown,
                 kCGEventRightMouseUp, kCGEventOtherMouseDown, kCGEventOtherMouseUp,
                 kCGEventLeftMouseDragged, kCGEventRightMouseDragged, kCGEventScrollWheel)
        mask = 0
        for kind in kinds: mask |= CGEventMaskBit(kind)
        self.tap = CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap,
                                    kCGEventTapOptionDefault, mask, self._event, None)
        if self.tap is None:
            self.on_status("macOS refused the input event tap. Recheck both permissions for this V3 app.")
            self.tap_failed.set()
            return
        source = CFMachPortCreateRunLoopSource(None, self.tap, 0)
        self.loop = CFRunLoopGetCurrent()
        CFRunLoopAddSource(self.loop, source, kCFRunLoopCommonModes)
        CGEventTapEnable(self.tap, True)
        self.tap_ready.set()
        CFRunLoopRun()

    def _event(self, _proxy, kind, event, _ref):
        if kind in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
            CGEventTapEnable(self.tap, True)
            return event
        with self.lock: remote, returning = self.remote, self.returning
        if returning: return None
        if kind in (kCGEventMouseMoved, kCGEventLeftMouseDragged, kCGEventRightMouseDragged):
            if remote:
                dx = int(CGEventGetIntegerValueField(event, kCGMouseEventDeltaX))
                dy = int(CGEventGetIntegerValueField(event, kCGMouseEventDeltaY))
                if dx or dy: self._send({"type": "move", "dx": dx, "dy": dy})
                return None
            handed_off = self._handoff(event)
            return None if handed_off else event
        if kind == kCGEventFlagsChanged:
            keycode = int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode))
            name = {56: "shift_l", 60: "shift_r", 59: "ctrl_l", 62: "ctrl_r",
                    58: "alt_l", 61: "alt_r", 55: "ctrl_l", 54: "ctrl_r"}.get(keycode)
            if name:
                down = name not in self.modifiers_pressed
                if down: self.modifiers_pressed.add(name)
                else: self.modifiers_pressed.discard(name)
                if remote: self._send({"type": "key", "key": name, "down": down})
            return None if remote else event
        if kind in (kCGEventKeyDown, kCGEventKeyUp):
            if not remote: return event
            key = KEYCODES.get(int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)))
            if key: self._send({"type": "key", "key": key, "down": kind == kCGEventKeyDown})
            return None
        buttons = {kCGEventLeftMouseDown: ("left", True), kCGEventLeftMouseUp: ("left", False),
                   kCGEventRightMouseDown: ("right", True), kCGEventRightMouseUp: ("right", False),
                   kCGEventOtherMouseDown: ("middle", True), kCGEventOtherMouseUp: ("middle", False)}
        if kind in buttons:
            button, down = buttons[kind]
            if down: self.buttons_pressed.add(button)
            else: self.buttons_pressed.discard(button)
            if remote:
                self._send({"type": "button", "button": button, "down": down})
                return None
        if kind == kCGEventScrollWheel and remote:
            self._send({"type": "scroll", "dx": int(CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis2)),
                        "dy": int(CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis1))})
            return None
        return None if remote else event

    def _handoff(self, event):
        with self.lock:
            if not self.connected or self.remote or time.monotonic() - self.last_return < 0.35: return False
        pos = CGEventGetLocation(event)
        bounds = CGDisplayBounds(CGMainDisplayID())
        left, top, width, height = bounds.origin.x, bounds.origin.y, bounds.size.width, bounds.size.height
        edge = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}[self.position]
        limits = {"left": pos.x <= left + 3, "right": pos.x >= left + width - 3,
                  "top": pos.y <= top + 3, "bottom": pos.y >= top + height - 3}
        if not limits[edge]: return False
        vertical = edge in ("left", "right")
        coordinate, start, extent = ((pos.y, top, height) if vertical else (pos.x, left, width))
        fraction = max(0.0, min(1.0, (coordinate - start) / max(1, extent - 1)))
        with self.lock: self.remote = True
        self._send({"type": "activate", "position": fraction})
        for key in tuple(self.modifiers_pressed):
            self._send({"type": "key", "key": key, "down": True})
        for button in tuple(self.buttons_pressed):
            self._send({"type": "button", "button": button, "down": True})
        if vertical:
            point = (left + 12 if edge == "left" else left + width - 13, coordinate)
        else:
            point = (coordinate, top + 12 if edge == "top" else top + height - 13)
        CGWarpMouseCursorPosition(point)
        CGAssociateMouseAndMouseCursorPosition(False)
        CGDisplayHideCursor(CGMainDisplayID())
        self.on_status("Windows has control.")
        return True

    def _return_to_mac(self, position):
        with self.lock:
            if not self.remote: return
            self.returning = True
        self._send({"type": "deactivate"})
        bounds = CGDisplayBounds(CGMainDisplayID())
        left, top, width, height = bounds.origin.x, bounds.origin.y, bounds.size.width, bounds.size.height
        vertical = self.position in ("left", "right")
        extent = height if vertical else width
        coordinate = (top if vertical else left) + round(position * max(0, extent - 1))
        entry = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}[self.position]
        point = ((left + 18 if entry == "left" else left + width - 19, coordinate) if vertical
                 else (coordinate, top + 18 if entry == "top" else top + height - 19))
        self._restore()
        CGWarpMouseCursorPosition(point)
        with self.lock:
            self.remote = self.returning = False
            self.last_return = time.monotonic()
        self.on_status("Mac control is active.")

    def _restore(self):
        if QUARTZ_ERROR: return
        CGAssociateMouseAndMouseCursorPosition(True)
        CGDisplayShowCursor(CGMainDisplayID())

    def _connect_loop(self):
        while not self.stop_event.is_set() and not self.tap_ready.wait(0.25):
            if self.tap_failed.is_set(): return
        while not self.stop_event.is_set():
            try:
                self.on_status(f"Connecting to {self.host}:{self.port}…")
                sock = socket.create_connection((self.host, self.port), timeout=5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                channel = client_handshake(sock, self.token, self.position)
                sock.settimeout(None)
                with self.lock:
                    self.sock, self.channel, self.connected = sock, channel, True
                self.on_status("Securely paired. Mac control is active.")
                while not self.stop_event.is_set():
                    message = channel.receive(sock)
                    if message.get("type") == "return":
                        position = message.get("position")
                        if (not isinstance(position, (int, float)) or isinstance(position, bool)
                                or not 0.0 <= position <= 1.0):
                            raise ProtocolError("Windows returned an invalid cursor position")
                        self._return_to_mac(float(position))
                    elif message.get("type") == "ack" and not message.get("positioned"):
                        self.on_status("Windows could not position its cursor at the entry edge.")
            except (OSError, ProtocolError, ConnectionError) as exc:
                if not self.stop_event.is_set(): self.on_status("Waiting for Windows: " + str(exc))
            finally:
                with self.lock:
                    sock, self.sock = self.sock, None
                    self.channel = None
                    self.connected = self.remote = False
                self._restore()
                if sock:
                    try: sock.close()
                    except OSError: pass
            self.stop_event.wait(2)

    def _send(self, message):
        with self.lock: sock, channel = self.sock, self.channel
        if sock and channel:
            try: channel.send(sock, message)
            except (OSError, ProtocolError): log.exception("Could not send V3 input event")
