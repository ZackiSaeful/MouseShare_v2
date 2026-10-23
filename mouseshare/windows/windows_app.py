#!/usr/bin/env python3
"""
MouseShare -- Windows Agent
===========================
* TCP server: accepts one connection from the Mac agent.
* Performs handshake and token verification.
* Receives input events and injects them via pynput.
* Monitors the edge selected by the Mac's placement and returns control there.
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
    from ..secure_transport import HandshakeError, server_handshake
    from ..clipboard_sync import ClipboardSynchronizer, valid_clipboard_text
except ImportError:  # Direct legacy execution: python windows_app.py
    from protocol import recv_message, send_message
    from mouseshare.secure_transport import HandshakeError, server_handshake
    from mouseshare.clipboard_sync import ClipboardSynchronizer, valid_clipboard_text


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
        self.client_channel = None # authenticated encrypted stream
        self.clipboard_sync_enabled = False
        self.injecting   = False   # True while injecting a mouse movement


STATE  = WinState()
CONFIG: dict = {}
_status_callback = None
_clipboard_sync_lock = threading.Lock()
_clipboard_sync: ClipboardSynchronizer | None = None


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
    if not isinstance(key_dict, dict):
        return None
    kind  = key_dict.get("kind", "char")
    value = key_dict.get("value", "")

    if kind == "char":
        if isinstance(value, str) and len(value) == 1:
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

def _net_send(msg: dict) -> bool:
    with STATE.lock:
        s = STATE.client_sock
        channel = STATE.client_channel
    if s is None or channel is None:
        return False
    return channel.send(s, msg, send_message)


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
#  Edge detection (Windows cursor reaching its edge facing Mac)
# ═══════════════════════════════════════════════════════════════════════════════

_edge_sent = False
_edge_lock = threading.Lock()


def _on_mouse_move(x: int, y: int) -> None:
    """pynput mouse listener callback.
    Detects the Windows edge facing the configured Mac position.

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
    edge = CONFIG.get("mac_position", "left")
    user32 = ctypes.windll.user32
    left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
    width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    height = user32.GetSystemMetrics(79) # SM_CYVIRTUALSCREEN
    limits = {
        "left": x <= left + threshold,
        "right": x >= left + width - 1 - threshold,
        "top": y <= top + threshold,
        "bottom": y >= top + height - 1 - threshold,
    }

    if limits.get(edge, limits["left"]):
        with _edge_lock:
            if _edge_sent:
                return          # already sent for this crossing, skip
            _edge_sent = True

        log.info("[EDGE] Windows %s edge detected at x=%d y=%d", edge, x, y)

        # Immediately mark inactive (prevents further edge sends)
        with STATE.lock:
            STATE.active = False

        # Preserve relative position when returning to a different-sized display.
        along_start = top if edge in ("left", "right") else left
        along_extent = height if edge in ("left", "right") else width
        along = y if edge in ("left", "right") else x
        fraction = (along - along_start) / max(1, along_extent - 1)
        fraction = max(0.0, min(1.0, fraction))
        _net_send({"type": "edge", "edge": edge, "position": fraction})
        _update_tray()

    else:
        # Cursor moved away from edge -- reset debounce
        with _edge_lock:
            _edge_sent = False


# ═══════════════════════════════════════════════════════════════════════════════
#  Message handlers
# ═══════════════════════════════════════════════════════════════════════════════

def _place_cursor_at_peer_edge(edge: str, fraction: float) -> None:
    """Place the Windows cursor at the edge where it enters from the Mac."""
    try:
        user32 = ctypes.windll.user32
        left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
        width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        height = user32.GetSystemMetrics(79) # SM_CYVIRTUALSCREEN
        edge = edge if edge in ("left", "right", "top", "bottom") else "left"
        inset = max(6, int(CONFIG.get("edge_threshold", 3)) * 2 + 2)
        if edge in ("left", "right"):
            x = left + inset if edge == "left" else left + width - 1 - inset
            y = top + round(fraction * max(0, height - 1))
        else:
            x = left + round(fraction * max(0, width - 1))
            y = top + inset if edge == "top" else top + height - 1 - inset
        if not user32.SetCursorPos(x, y):
            log.warning("[CURSOR] Windows refused edge placement at %d,%d", x, y)
    except Exception as exc:
        log.warning("[CURSOR] Could not place cursor at peer edge: %s", exc)

def _handle_activate_windows(msg: dict) -> None:
    """Mac is sending input to Windows now."""
    log.info("[STATE] WINDOWS_ACTIVE")
    global _edge_sent
    with _edge_lock:
        _edge_sent = False   # fresh start -- reset debounce
    position = msg.get("position", 0.5)
    if isinstance(position, (int, float)) and not isinstance(position, bool) \
            and 0.0 <= position <= 1.0:
        _place_cursor_at_peer_edge(CONFIG.get("mac_position", "left"), float(position))
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
    if action not in ("press", "release"):
        return
    key    = _resolve_key(msg.get("key", {}))
    if key is None:
        return
    if CONFIG.get("debug"):
        log.debug("[KEY] %s %s", msg.get("key", {}).get("value", "?"), action)
    try:
        if action == "press":
            _kb.press(key)
        else:
            _kb.release(key)
    except Exception as exc:
        log.error("[KEY] Injection error: %s", exc)


def _handle_mouse_move(msg: dict) -> None:
    """Inject a relative mouse movement.

    Uses the injecting flag so that the on_mouse_move callback can identify
    programmatic movements vs. user-initiated movements (feedback-loop guard).
    """
    dx, dy = msg.get("dx"), msg.get("dy")
    if type(dx) is not int or type(dy) is not int or abs(dx) > 10000 or abs(dy) > 10000:
        return
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
    btn_name = msg.get("button")
    pressed  = msg.get("pressed")
    if btn_name not in MOUSE_BUTTON_MAP or type(pressed) is not bool:
        return
    btn = MOUSE_BUTTON_MAP[btn_name]
    try:
        if pressed:
            _ms.press(btn)
        else:
            _ms.release(btn)
    except Exception as exc:
        log.error("[MOUSE] Click injection error: %s", exc)


def _handle_mouse_scroll(msg: dict) -> None:
    """Inject a scroll wheel event."""
    dx, dy = msg.get("dx"), msg.get("dy")
    if type(dx) is not int or type(dy) is not int or abs(dx) > 1000 or abs(dy) > 1000:
        return
    try:
        _ms.scroll(dx, dy)
    except Exception as exc:
        log.error("[MOUSE] Scroll injection error: %s", exc)


def _dispatch(msg: dict) -> None:
    """Route an incoming message to the appropriate handler."""
    if not isinstance(msg, dict):
        return
    msg_type = msg.get("type")
    log.debug("[NETWORK] <- %s", msg_type)

    if msg_type == "activate_windows":
        _handle_activate_windows(msg)
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
    elif msg_type == "clipboard":
        with STATE.lock:
            enabled = STATE.clipboard_sync_enabled
        if enabled:
            _apply_remote_clipboard(msg)
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
        clipboard_preference = CONFIG.get("clipboard_sync", False)
        channel, mac_position, clipboard_enabled = server_handshake(
            conn, CONFIG["token"], clipboard_preference, send_message, recv_message)

        with STATE.lock:
            already_connected = STATE.client_sock is not None
            if not already_connected:
                STATE.client_sock = conn
                STATE.client_channel = channel
                STATE.active = False
                STATE.clipboard_sync_enabled = clipboard_enabled
        if already_connected:
            log.warning("[NETWORK] Rejecting extra client while one is paired")
            conn.close()
            return
        CONFIG["mac_position"] = mac_position
        conn.settimeout(None)
        log.info("[NETWORK] Secure pairing OK; Mac is %s of Windows", mac_position)
        if clipboard_enabled:
            _start_clipboard_sync()

    except HandshakeError as exc:
        log.warning("[NETWORK] Secure pairing rejected: %s", exc)
        try:
            conn.close()
        except OSError:
            pass
        return
    except Exception as exc:
        log.error("[NETWORK] Handshake exception: %s", exc)
        try:
            conn.close()
        except OSError:
            pass
        return

    _update_tray()
    status_message = f"Securely paired with {addr[0]}; Mac is {mac_position} of Windows."
    if clipboard_enabled:
        status_message += " Plain-text clipboard sync is active."
    else:
        status_message += " Clipboard text sharing is off; both devices must enable it."
    _notify_status("connected", status_message)

    # ── Receive loop ─────────────────────────────────────────────────────────
    while True:
        msg = channel.receive(conn, recv_message)
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
        if STATE.client_sock is conn:
            STATE.active = False
            STATE.client_sock = None
            STATE.client_channel = None
            STATE.clipboard_sync_enabled = False
    _stop_clipboard_sync()
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
_client_slots = threading.BoundedSemaphore(4)


def _serve_client_limited(conn: socket.socket, addr) -> None:
    try:
        _handle_client(conn, addr)
    finally:
        _client_slots.release()


def _run_server() -> None:
    global _server_socket
    host = CONFIG.get("host", "0.0.0.0")
    if host in ("", "0.0.0.0"):
        # Keep legacy configurations that requested all interfaces scoped to
        # the active LAN route instead of exposing Mousehare on every adapter.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 1))
            host = probe.getsockname()[0]
            if host.startswith("127."):
                host = "127.0.0.1"
        except OSError:
            host = "127.0.0.1"
        finally:
            probe.close()
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

        # Bound concurrent handshakes so unauthenticated sockets cannot spawn
        # an unlimited number of waiting threads.
        if not _client_slots.acquire(blocking=False):
            conn.close()
            continue
        threading.Thread(
            target=_serve_client_limited,
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
    _stop_clipboard_sync()
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
    with STATE.lock:
        STATE.client_sock = None
        STATE.client_channel = None
        STATE.clipboard_sync_enabled = False
        STATE.active = False
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
