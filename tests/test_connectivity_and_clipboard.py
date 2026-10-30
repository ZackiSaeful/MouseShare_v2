import unittest
import socket
import threading
import time

from mouseshare.secure_transport import (
    PROTOCOL_VERSION,
    HandshakeError,
    opposite_edge,
    client_handshake,
    server_handshake,
    SecureChannel,
)
from mouseshare.clipboard_sync import (
    valid_clipboard_text,
    MAX_CLIPBOARD_BYTES,
)
from mouseshare.mac.mac_app import (
    send_message as mac_send_message,
    recv_message as mac_recv_message,
    _handle_incoming as mac_handle_incoming,
)
from mouseshare.windows.windows_app import (
    _normalized_position,
    _entry_point,
    _get_local_ip,
)


class TestConnectivityAndClipboard(unittest.TestCase):

    def test_opposite_edge(self):
        self.assertEqual(opposite_edge("left"), "right")
        self.assertEqual(opposite_edge("right"), "left")
        self.assertEqual(opposite_edge("top"), "bottom")
        self.assertEqual(opposite_edge("bottom"), "top")

    def test_valid_clipboard_text(self):
        self.assertTrue(valid_clipboard_text("Hello World"))
        self.assertTrue(valid_clipboard_text("Special characters: \u2022 \u00a9 \ud83d\ude80"))
        self.assertFalse(valid_clipboard_text(12345))
        self.assertFalse(valid_clipboard_text(None))
        self.assertFalse(valid_clipboard_text(["a", "b"]))
        self.assertFalse(valid_clipboard_text("x" * (MAX_CLIPBOARD_BYTES + 10)))

    def test_secure_channel_encryption(self):
        import os
        key1 = os.urandom(32)
        key2 = os.urandom(32)
        channel_client = SecureChannel(send_key=key1, receive_key=key2)
        channel_server = SecureChannel(send_key=key2, receive_key=key1)

        # Create socket pair
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        server_conn = []
        def accept():
            conn, _ = srv.accept()
            server_conn.append(conn)

        t = threading.Thread(target=accept)
        t.start()

        cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cli.connect(("127.0.0.1", port))
        t.join()
        srv.close()
        s_sock = server_conn[0]

        try:
            # Client sends to server
            test_msg = {"type": "mouse_move", "dx": 10, "dy": -5}
            self.assertTrue(channel_client.send(cli, test_msg, mac_send_message))
            received = channel_server.receive(s_sock, mac_recv_message)
            self.assertEqual(received, test_msg)

            # Server sends to client
            test_reply = {"type": "clipboard", "text": "Synced text from Windows"}
            self.assertTrue(channel_server.send(s_sock, test_reply, mac_send_message))
            received_reply = channel_client.receive(cli, mac_recv_message)
            self.assertEqual(received_reply, test_reply)
        finally:
            cli.close()
            s_sock.close()

    def _run_handshake(self, client_token, server_token, client_clip, server_clip, mac_pos="left"):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        server_res = {}
        server_exc = []

        def run_server():
            try:
                conn, _ = srv.accept()
                ch, pos, clip = server_handshake(conn, server_token, server_clip, mac_send_message, mac_recv_message)
                server_res["channel"] = ch
                server_res["mac_position"] = pos
                server_res["clipboard_enabled"] = clip
                conn.close()
            except Exception as e:
                server_exc.append(e)

        t = threading.Thread(target=run_server)
        t.start()

        cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cli.connect(("127.0.0.1", port))
        client_res = {}
        client_exc = []
        try:
            ch, clip = client_handshake(cli, client_token, mac_pos, client_clip, mac_send_message, mac_recv_message)
            client_res["channel"] = ch
            client_res["clipboard_enabled"] = clip
        except Exception as e:
            client_exc.append(e)
        finally:
            cli.close()
            t.join()
            srv.close()

        return client_res, server_res, client_exc, server_exc

    def test_handshake_both_clipboard_enabled(self):
        c_res, s_res, c_exc, s_exc = self._run_handshake("mysecretkey", "mysecretkey", True, True)
        self.assertEqual(c_exc, [])
        self.assertEqual(s_exc, [])
        self.assertTrue(c_res["clipboard_enabled"])
        self.assertTrue(s_res["clipboard_enabled"])
        self.assertEqual(s_res["mac_position"], "left")

    def test_handshake_client_clip_true_server_false(self):
        c_res, s_res, c_exc, s_exc = self._run_handshake("mysecretkey", "mysecretkey", True, False)
        self.assertEqual(c_exc, [])
        self.assertEqual(s_exc, [])
        self.assertFalse(c_res["clipboard_enabled"])
        self.assertFalse(s_res["clipboard_enabled"])

    def test_handshake_client_clip_false_server_true(self):
        c_res, s_res, c_exc, s_exc = self._run_handshake("mysecretkey", "mysecretkey", False, True)
        self.assertEqual(c_exc, [])
        self.assertEqual(s_exc, [])
        self.assertFalse(c_res["clipboard_enabled"])
        self.assertFalse(s_res["clipboard_enabled"])

    def test_handshake_wrong_token_rejected(self):
        c_res, s_res, c_exc, s_exc = self._run_handshake("tokenA", "tokenB", True, True)
        self.assertTrue(len(c_exc) > 0 or len(s_exc) > 0)
        self.assertNotIn("channel", c_res)

    def test_normalized_position(self):
        self.assertEqual(_normalized_position(0.5), 0.5)
        self.assertEqual(_normalized_position(0), 0.0)
        self.assertEqual(_normalized_position(1.0), 1.0)
        self.assertIsNone(_normalized_position(-0.1))
        self.assertIsNone(_normalized_position(1.1))
        self.assertIsNone(_normalized_position("0.5"))
        self.assertIsNone(_normalized_position(True))

    def test_entry_point_inset(self):
        bounds = (0, 0, 1920, 1080)
        monitors = [(0, 0, 1920, 1080)]
        x, y, frac = _entry_point("left", 0.5, bounds, monitors)
        # Entry on left edge should have x inset >= 14
        self.assertGreaterEqual(x, 14)
        self.assertAlmostEqual(frac, 0.5, delta=0.01)

    def test_mac_activation_ack_handling(self):
        # Verify mac_handle_incoming handles activation_ack without error
        msg = {
            "type": "activation_ack",
            "active": True,
            "cursor_positioned": True,
            "edge": "left",
            "position": 0.5,
        }
        # Should not raise exception
        mac_handle_incoming(msg)

    def test_get_local_ip(self):
        ip = _get_local_ip()
        self.assertTrue(isinstance(ip, str) and len(ip.split(".")) == 4)

    def test_parse_connection_details(self):
        from mouseshare_app import MousehareApp
        raw = "192.168.2.100:5555\nmytoken12345"
        res = MousehareApp._parse_connection_details(raw)
        self.assertIsNotNone(res)
        host, port, token = res
        self.assertEqual(host, "192.168.2.100")
        self.assertEqual(port, 5555)
        self.assertEqual(token, "mytoken12345")

    def test_default_settings(self):
        from mouseshare_app import default_settings
        settings = default_settings()
        self.assertEqual(settings["server_host"], "")
        self.assertEqual(settings["client_port"], 5555)
        self.assertEqual(settings["server_port"], 5555)
        self.assertTrue(settings["clipboard_sync"])
        self.assertEqual(settings["mac_position"], "left")


if __name__ == "__main__":
    unittest.main()
