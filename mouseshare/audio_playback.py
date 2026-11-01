"""Audio playback engine for Mousehare macOS client.

Receives signed little-endian 16-bit 48,000 Hz stereo PCM in 20 ms frames
(960 samples/channel, 3,840 bytes, 5,120 Base64 characters), rejects malformed
or oversized frames, applies independent local stream gain (0-100), and outputs
to the macOS audio endpoint.
"""
from __future__ import annotations

import array
import base64
import logging
import queue
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2
FRAME_DURATION_MS = 20
SAMPLES_PER_FRAME_PER_CH = int(SAMPLE_RATE * (FRAME_DURATION_MS / 1000.0))  # 960
FRAME_BYTES = SAMPLES_PER_FRAME_PER_CH * CHANNELS * SAMPLE_WIDTH            # 3,840
BASE64_FRAME_LEN = ((FRAME_BYTES + 2) // 3) * 4                              # 5,120
MAX_PLAYBACK_QUEUE = 8


class AudioPlayback:
    """Manages audio playback for incoming Windows system audio stream."""

    def __init__(self, gain: int = 100) -> None:
        self._lock = threading.Lock()
        self._gain = max(0, min(100, int(gain)))
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=MAX_PLAYBACK_QUEUE)
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._is_running = False

    @property
    def gain(self) -> int:
        with self._lock:
            return self._gain

    def set_gain(self, gain: int) -> None:
        with self._lock:
            self._gain = max(0, min(100, int(gain)))

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_running and not self._stop_event.is_set()

    def start(self) -> bool:
        with self._lock:
            if self._is_running and not self._stop_event.is_set():
                return True
            self._stop_event.clear()
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._worker_thread = threading.Thread(
                target=self._run_playback, daemon=True, name="AudioPlayback"
            )
            self._is_running = True
            self._worker_thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            if not self._is_running:
                return
            self._is_running = False
            self._stop_event.set()

        thread = self._worker_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.5)

        with self._lock:
            self._worker_thread = None
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    def feed_frame(self, b64_pcm: str) -> bool:
        """Validate and enqueue one 20 ms PCM frame.

        Rejects malformed, invalid length, or oversized frames.
        Returns True if accepted and enqueued, False if rejected or dropped.
        """
        if not isinstance(b64_pcm, str) or len(b64_pcm) != BASE64_FRAME_LEN:
            log.warning("[AUDIO] Dropping invalid base64 length %s != %d",
                        len(b64_pcm) if isinstance(b64_pcm, str) else type(b64_pcm),
                        BASE64_FRAME_LEN)
            return False

        try:
            pcm_bytes = base64.b64decode(b64_pcm, validate=True)
        except Exception as exc:
            log.warning("[AUDIO] Base64 decode failed: %s", exc)
            return False

        if len(pcm_bytes) != FRAME_BYTES:
            log.warning("[AUDIO] Decoded frame size %d != %d. Rejected.", len(pcm_bytes), FRAME_BYTES)
            return False

        # Apply local gain
        with self._lock:
            gain_val = self._gain

        if gain_val == 0:
            # Muted: send silence frame
            processed = b"\x00" * FRAME_BYTES
        elif gain_val != 100:
            # Scale int16 samples
            samples = array.array("h")
            samples.frombytes(pcm_bytes)
            factor = gain_val / 100.0
            for i in range(len(samples)):
                scaled = int(samples[i] * factor)
                samples[i] = max(-32768, min(32767, scaled))
            processed = samples.tobytes()
        else:
            processed = pcm_bytes

        # Put to bounded playback queue; drop oldest on overload
        try:
            self._queue.put_nowait(processed)
            return True
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(processed)
                return True
            except queue.Full:
                return False

    def _run_playback(self) -> None:
        p = None
        stream = None
        try:
            try:
                import pyaudio
                p = pyaudio.PyAudio()
                stream = p.open(
                    format=pyaudio.paInt16,
                    channels=CHANNELS,
                    rate=SAMPLE_RATE,
                    output=True,
                    frames_per_buffer=SAMPLES_PER_FRAME_PER_CH,
                )
            except Exception as exc:
                log.info("[AUDIO] PyAudio playback output not active (%s). Running in discard mode.", exc)

            while not self._stop_event.is_set():
                try:
                    data = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue

                if stream is not None:
                    try:
                        stream.write(data)
                    except Exception as exc:
                        log.warning("[AUDIO] Output write error: %s", exc)
                        time.sleep(0.01)

        except Exception as exc:
            log.error("[AUDIO] Playback loop failed: %s", exc)
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            if p is not None:
                try:
                    p.terminate()
                except Exception:
                    pass
