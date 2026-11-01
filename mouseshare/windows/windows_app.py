"""Windows receiver backend for Mousehare.

The GUI lives in ``mouseshare_app.py``.  This module owns one authenticated
Mac session, input injection, and the physical edge that returns control.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
from logging.handlers import RotatingFileHandler
import os
import socket
import sys
import threading
import time
from pathlib import Path

try:
    from pynput import mouse as pynput_mouse
    from pynput.keyboard import Controller as KeyboardController, Key, KeyCode
    from pynput.mouse import Button, Controller as MouseController
except ImportError:
    pynput_mouse = None
    KeyboardController = Key = KeyCode = Button = MouseController = None

try:
    from .protocol import recv_message, send_message
    from ..clipboard_sync import ClipboardSynchronizer, valid_clipboard_text
    from ..secure_transport import HandshakeError, PROTOCOL_VERSION, server_handshake
except ImportError:  # direct contributor execution
    from protocol import recv_message, send_message
    from mouseshare.clipboard_sync import ClipboardSynchronizer, valid_clipboard_text
    from mouseshare.secure_transport import HandshakeError, PROTOCOL_VERSION, server_handshake


log = logging.getLogger(__name__)
CONFIG: dict = {}
_status_callback = None
_backend: "WindowsBackend | None" = None

_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_LLMHF_INJECTED = 0x00000001
_LLMHF_LOWER_IL_INJECTED = 0x00000002
_VALID_EDGES = frozenset(("left", "right", "top", "bottom"))


def _notify_status(status: str, message: str) -> None:
    if _status_callback is not None:
        try:
            _status_callback(status, message)
        except Exception:
            log.exception("Status callback failed")


def _setup_logging(debug: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    if not root.handlers:
        logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if any(getattr(handler, "name", "") == "mousehare-windows-log" for handler in root.handlers):
        return
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Mousehare"
    try:
        base.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(base / "Mousehare.log", maxBytes=512 * 1024,
                                      backupCount=3, encoding="utf-8")
        handler.name = "mousehare-windows-log"
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(handler)
    except OSError:
        log.exception("Could not open the Windows diagnostic log")


def _normalized_position(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 <= value <= 1.0:
        return float(value)
    return None


def _get_local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 1))
        ip = probe.getsockname()[0]
        return ip if not ip.startswith("127.") else "127.0.0.1"
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def _virtual_screen_bounds() -> tuple[int, int, int, int]:
    if sys.platform != "win32":
        return (0, 0, 1, 1)
    user32 = ctypes.windll.user32
    return (user32.GetSystemMetrics(_SM_XVIRTUALSCREEN),
            user32.GetSystemMetrics(_SM_YVIRTUALSCREEN),
            max(1, user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)),
            max(1, user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)))


def _monitor_bounds() -> list[tuple[int, int, int, int]]:
    """Return real monitor rectangles, falling back to the virtual desktop."""
    if sys.platform != "win32":
        left, top, width, height = _virtual_screen_bounds()
        return [(left, top, left + width, top + height)]

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    monitors: list[tuple[int, int, int, int]] = []
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.POINTER(wintypes.RECT), ctypes.c_ssize_t)

    def collect(monitor, _dc, _rect, _data):
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect = info.rcMonitor
            monitors.append((rect.left, rect.top, rect.right, rect.bottom))
        return True

    user32.EnumDisplayMonitors(None, None, callback_type(collect), 0)
    if monitors:
        return monitors
    left, top, width, height = _virtual_screen_bounds()
    return [(left, top, left + width, top + height)]


def _entry_point(edge: str, fraction: float, bounds: tuple[int, int, int, int],
                 monitors: list[tuple[int, int, int, int]], inset: int = 18) -> tuple[int, int, float]:
    """Map a normalized Mac position to a valid inset point on a Windows edge."""
    left, top, width, height = bounds
    vertical = edge in ("left", "right")
    start, extent = (top, height) if vertical else (left, width)
    requested = start + round(fraction * max(0, extent - 1))
    right, bottom = left + width, top + height
    if edge == "left":
        candidates = [item for item in monitors if item[0] == left]
    elif edge == "right":
        candidates = [item for item in monitors if item[2] == right]
    elif edge == "top":
        candidates = [item for item in monitors if item[1] == top]
    else:
        candidates = [item for item in monitors if item[3] == bottom]
    if not candidates:
        candidates = [(left, top, right, bottom)]

    def distance(rect):
        low, high = (rect[1], rect[3] - 1) if vertical else (rect[0], rect[2] - 1)
        return abs(min(max(requested, low), high) - requested)

    rect = min(candidates, key=distance)
    low, high = (rect[1], rect[3] - 1) if vertical else (rect[0], rect[2] - 1)
    along = min(max(requested, low), high)
    maximum_inset = (rect[2] - rect[0] - 1) if vertical else (rect[3] - rect[1] - 1)
    safe_inset = max(1, min(inset, maximum_inset))
    if edge == "left":
        x, y = rect[0] + safe_inset, along
    elif edge == "right":
        x, y = rect[2] - 1 - safe_inset, along
    elif edge == "top":
        x, y = along, rect[1] + safe_inset
    else:
        x, y = along, rect[3] - 1 - safe_inset
    effective = max(0.0, min(1.0, (along - start) / max(1, extent - 1)))
    return x, y, effective


def _edge_reached(edge: str, x: int, y: int, bounds: tuple[int, int, int, int], threshold: int) -> bool:
    left, top, width, height = bounds
    return {"left": x <= left + threshold, "right": x >= left + width - 1 - threshold,
            "top": y <= top + threshold, "bottom": y >= top + height - 1 - threshold}[edge]


SPECIAL_KEYS = {
    "enter": "enter", "space": "space", "tab": "tab", "backspace": "backspace",
    "delete": "delete", "escape": "esc", "shift": "shift", "ctrl": "ctrl",
    "alt": "alt", "cmd": "ctrl", "caps_lock": "caps_lock", "left": "left",
    "right": "right", "up": "up", "down": "down", "home": "home", "end": "end",
    "page_up": "page_up", "page_down": "page_down", "insert": "insert",
    "num_lock": "num_lock", "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4",
    "f5": "f5", "f6": "f6", "f7": "f7", "f8": "f8", "f9": "f9", "f10": "f10",
    "f11": "f11", "f12": "f12",
}


class WindowsBackend:
    """One Windows server session, with state protected by one lock."""

    def __init__(self, config: dict):
        self.config = dict(config)
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.server: socket.socket | None = None
        self.sock: socket.socket | None = None
        self.channel = None
        self.listener = None
        self.active = False
        self.return_armed = False
        self.activation_guard_until = 0.0
        self.injection_guard_until = 0.0
        self.mac_position = "left"
        self.clipboard: ClipboardSynchronizer | None = None
        self.keyboard = KeyboardController() if KeyboardController is not None else None
        self.mouse = MouseController() if MouseController is not None else None

    def start(self) -> bool:
        if sys.platform != "win32" or pynput_mouse is None or self.keyboard is None or self.mouse is None:
            _notify_status("error", "Windows input support is unavailable. Install the Windows dependencies on Windows.")
            return False
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        self.listener = pynput_mouse.Listener(on_move=self._on_mouse_move,
                                              win32_event_filter=self._mouse_filter)
        self.listener.start()
        threading.Thread(target=self._serve, name="MousehareServer", daemon=True).start()
        return True

    def stop(self) -> None:
        self.stop_event.set()
        self._stop_clipboard()
        with self.lock:
            server, sock = self.server, self.sock
            self.server = self.sock = None
            self.channel = None
            self.active = False
        for item in (sock, server):
            if item is None:
                continue
            try:
                item.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                item.close()
            except OSError:
                pass
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        _notify_status("disconnected", "Windows server stopped.")

    def _serve(self) -> None:
        host = self.config.get("host") or "0.0.0.0"
        port = int(self.config.get("port", 5555))
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((host, port))
            server.listen(2)
            server.settimeout(0.5)
        except OSError as exc:
            _notify_status("error", f"Windows cannot listen on {host}:{port}: {exc}")
            return
        with self.lock:
            self.server = server
        _notify_status("disconnected", f"Listening on {_get_local_ip()}:{port}; waiting for the Mac.")
        while not self.stop_event.is_set():
            try:
                conn, address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                occupied = self.sock is not None
            if occupied:
                conn.close()
            else:
                threading.Thread(target=self._handle_client, args=(conn, address),
                                 name="MousehareClient", daemon=True).start()

    def _handle_client(self, conn: socket.socket, address) -> None:
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(10)
            channel, position, clipboard_enabled = server_handshake(
                conn, self.config["token"], bool(self.config.get("clipboard_sync", False)),
                send_message, recv_message)
            conn.settimeout(None)
        except (HandshakeError, OSError, KeyError) as exc:
            log.warning("Pairing from %s rejected: %s", address, exc)
            conn.close()
            return
        with self.lock:
            if self.sock is not None:
                conn.close()
                return
            self.sock, self.channel = conn, channel
            self.mac_position = position
            self.active = False
        if clipboard_enabled:
            self._start_clipboard()
        _notify_status("connected", f"Paired with Mac at {address[0]}. Mac control is active.")
        try:
            while not self.stop_event.is_set():
                message = channel.receive(conn, recv_message)
                if message is None:
                    break
                self._dispatch(message)
        finally:
            self._close_client(conn)

    def _close_client(self, conn: socket.socket) -> None:
        with self.lock:
            if self.sock is not conn:
                return
            self.sock = self.channel = None
            self.active = self.return_armed = False
        self._stop_clipboard()
        try:
            conn.close()
        except OSError:
            pass
        if not self.stop_event.is_set():
            _notify_status("disconnected", "Mac disconnected. Windows is still listening.")

    def _send(self, message: dict) -> bool:
        with self.lock:
            sock, channel = self.sock, self.channel
        return bool(sock is not None and channel is not None and channel.send(sock, message, send_message))

    def _dispatch(self, message: dict) -> None:
        kind = message.get("type") if isinstance(message, dict) else None
        if kind == "activate_windows":
            self._activate(message)
        elif kind == "deactivate_windows":
            with self.lock:
                self.active = self.return_armed = False
            _notify_status("connected", "Mac control is active.")
        elif kind == "mouse_move":
            self._move(message)
        elif kind == "mouse_click":
            self._click(message)
        elif kind == "mouse_scroll":
            self._scroll(message)
        elif kind == "key":
            self._key(message)
        elif kind == "clipboard" and self.clipboard is not None and valid_clipboard_text(message.get("text")):
            self.clipboard.apply_remote(message["text"])

    def _activate(self, message: dict) -> None:
        position = _normalized_position(message.get("position"))
        if position is None:
            position = 0.5
        edge = self.mac_position if self.mac_position in _VALID_EDGES else "left"
        placed = None
        try:
            x, y, effective_position = _entry_point(
                edge, position, _virtual_screen_bounds(), _monitor_bounds())
            if ctypes.windll.user32.SetCursorPos(x, y):
                placed = effective_position
        except Exception as exc:
            log.warning("Could not position cursor on activation: %s", exc)
        with self.lock:
            self.active = True
            self.return_armed = False
            # SetCursorPos and hook delivery are asynchronous. The guard and
            # arm requirement prevent a new session bouncing back immediately.
            self.activation_guard_until = time.monotonic() + 0.55
        self._send({"type": "activation_ack", "active": True,
                    "cursor_positioned": placed is not None, "edge": edge,
                    "position": placed if placed is not None else position})
        _notify_status("connected", "Windows control is active.")

    def _input_allowed(self) -> bool:
        with self.lock:
            return self.active

    def _injecting(self) -> None:
        with self.lock:
            self.injection_guard_until = time.monotonic() + 0.08

    def _move(self, message: dict) -> None:
        dx, dy = message.get("dx"), message.get("dy")
        if type(dx) is not int or type(dy) is not int or abs(dx) > 10_000 or abs(dy) > 10_000 or not self._input_allowed():
            return
        self._injecting()
        try:
            ctypes.windll.user32.mouse_event(0x0001, dx, dy, 0, 0)
        except Exception as exc:
            log.warning("Mouse injection failed: %s", exc)

    def _click(self, message: dict) -> None:
        name, pressed = message.get("button"), message.get("pressed")
        if name not in ("left", "right", "middle") or type(pressed) is not bool or not self._input_allowed():
            return
        button = getattr(Button, name, None)
        if button is None:
            return
        self._injecting()
        try:
            (self.mouse.press if pressed else self.mouse.release)(button)
        except Exception as exc:
            log.warning("Mouse click injection failed: %s", exc)

    def _scroll(self, message: dict) -> None:
        dx, dy = message.get("dx"), message.get("dy")
        if type(dx) is not int or type(dy) is not int or abs(dx) > 1_000 or abs(dy) > 1_000 or not self._input_allowed():
            return
        self._injecting()
        try:
            self.mouse.scroll(dx, dy)
        except Exception as exc:
            log.warning("Scroll injection failed: %s", exc)

    def _key(self, message: dict) -> None:
        if not self._input_allowed() or message.get("action") not in ("press", "release"):
            return
        item = message.get("key")
        if not isinstance(item, dict):
            return
        if item.get("kind") == "char" and isinstance(item.get("value"), str) and len(item["value"]) == 1:
            key = KeyCode.from_char(item["value"])
        elif item.get("kind") == "special":
            key = getattr(Key, SPECIAL_KEYS.get(item.get("value"), ""), None)
        else:
            key = None
        if key is None:
            return
        try:
            (self.keyboard.press if message["action"] == "press" else self.keyboard.release)(key)
        except Exception as exc:
            log.warning("Keyboard injection failed: %s", exc)

    def _mouse_filter(self, _message, data):
        flags = getattr(data, "flags", 0)
        return not bool(flags & (_LLMHF_INJECTED | _LLMHF_LOWER_IL_INJECTED))

    def _on_mouse_move(self, x: int, y: int) -> None:
        with self.lock:
            active, edge = self.active, self.mac_position
            guard, armed = max(self.activation_guard_until, self.injection_guard_until), self.return_armed
        if not active or edge not in _VALID_EDGES or time.monotonic() < guard:
            return
        bounds = _virtual_screen_bounds()
        threshold = max(1, int(self.config.get("edge_threshold", 3)))
        if not armed:
            if not _edge_reached(edge, x, y, bounds, max(28, threshold * 4)):
                with self.lock:
                    self.return_armed = True
            return
        if not _edge_reached(edge, x, y, bounds, threshold):
            return
        vertical = edge in ("left", "right")
        start, extent = (bounds[1], bounds[3]) if vertical else (bounds[0], bounds[2])
        coordinate = y if vertical else x
        position = max(0.0, min(1.0, (coordinate - start) / max(1, extent - 1)))
        with self.lock:
            if not self.active:
                return
            self.active = self.return_armed = False
        self._send({"type": "edge", "edge": edge, "position": position})
        _notify_status("connected", "Mac control resumed.")

    def _start_clipboard(self) -> None:
        self._stop_clipboard()
        self.clipboard = ClipboardSynchronizer(lambda text: self._send({"type": "clipboard", "text": text}))
        self.clipboard.start()

    def _stop_clipboard(self) -> None:
        clipboard, self.clipboard = self.clipboard, None
        if clipboard is not None:
            clipboard.stop()


def _handle_activate_windows(message: dict) -> None:
    if _backend is not None:
        _backend._activate(message)


def start_backend(config: dict, status_callback=None) -> bool:
    global CONFIG, _status_callback, _backend
    stop_backend()
    CONFIG = dict(config)
    _status_callback = status_callback
    _setup_logging(bool(CONFIG.get("debug", False)))
    _backend = WindowsBackend(CONFIG)
    return _backend.start()


def stop_backend() -> None:
    global _backend
    backend, _backend = _backend, None
    if backend is not None:
        backend.stop()


def main() -> None:
    import json
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
