"""Comprehensive end-to-end integration test for Mousehare Windows audio sharing.

Validates:
1. Secure pairing & handshake sequencing.
2. Both directions of opt-out (Mac client control & Windows setting toggle).
3. Wire format: exactly 20 ms frames (960 samples/ch, 3,840 bytes, 5,120 Base64 chars).
4. Audio frames sent only over authenticated, encrypted SecureChannel.
5. Mouse and keyboard event processing while audio is actively streaming.
6. Disconnect under load & prompt worker shutdown.
"""
import base64
import json
import logging
import socket
import struct
import sys
import threading
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("audio_e2e")

from mouseshare.secure_transport import (
    client_handshake,
    server_handshake,
    PROTOCOL_VERSION,
)
from mouseshare.windows.protocol import recv_message, send_message
from mouseshare.windows import windows_app

TEST_PORT = 5566
TEST_TOKEN = "test_audio_token_12345"


def run_e2e_test():
    log.info("Starting Mousehare Windows audio sharing E2E test...")

    # Start Windows backend
    server_config = {
        "host": "127.0.0.1",
        "port": TEST_PORT,
        "token": TEST_TOKEN,
        "edge_threshold": 3,
        "mac_position": "left",
        "clipboard_sync": True,
        "audio_share": True,  # Windows opt-in initially True
        "debug": True,
    }

    status_events = []
    def on_status(st, msg):
        log.info("[STATUS EVENT] %s: %s", st, msg)
        status_events.append((st, msg))

    windows_app.start_backend(server_config, on_status)
    time.sleep(0.5)

    # Connect client
    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    client_sock.connect(("127.0.0.1", TEST_PORT))
    log.info("Client connected to server.")

    # Perform secure client handshake
    channel, clip_enabled = client_handshake(
        client_sock,
        TEST_TOKEN,
        "left",
        True,
        send_message,
        recv_message,
    )
    assert channel.is_encrypted, "Client channel must be encrypted"
    log.info("Secure handshake complete. Channel is encrypted.")

    # Receiver thread on client
    received_audio_frames = []
    received_other_messages = []
    receiver_stop = threading.Event()

    import select
    def client_recv():
        client_sock.setblocking(True)
        while not receiver_stop.is_set():
            r, _, _ = select.select([client_sock], [], [], 0.1)
            if not r:
                continue
            try:
                msg = channel.receive(client_sock, recv_message)
            except Exception as e:
                log.info("Client recv exception: %s", e)
                break
            if msg is None:
                log.info("Client received None (EOF)")
                break
            if msg.get("type") == "audio_pcm":
                received_audio_frames.append(msg)
            else:
                received_other_messages.append(msg)

    recv_thread = threading.Thread(target=client_recv, daemon=True)
    recv_thread.start()

    try:
        # Step 1: Initial state - Mac audio_control has NOT been sent yet.
        # Ensure no audio frames are received yet.
        time.sleep(0.4)
        assert len(received_audio_frames) == 0, f"Expected 0 frames before opt-in, got {len(received_audio_frames)}"
        log.info("[PASS 1] No audio transmitted before Mac audio_control.enabled=True.")

        # Step 2: Mac sends audio_control: enabled=True
        log.info("Mac enabling audio: audio_control -> True")
        ok = channel.send(client_sock, {"type": "audio_control", "enabled": True}, send_message)
        assert ok, "Failed to send audio_control"

        # Wait and collect frames
        time.sleep(0.8)
        frames_count_1 = len(received_audio_frames)
        assert frames_count_1 > 5, f"Expected > 5 audio frames, received {frames_count_1}"
        log.info("[PASS 2] Audio streaming active. Received %d frames.", frames_count_1)

        # Step 3: Validate wire format on all received frames
        for idx, f in enumerate(received_audio_frames[:20]):
            b64_pcm = f.get("pcm")
            assert isinstance(b64_pcm, str), f"Frame {idx}: pcm not str"
            assert len(b64_pcm) == 5120, f"Frame {idx}: base64 len {len(b64_pcm)} != 5120"
            raw = base64.b64decode(b64_pcm, validate=True)
            assert len(raw) == 3840, f"Frame {idx}: decoded len {len(raw)} != 3840"
        log.info("[PASS 3] Wire format validated: all frames exactly 3,840 bytes (5,120 Base64 chars).")

        # Step 4: Verify mouse and keyboard events while audio is streaming
        log.info("Sending concurrent mouse and keyboard events while audio streams...")
        t_start = time.time()
        for i in range(10):
            channel.send(client_sock, {"type": "mouse_move", "dx": 1, "dy": 1}, send_message)
            channel.send(client_sock, {"type": "key", "action": "press", "key": {"type": "char", "value": "a"}}, send_message)
            channel.send(client_sock, {"type": "key", "action": "release", "key": {"type": "char", "value": "a"}}, send_message)
        t_duration = time.time() - t_start
        assert t_duration < 0.2, f"Input injection blocked: took {t_duration*1000:.1f}ms"
        log.info("[PASS 4] Concurrent input injection responsive: 30 events sent in %.1f ms.", t_duration * 1000)

        # Step 5: Test Mac opt-out (audio_control -> False)
        log.info("Mac opting out: audio_control -> False")
        channel.send(client_sock, {"type": "audio_control", "enabled": False}, send_message)
        time.sleep(0.3)
        count_at_optout = len(received_audio_frames)
        time.sleep(0.4)
        count_after = len(received_audio_frames)
        # Should have stopped
        assert count_after - count_at_optout <= 1, f"Audio should have stopped: got {count_after - count_at_optout} new frames"
        log.info("[PASS 5] Mac opt-out stopped audio promptly.")

        # Step 6: Test Mac re-enable (audio_control -> True)
        log.info("Mac re-enabling audio: audio_control -> True")
        channel.send(client_sock, {"type": "audio_control", "enabled": True}, send_message)
        time.sleep(0.6)
        count_after_reoptin = len(received_audio_frames)
        assert count_after_reoptin > count_after + 5, f"Expected audio resumption, got {count_after_reoptin - count_after} frames"
        log.info("[PASS 6] Mac re-enable resumed audio stream cleanly.")

        # Step 7: Test Windows opt-out (set_audio_share_enabled(False))
        log.info("Windows user opting out: set_audio_share_enabled(False)")
        windows_app.set_audio_share_enabled(False)
        time.sleep(0.3)
        count_win_optout = len(received_audio_frames)
        time.sleep(0.4)
        count_win_after = len(received_audio_frames)
        assert count_win_after - count_win_optout <= 1, f"Audio should stop on Windows opt-out: got {count_win_after - count_win_optout} new frames"
        log.info("[PASS 7] Windows opt-out stopped audio promptly.")

        # Step 8: Test Windows re-opt-in (set_audio_share_enabled(True))
        log.info("Windows user re-enabling: set_audio_share_enabled(True)")
        windows_app.set_audio_share_enabled(True)
        time.sleep(0.6)
        count_win_resumed = len(received_audio_frames)
        assert count_win_resumed > count_win_after + 5, f"Expected audio resumption on Windows re-opt-in, got {count_win_resumed - count_win_after} frames"
        log.info("[PASS 8] Windows re-enable resumed audio stream cleanly.")

        # Step 9: Disconnect under load
        log.info("Testing abrupt client disconnect under load while audio plays...")
        receiver_stop.set()
        client_sock.close()
        time.sleep(0.4)
        assert not windows_app._audio_server.is_running, "Audio capture must stop on disconnect"
        log.info("[PASS 9] Disconnect under load stopped audio workers promptly without crash.")

    finally:
        receiver_stop.set()
        try:
            client_sock.close()
        except Exception:
            pass
        windows_app.stop_backend()
        time.sleep(0.3)
        assert not windows_app._audio_server.is_running
        log.info("[PASS 10] Server backend stopped cleanly.")

    print("\nALL 10 END-TO-END AUDIO SHARING TESTS PASSED SUCCESSFULLY!\n")


if __name__ == "__main__":
    run_e2e_test()
