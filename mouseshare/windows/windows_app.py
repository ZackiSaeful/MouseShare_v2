#!/usr/bin/env python3
"""
MouseShare -- Windows Agent
===========================
* TCP server: accepts one connection from the Mac agent.
* Performs handshake and token verification.
* Receives input events and injects them via pynput.
* Monitors cursor position; sends edge=left to Mac when cursor hits left edge.
* System tray icon shows connection/active state.

Run on Windows:
    cd windows
    .venv\\Scripts\\python.exe windows_app.py
"""
from __future__ import annotations

import ctypes
import json
import logging
import socket
import sys
import threading
from enum import Enum, auto
from pathlib import Path

try:
    from pynput import mouse as ms
    from pynput.keyboard import Controller as KbController, Key, KeyCode
    from pynput.mouse import Button, Controller as MsController
except ImportError as _e:
    print(f"[FATAL] Cannot import pynput: {_e}")
    print("        pip install pynput")
    sys.exit(1)

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError as _e:
    print(f"[FATAL] Cannot import pystray/Pillow: {_e}")
    sys.exit(1)

try:
    from .protocol import recv_message, send_message
except ImportError:  # Direct legacy execution: python windows_app.py
    from protocol import recv_message, send_message


# ── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )


log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  State
# ═══════════════════════════════════════════════════════════════════════════════

class WinState:
    """Thread-safe state for the Windows agent."""

    def __init__(self) -> None:
        self.lock        = threading.Lock()
        self.active      = False   # True == WINDOWS_ACTIVE (receiving remote input)
        self.client_sock = None    # current Mac TCP socket
        self.injecting   = False   # True while injecting a mouse movement


STATE  = WinState()
CONFIG: dict = {}
_status_callback = None


def _notify_status(status: str, message: str = "") -> None:
    callback = _status_callback
    if callback is not None:
        try:
            callback(status, message)
        except Exception:
            log.exception("[GUI] Status callback failed")

# ── Input controllers ──────────────────────────────────────────────────────────

_kb = KbController()
_ms = MsController()


# ═══════════════════════════════════════════════════════════════════════════════
#  Keyboard translation layer
#
#  semantic key -> pynput Key/KeyCode
#
#  NOTE: Mac Command ("cmd") is mapped to Windows Ctrl.
#        This makes Cmd+C -> Ctrl+C, Cmd+V -> Ctrl+V, etc. work automatically:
#        the Mac sends   [cmd down] [c down] [c up] [cmd up]
#        Windows receives [ctrl down] [c down] [c up] [ctrl up]  = Ctrl+C.
# ═══════════════════════════════════════════════════════════════════════════════

SPECIAL_KEY_MAP: dict[str, object] = {
    "enter":       Key.enter,
    "space":       Key.space,
    "tab":         Key.tab,
    "backspace":   Key.backspace,
    "delete":      Key.delete,
    "escape":      Key.esc,
    # Modifiers -- note: cmd -> ctrl (Command mapping)
    "shift":       Key.shift,
    "ctrl":        Key.ctrl,
    "alt":         Key.alt,
    "cmd":         Key.ctrl,      # Mac Command -> Windows Ctrl
    "caps_lock":   Key.caps_lock,
    # Arrow keys
    "left":        Key.left,
    "right":       Key.right,
    "up":          Key.up,
    "down":        Key.down,
    # Navigation
    "home":        Key.home,
    "end":         Key.end,
    "page_up":     Key.page_up,
    "page_down":   Key.page_down,
    "insert":      Key.insert,
    # Misc
    "num_lock":    Key.num_lock,
    "print_screen":Key.print_screen,
    "scroll_lock": Key.scroll_lock,
    "pause":       Key.pause,
    # Function keys
    "f1":  Key.f1,  "f2":  Key.f2,  "f3":  Key.f3,  "f4":  Key.f4,
    "f5":  Key.f5,  "f6":  Key.f6,  "f7":  Key.f7,  "f8":  Key.f8,
    "f9":  Key.f9,  "f10": Key.f10, "f11": Key.f11, "f12": Key.f12,
}

MOUSE_BUTTON_MAP: dict[str, Button] = {
    "left":   Button.left,
    "right":  Button.right,
    "middle": Button.middle,
}


def _resolve_key(key_dict: dict):
    """Convert a semantic key dict to a pynput Key or KeyCode.
    Returns None if the key is unknown."""
    kind  = key_dict.get("kind", "char")
    value = key_dict.get("value", "")

    if kind == "char":
        if len(value) == 1:
            return KeyCode.from_char(value)
        log.warning("[KEY] Unexpected char value: %r", value)
        return None

    if kind == "special":
        mapped = SPECIAL_KEY_MAP.get(value)
        if mapped is not None:
            return mapped
        log.warning("[KEY] Unknown special key: %r", value)
        return None

    log.warning("[KEY] Unknown key kind: %r", kind)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  Tray icon
# ═══════════════════════════════════════════════════════════════════════════════

def _make_icon(color: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([8, 8, 56, 56], fill=color)
    return img


_ICON_GRAY  = _make_icon("#808080")   # no Mac connected
_ICON_GREEN = _make_icon("#00CC44")   # connected, mac active
_ICON_BLUE  = _make_icon("#0088FF")   # connected, windows active

_tray_icon = None


def _update_tray() -> None:
    ic = _tray_icon
    if ic is None:
        return
    with STATE.lock:
        connected = STATE.client_sock is not None
        active    = STATE.active
    if not connected:
        ic.icon  = _ICON_GRAY
        ic.title = "MouseShare -- No Mac connected"
    elif active:
        ic.icon  = _ICON_BLUE
        ic.title = "MouseShare -- Windows Active"
    else:
        ic.icon  = _ICON_GREEN
        ic.title = "MouseShare -- Mac Active"


# ═══════════════════════════════════════════════════════════════════════════════
#  Network helpers
# ═══════════════════════════════════════════════════════════════════════════════

_send_lock = threading.Lock()


def _net_send(msg: dict) -> bool:
    with STATE.lock:
        s = STATE.client_sock
    if s is None:
        return False
    with _send_lock:
        return send_message(s, msg)


# ═══════════════════════════════════════════════════════════════════════════════
#  Edge detection (Windows cursor reaching left edge)
# ═══════════════════════════════════════════════════════════════════════════════

_edge_sent = False
_edge_lock = threading.Lock()


def _on_mouse_move(x: int, y: int) -> None:
    """pynput mouse listener callback.
    Detects when cursor reaches the left screen edge while WINDOWS_ACTIVE.

    The _edge_sent flag prevents sending the edge notification more than once
    per crossing (debounce). It resets when the cursor moves away from the edge
    or when WINDOWS_ACTIVE starts again."""
    global _edge_sent

    with STATE.lock:
        active = STATE.active

    if not active:
        # Reset debounce when not active, so the next activation works cleanly
        with _edge_lock:
            _edge_sent = False
        return

    threshold = int(CONFIG.get("edge_threshold", 3))

    if x <= threshold:
        with _edge_lock:
            if _edge_sent:
                return          # already sent for this crossing, skip
            _edge_sent = True

        log.info("[EDGE] Windows left edge detected at x=%d", x)

        # Immediately mark inactive (prevents further edge sends)
        with STATE.lock:
            STATE.active = False

        # Notify Mac
        _net_send({"type": "edge", "edge": "left"})
        _update_tray()

    else:
        # Cursor moved away from edge -- reset debounce
        with _edge_lock:
            _edge_sent = False


# ═══════════════════════════════════════════════════════════════════════════════
#  Message handlers
# ═══════════════════════════════════════════════════════════════════════════════

def _handle_activate_windows() -> None:
    """Mac is sending input to Windows now."""
    log.info("[STATE] WINDOWS_ACTIVE")
    global _edge_sent
    with _edge_lock:
        _edge_sent = False   # fresh start -- reset debounce
    with STATE.lock:
        STATE.active = True
    _update_tray()


def _handle_deactivate_windows() -> None:
    """Mac resumed control of its own input."""
    log.info("[STATE] MAC_ACTIVE (deactivated by Mac)")
    with STATE.lock:
        STATE.active = False
    _update_tray()


def _handle_key(msg: dict) -> None:
    """Inject a keyboard event."""
    action = msg.get("action")
    key    = _resolve_key(msg.get("key", {}))
    if key is None:
        return
    if CONFIG.get("debug"):
        log.debug("[KEY] %s %s", msg.get("key", {}).get("value", "?"), action)
    try:
        if action == "press":
            _kb.press(key)
        elif action == "release":
            _kb.release(key)
        else:
            log.warning("[KEY] Unknown action: %r", action)
    except Exception as exc:
        log.error("[KEY] Injection error: %s", exc)


def _handle_mouse_move(msg: dict) -> None:
    """Inject a relative mouse movement.

    Uses the injecting flag so that the on_mouse_move callback can identify
    programmatic movements vs. user-initiated movements (feedback-loop guard).
    """
    dx = int(msg.get("dx", 0))
    dy = int(msg.get("dy", 0))
    if not (dx or dy):
        return
    if CONFIG.get("debug"):
        log.debug("[MOUSE] dx=%d dy=%d", dx, dy)
    with STATE.lock:
        STATE.injecting = True
    try:
        _ms.move(dx, dy)
    except Exception as exc:
        log.error("[MOUSE] Move injection error: %s", exc)
    finally:
        with STATE.lock:
            STATE.injecting = False


def _handle_mouse_click(msg: dict) -> None:
    """Inject a mouse button press or release."""
    btn_name = msg.get("button", "left")
    pressed  = bool(msg.get("pressed", True))
    btn      = MOUSE_BUTTON_MAP.get(btn_name, Button.left)
    try:
        if pressed:
            _ms.press(btn)
        else:
            _ms.release(btn)
    except Exception as exc:
        log.error("[MOUSE] Click injection error: %s", exc)


def _handle_mouse_scroll(msg: dict) -> None:
    """Inject a scroll wheel event."""
    dx = int(msg.get("dx", 0))
    dy = int(msg.get("dy", 0))
    try:
        _ms.scroll(dx, dy)
    except Exception as exc:
        log.error("[MOUSE] Scroll injection error: %s", exc)


def _dispatch(msg: dict) -> None:
    """Route an incoming message to the appropriate handler."""
    msg_type = msg.get("type")
    log.debug("[NETWORK] <- %s", msg_type)

    if msg_type == "activate_windows":
        _handle_activate_windows()
    elif msg_type == "deactivate_windows":
        _handle_deactivate_windows()
    elif msg_type == "key":
        _handle_key(msg)
    elif msg_type == "mouse_move":
        _handle_mouse_move(msg)
    elif msg_type == "mouse_click":
        _handle_mouse_click(msg)
    elif msg_type == "mouse_scroll":
        _handle_mouse_scroll(msg)
    else:
        log.debug("[NETWORK] Unknown message type: %r", msg_type)


# ═══════════════════════════════════════════════════════════════════════════════
#  Client handler (handshake + receive loop)
# ═══════════════════════════════════════════════════════════════════════════════

def _handle_client(conn: socket.socket, addr) -> None:
    log.info("[NETWORK] Client connected from %s:%d", *addr)

    # ── Handshake ─────────────────────────────────────────────────────────────
    try:
        conn.settimeout(10.0)
        hello = recv_message(conn)
        if hello is None or hello.get("type") != "hello":
            log.warning("[NETWORK] Bad handshake -- closing")
            conn.close()
            return

        if hello.get("token", "") != CONFIG["token"]:
            log.warning("[NETWORK] Auth failed -- wrong token")
            send_message(conn, {"type": "hello_error"})
            conn.close()
            return

        with STATE.lock:
            already_connected = STATE.client_sock is not None
            if not already_connected:
                STATE.client_sock = conn
                STATE.active = False
        if already_connected:
            log.warning("[NETWORK] Rejecting extra client while one is paired")
            send_message(conn, {"type": "hello_error"})
            conn.close()
            return

        if not send_message(conn, {"type": "hello_ok"}):
            with STATE.lock:
                if STATE.client_sock is conn:
                    STATE.client_sock = None
            conn.close()
            return
        conn.settimeout(None)
        log.info("[NETWORK] Handshake OK")

    except Exception as exc:
        log.error("[NETWORK] Handshake exception: %s", exc)
        try:
            conn.close()
        except OSError:
            pass
        return

    _update_tray()
    _notify_status("connected", f"Connected to client {addr[0]}.")

    # ── Receive loop ─────────────────────────────────────────────────────────
    while True:
        msg = recv_message(conn)
        if msg is None:
            log.info("[NETWORK] Client disconnected")
            break
        try:
            _dispatch(msg)
        except Exception as exc:
            # One bad message must NOT break the TCP connection
            log.error("[NETWORK] Error dispatching message: %s", exc)

    # ── Cleanup ───────────────────────────────────────────────────────────────
    with STATE.lock:
        STATE.active      = False
        STATE.client_sock = None
    _update_tray()
    if not _server_stop.is_set():
        _notify_status("disconnected", "Client disconnected. Server is still listening.")
    try:
        conn.close()
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  TCP Server
# ═══════════════════════════════════════════════════════════════════════════════

_server_stop = threading.Event()
_server_socket: socket.socket | None = None
_server_thread: threading.Thread | None = None


def _run_server() -> None:
    global _server_socket
    host = CONFIG.get("host", "0.0.0.0")
    port = CONFIG["port"]

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((host, port))
    except OSError as exc:
        log.error("[FATAL] Cannot bind %s:%d -- %s", host, port, exc)
        _server_stop.set()
        _stop_mouse_listener()
        _notify_status("error", f"Cannot listen on {host}:{port}: {exc}")
        srv.close()
        return

    srv.listen(1)
    srv.settimeout(1.0)   # allows checking _server_stop periodically
    _server_socket = srv
    log.info("[NETWORK] Listening on %s:%d", host, port)
    _notify_status("disconnected", f"Listening on {host}:{port}. Waiting for a client.")

    while not _server_stop.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break

        # Only allow one Mac at a time; spin up a handler thread
        threading.Thread(
            target=_handle_client,
            args=(conn, addr),
            daemon=True,
            name="ClientHandler",
        ).start()

    srv.close()
    _server_socket = None
    log.info("[NETWORK] Server stopped")


# ═══════════════════════════════════════════════════════════════════════════════
#  Mouse listener (for edge detection)
# ═══════════════════════════════════════════════════════════════════════════════

_mouse_listener: ms.Listener | None = None


def _start_mouse_listener() -> None:
    global _mouse_listener
    _mouse_listener = ms.Listener(on_move=_on_mouse_move)
    _mouse_listener.start()
    log.info("[MOUSE] Listener started")


def _stop_mouse_listener() -> None:
    global _mouse_listener
    if _mouse_listener is not None:
        _mouse_listener.stop()
        _mouse_listener = None


def start_backend(config: dict, status_callback=None) -> None:
    """Start the Windows server backend without its legacy tray UI."""
    global CONFIG, _status_callback, _server_thread
    CONFIG = dict(config)
    _status_callback = status_callback
    _server_stop.clear()
    _setup_logging(CONFIG.get("debug", False))
    try:
        _start_mouse_listener()
    except Exception as exc:
        _notify_status("error", f"Could not start mouse input monitoring: {exc}")
        return
    _server_thread = threading.Thread(target=_run_server, daemon=True, name="TCPServer")
    _server_thread.start()


def stop_backend() -> None:
    """Stop accepting connections and release the listening socket."""
    _server_stop.set()
    _stop_mouse_listener()
    with STATE.lock:
        client = STATE.client_sock
    if client:
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass
    server = _server_socket
    if server:
        try:
            server.close()
        except OSError:
            pass
    _notify_status("disconnected", "Server stopped.")


# ═══════════════════════════════════════════════════════════════════════════════
#  Quit / cleanup
# ═══════════════════════════════════════════════════════════════════════════════

def _do_quit(icon=None, _item=None) -> None:
    log.info("[SHUTDOWN] Quitting MouseShare Windows Agent...")
    stop_backend()
    if icon is not None:
        icon.stop()
    log.info("[SHUTDOWN] Done")


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
    log.info("MouseShare -- Windows Agent")
    log.info("Listening on port : %d", CONFIG["port"])
    log.info("Edge threshold    : %d px", CONFIG.get("edge_threshold", 3))

    start_backend(CONFIG)

    _tray_icon = pystray.Icon(
        "MouseShare",
        _ICON_GRAY,
        "MouseShare -- No Mac connected",
        menu=pystray.Menu(
            pystray.MenuItem(
                "MouseShare Server", lambda i, it: None, enabled=False
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", _do_quit),
        ),
    )
    _update_tray()
    log.info("[TRAY] System tray icon running")
    _tray_icon.run()   # blocks on main thread


if __name__ == "__main__":
    main()
