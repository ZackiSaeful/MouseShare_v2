"""Windows input receiver and TCP server."""
from __future__ import annotations

import ctypes
import logging
import socket
import sys
import threading
import time

from .protocol import ProtocolError, receive_frame, send_frame, server_handshake

log = logging.getLogger(__name__)
PORT = 5555

try:
    from pynput import mouse
    from pynput.keyboard import Controller as Keyboard, Key, KeyCode
    from pynput.mouse import Button, Controller as Mouse
except ImportError:
    mouse = Keyboard = Key = KeyCode = Button = Mouse = None


def enable_dpi_awareness():
    if sys.platform != "win32": return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try: ctypes.windll.user32.SetProcessDPIAware()
        except Exception: pass


class WindowsReceiver:
    def __init__(self, token: str, on_status):
        self.token, self.on_status = token, on_status
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.sock = self.channel = None
        self.active = False
        self.armed = False
        self.entry_edge = "left"
        self.guard_until = 0.0
        self.keyboard = Keyboard() if Keyboard else None
        self.mouse = Mouse() if Mouse else None
        self.listener = None
        self.pressed_keys = set()
        self.pressed_buttons = set()

    def start(self) -> None:
        if sys.platform != "win32" or mouse is None:
            raise RuntimeError("Windows input backend must run on Windows with requirements-windows installed")
        enable_dpi_awareness()
        self.listener = mouse.Listener(on_move=self._on_move, win32_event_filter=self._filter)
        self.listener.start()
        threading.Thread(target=self._serve, name="V3Server", daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            sock, self.sock = self.sock, None
            self.channel = None
            self.active = False
            self._release_all()
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if self.listener:
            self.listener.stop()
            self.listener = None

    def _serve(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("0.0.0.0", PORT))
            server.listen(1)
            server.settimeout(0.5)
        except OSError as exc:
            self.on_status("Could not listen on port 5555: " + str(exc))
            server.close()
            return
        self.on_status(f"Listening on {_local_ip()}:{PORT}; pairing key: {self.token}")
        while not self.stop_event.is_set():
            try:
                conn, address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client, args=(conn, address), daemon=True).start()
        server.close()

    def _client(self, conn, address) -> None:
        accepted = False
        try:
            conn.settimeout(8)
            channel, position = server_handshake(conn, self.token)
            entry_edge = {"left": "right", "right": "left",
                          "top": "bottom", "bottom": "top"}[position]
            conn.settimeout(None)
            with self.lock:
                if self.sock is not None:
                    conn.close()
                    return
                self.sock, self.channel = conn, channel
                self.entry_edge = entry_edge
                accepted = True
            self.on_status(f"Securely paired with {address[0]}. Mac is active.")
            while not self.stop_event.is_set():
                message = channel.receive(conn)
                self._dispatch(message)
        except (OSError, ProtocolError, ConnectionError) as exc:
            log.info("Client %s ended: %s", address, exc)
        finally:
            with self.lock:
                if self.sock is conn:
                    self.sock = self.channel = None
                    self.active = self.armed = False
                    self._release_all()
            try:
                conn.close()
            except OSError:
                pass
            if accepted and not self.stop_event.is_set():
                self.on_status("Mac disconnected. Waiting for a new connection.")

    def _send(self, message) -> None:
        with self.lock:
            sock, channel = self.sock, self.channel
        if sock and channel:
            channel.send(sock, message)

    def _dispatch(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "activate":
            self._activate(message)
        elif kind == "deactivate":
            with self.lock:
                self.active = self.armed = False
                self._release_all()
            self.on_status("Mac control is active.")
        elif kind == "move":
            self._move(message)
        elif kind == "button":
            self._button(message)
        elif kind == "scroll":
            dx, dy = message.get("dx", 0), message.get("dy", 0)
            if (type(dx) is int and type(dy) is int and abs(dx) <= 1000 and abs(dy) <= 1000
                    and self._is_active()):
                self.mouse.scroll(dx, dy)
        elif kind == "key":
            self._key(message)

    def _bounds(self):
        user = ctypes.windll.user32
        return (user.GetSystemMetrics(76), user.GetSystemMetrics(77),
                user.GetSystemMetrics(78), user.GetSystemMetrics(79))

    def _activate(self, message):
        edge = self.entry_edge
        fraction = message.get("position", 0.5)
        if not isinstance(fraction, (int, float)) or isinstance(fraction, bool):
            fraction = 0.5
        fraction = max(0.0, min(1.0, fraction))
        x0, y0, width, height = self._bounds()
        inset = 22
        if edge == "left": x, y = x0 + inset, y0 + round(fraction * max(1, height - 1))
        elif edge == "right": x, y = x0 + width - 1 - inset, y0 + round(fraction * max(1, height - 1))
        elif edge == "top": x, y = x0 + round(fraction * max(1, width - 1)), y0 + inset
        else: x, y = x0 + round(fraction * max(1, width - 1)), y0 + height - 1 - inset
        positioned = bool(ctypes.windll.user32.SetCursorPos(x, y))
        with self.lock:
            self.active, self.armed = True, False
            self.guard_until = time.monotonic() + 0.6
        self._send({"type": "ack", "positioned": positioned})
        self.on_status("Windows has control. Move away from the entry edge before returning to Mac.")

    def _is_active(self):
        with self.lock:
            return self.active

    def _move(self, message):
        dx, dy = message.get("dx"), message.get("dy")
        if type(dx) is int and type(dy) is int and abs(dx) <= 10000 and abs(dy) <= 10000 and self._is_active():
            ctypes.windll.user32.mouse_event(1, dx, dy, 0, 0)

    def _button(self, message):
        if (not self._is_active() or message.get("button") not in ("left", "right", "middle")
                or type(message.get("down")) is not bool):
            return
        button = getattr(Button, message["button"])
        if message.get("down"):
            self.mouse.press(button)
            self.pressed_buttons.add(button)
        else:
            self.mouse.release(button)
            self.pressed_buttons.discard(button)

    def _key(self, message):
        if not self._is_active() or type(message.get("down")) is not bool:
            return
        key_value = message.get("key")
        key = KeyCode.from_char(key_value) if isinstance(key_value, str) and len(key_value) == 1 else getattr(Key, str(key_value), None)
        if key is not None:
            if message.get("down"):
                self.keyboard.press(key)
                self.pressed_keys.add(key)
            else:
                self.keyboard.release(key)
                self.pressed_keys.discard(key)

    def _release_all(self):
        for key in tuple(self.pressed_keys):
            try: self.keyboard.release(key)
            except Exception: log.exception("Could not release remote key")
        for button in tuple(self.pressed_buttons):
            try: self.mouse.release(button)
            except Exception: log.exception("Could not release remote mouse button")
        self.pressed_keys.clear()
        self.pressed_buttons.clear()

    def _filter(self, _message, data):
        return not bool(getattr(data, "flags", 0) & 0x03)

    def _on_move(self, x, y):
        with self.lock:
            active, edge, armed, guard = self.active, self.entry_edge, self.armed, self.guard_until
        if not active or time.monotonic() < guard:
            return
        x0, y0, width, height = self._bounds()
        threshold = 3
        at_edge = {"left": x <= x0 + threshold, "right": x >= x0 + width - 1 - threshold,
                   "top": y <= y0 + threshold, "bottom": y >= y0 + height - 1 - threshold}[edge]
        if not armed:
            distance = {"left": x > x0 + 40, "right": x < x0 + width - 1 - 40,
                        "top": y > y0 + 40, "bottom": y < y0 + height - 1 - 40}[edge]
            if distance:
                with self.lock:
                    self.armed = True
            return
        if at_edge:
            vertical = edge in ("left", "right")
            pos, extent = ((y - y0), height) if vertical else ((x - x0), width)
            with self.lock:
                self.active = self.armed = False
            self._send({"type": "return", "edge": edge,
                        "position": max(0.0, min(1.0, pos / max(1, extent - 1)))})
            self.on_status("Mac control is active.")


def _local_ip():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()
