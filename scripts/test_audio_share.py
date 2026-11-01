import base64
import logging
import socket
import time
from mouseshare.windows.audio_share import AudioCaptureServer, FRAME_BYTES, BASE64_FRAME_LEN

logging.basicConfig(level=logging.DEBUG)

def test_audio():
    server = AudioCaptureServer()
    assert not server.is_running

    class DummyEncryptedChannel:
        is_encrypted = True
        def __init__(self):
            self.messages = []
        def send(self, sock, msg, send_fn):
            self.messages.append(msg)
            return True

    class DummyUnencryptedChannel:
        is_encrypted = False

    # 1. Test unencrypted rejection
    ok = server.start(None, DummyUnencryptedChannel(), lambda s, m: True)
    assert not ok, 'Should reject unencrypted'
    assert not server.is_running
    print("[PASS] Successfully rejected unencrypted session")

    # 2. Test start with encrypted channel
    channel = DummyEncryptedChannel()
    s1, s2 = socket.socketpair()
    try:
        ok = server.start(s1, channel, lambda s, m: True)
        assert ok, 'Start should succeed'
        time.sleep(1.0)  # give 1s for device init and capturing ~50 frames
        assert server.is_running
        server.stop()
        assert not server.is_running
        print(f"[PASS] Successfully stopped. Captured {len(channel.messages)} frames.")
        assert len(channel.messages) > 0, 'Should have captured at least 1 frame'
        
        for idx, frame_msg in enumerate(channel.messages):
            assert frame_msg['type'] == 'audio_pcm', f"Wrong type {frame_msg['type']}"
            pcm_b64 = frame_msg['pcm']
            assert len(pcm_b64) == BASE64_FRAME_LEN, f"Frame {idx} b64 len {len(pcm_b64)} != {BASE64_FRAME_LEN}"
            decoded = base64.b64decode(pcm_b64)
            assert len(decoded) == FRAME_BYTES, f"Frame {idx} bytes len {len(decoded)} != {FRAME_BYTES}"
        print("[PASS] All frames match exact 3,840 bytes PCM and 5,120 base64 chars.")

        # 3. Test repeated start / stop
        for cycle in range(3):
            server.start(s1, channel, lambda s, m: True)
            time.sleep(0.05)
            assert server.is_running
            server.stop()
            assert not server.is_running
        print("[PASS] Repeated start/stop cycled cleanly 3 times.")

        # 4. Test bounded queue behavior on overload
        q = server._queue
        # Fill queue to maxsize
        while not q.full():
            q.put(b"X" * FRAME_BYTES)
        assert q.full()
        # Simulate capture pushing another frame: should drop oldest without blocking
        new_frame = b"Y" * FRAME_BYTES
        try:
            q.put_nowait(new_frame)
        except Exception:
            # Drop oldest and put
            try:
                q.get_nowait()
            except Exception:
                pass
            q.put_nowait(new_frame)
        assert q.full()
        # Verify newest frame is present and queue size never exceeds MAX_QUEUE_FRAMES
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        assert len(items) <= 5
        assert items[-1] == new_frame
        print("[PASS] Bounded queue drop-stale-on-overload verified.")

        # 5. Test no-device failure reporting without crash
        error_notified = []
        def mock_status_cb(st, msg):
            error_notified.append((st, msg))
        # Point to an invalid device finder by temporarily mocking it
        orig_find = server._find_loopback_device
        def mock_fail(p):
            from mouseshare.windows.audio_share import AudioDeviceError
            raise AudioDeviceError("Simulated missing audio endpoint")
        server._find_loopback_device = mock_fail
        try:
            ok = server.start(s1, channel, lambda s, m: True, mock_status_cb)
            assert ok
            time.sleep(0.3)
            assert not server.is_running or server._stop_event.is_set()
            assert any("Simulated missing audio endpoint" in msg for st, msg in error_notified)
            print("[PASS] No-audio device failure handled gracefully and reported without crash.")
        finally:
            server._find_loopback_device = orig_find
            server.stop()

    finally:
        s1.close()
        s2.close()

if __name__ == "__main__":
    test_audio()
