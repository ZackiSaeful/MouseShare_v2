import base64
from mouseshare.audio_playback import AudioPlayback, FRAME_BYTES, BASE64_FRAME_LEN

def test_audio_playback():
    playback = AudioPlayback(gain=80)
    assert playback.gain == 80

    # Test feed valid frame
    raw_pcm = b"\x10\x00" * (FRAME_BYTES // 2)
    b64_pcm = base64.b64encode(raw_pcm).decode("ascii")
    assert len(b64_pcm) == BASE64_FRAME_LEN
    ok = playback.feed_frame(b64_pcm)
    assert ok, "Valid frame should be accepted"

    # Test reject malformed: wrong base64 length
    bad_b64 = b64_pcm[:-4]
    assert not playback.feed_frame(bad_b64), "Shorter base64 must be rejected"

    # Test reject oversized
    oversized_b64 = b64_pcm + "AAAA"
    assert not playback.feed_frame(oversized_b64), "Oversized base64 must be rejected"

    # Test reject invalid base64 characters
    corrupt_b64 = "????" + b64_pcm[4:]
    assert not playback.feed_frame(corrupt_b64), "Invalid base64 chars must be rejected"

    # Test gain adjustment
    playback.set_gain(0)
    assert playback.gain == 0
    ok = playback.feed_frame(b64_pcm)
    assert ok
    # Muted frame should be silence in queue
    queued = playback._queue.get_nowait()
    # First frame was at gain=80, second is at gain=0
    queued_muted = playback._queue.get_nowait()
    assert queued_muted == b"\x00" * FRAME_BYTES, "Gain 0 should produce silence"

    print("ALL AUDIO PLAYBACK TESTS PASSED!")

if __name__ == "__main__":
    test_audio_playback()
