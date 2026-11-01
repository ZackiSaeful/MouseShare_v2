"""Audio playback engine for Mousehare macOS client.

Receives signed little-endian 16-bit 48,000 Hz stereo PCM in 20 ms frames
(960 samples/channel, 3,840 bytes, 5,120 Base64 characters), rejects malformed
or oversized frames, applies independent local stream gain (0-100), and outputs
to the macOS audio endpoint.
"""
from __future__ import annotations

import array
import base64
import ctypes
import logging
import queue
import sys
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


def _create_macos_audio_queue_stream():
    """Create a native macOS CoreAudio AudioQueue stream using ctypes (AudioToolbox framework)."""
    try:
        audio_toolbox = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox")
    except Exception as exc:
        log.warning("[AUDIO] Could not load AudioToolbox.framework: %s", exc)
        return None

    class AudioStreamBasicDescription(ctypes.Structure):
        _fields_ = [
            ("mSampleRate", ctypes.c_double),
            ("mFormatID", ctypes.c_uint32),
            ("mFormatFlags", ctypes.c_uint32),
            ("mBytesPerPacket", ctypes.c_uint32),
            ("mFramesPerPacket", ctypes.c_uint32),
            ("mBytesPerFrame", ctypes.c_uint32),
            ("mChannelsPerFrame", ctypes.c_uint32),
            ("mBitsPerChannel", ctypes.c_uint32),
            ("mReserved", ctypes.c_uint32),
        ]

    class AudioQueueBuffer(ctypes.Structure):
        _fields_ = [
            ("mAudioDataBytesCapacity", ctypes.c_uint32),
            ("mAudioData", ctypes.c_void_p),
            ("mAudioDataByteSize", ctypes.c_uint32),
            ("mUserData", ctypes.c_void_p),
            ("mPacketDescriptionCapacity", ctypes.c_uint32),
            ("mPacketDescriptions", ctypes.c_void_p),
            ("mPacketDescriptionCount", ctypes.c_uint32),
        ]

    kAudioFormatLinearPCM = 0x6C70636D  # 'lpcm'
    kAudioFormatFlagIsSignedInteger = 4
    kAudioFormatFlagIsPacked = 8

    asbd = AudioStreamBasicDescription(
        mSampleRate=float(SAMPLE_RATE),
        mFormatID=kAudioFormatLinearPCM,
        mFormatFlags=kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked,
        mBytesPerPacket=CHANNELS * SAMPLE_WIDTH,
        mFramesPerPacket=1,
        mBytesPerFrame=CHANNELS * SAMPLE_WIDTH,
        mChannelsPerFrame=CHANNELS,
        mBitsPerChannel=16,
        mReserved=0,
    )

    AQCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(AudioQueueBuffer))

    class MacAudioQueueStream:
        def __init__(self):
            self.queue = ctypes.c_void_p()
            self._cb = AQCallback(self._on_buffer_complete)
            self._available = []
            self._lock = threading.Lock()
            self._started = False

            hr = audio_toolbox.AudioQueueNewOutput(
                ctypes.byref(asbd),
                self._cb,
                None,
                None,
                None,
                0,
                ctypes.byref(self.queue),
            )
            if hr != 0 or not self.queue.value:
                raise RuntimeError(f"AudioQueueNewOutput failed with status {hr}")

            self.buffers = []
            for _ in range(6):  # 6 * 20ms = 120ms buffer pool
                buf_ptr = ctypes.POINTER(AudioQueueBuffer)()
                hr = audio_toolbox.AudioQueueAllocateBuffer(
                    self.queue,
                    FRAME_BYTES,
                    ctypes.byref(buf_ptr),
                )
                if hr == 0 and buf_ptr:
                    self.buffers.append(buf_ptr)
                    self._available.append(buf_ptr)

        def _on_buffer_complete(self, user_data, aq, buf_ptr):
            with self._lock:
                self._available.append(buf_ptr)

        def write(self, data: bytes) -> None:
            buf_ptr = None
            for _ in range(15):
                with self._lock:
                    if self._available:
                        buf_ptr = self._available.pop(0)
                        break
                time.sleep(0.002)

            if buf_ptr is None:
                # Buffer underrun/overrun, drop frame gracefully
                return

            buf = buf_ptr.contents
            n = min(len(data), buf.mAudioDataBytesCapacity)
            ctypes.memmove(buf.mAudioData, data, n)
            buf.mAudioDataByteSize = n

            audio_toolbox.AudioQueueEnqueueBuffer(self.queue, buf_ptr, 0, None)
            if not self._started:
                audio_toolbox.AudioQueueStart(self.queue, None)
                self._started = True

        def stop(self) -> None:
            if self.queue and self.queue.value:
                try:
                    audio_toolbox.AudioQueueStop(self.queue, True)
                    audio_toolbox.AudioQueueDispose(self.queue, True)
                except Exception:
                    pass
                self.queue = None

        def close(self) -> None:
            self.stop()

    return MacAudioQueueStream()


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
            # 1. Try PyAudio backend
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
                log.info("[AUDIO] Playback active using PyAudio backend.")
            except Exception as exc:
                p = None
                stream = None

            # 2. Try sounddevice backend if PyAudio is not available
            if stream is None:
                try:
                    import sounddevice as sd
                    sd_stream = sd.RawOutputStream(
                        samplerate=SAMPLE_RATE,
                        channels=CHANNELS,
                        dtype='int16',
                        blocksize=SAMPLES_PER_FRAME_PER_CH,
                    )
                    sd_stream.start()
                    stream = sd_stream
                    log.info("[AUDIO] Playback active using sounddevice backend.")
                except Exception:
                    stream = None

            # 3. Try native macOS CoreAudio AudioQueue (AudioToolbox) backend
            if stream is None and sys.platform == "darwin":
                try:
                    stream = _create_macos_audio_queue_stream()
                    if stream is not None:
                        log.info("[AUDIO] Playback active using native macOS CoreAudio (AudioToolbox) backend.")
                except Exception as exc:
                    log.warning("[AUDIO] Native macOS CoreAudio init failed: %s", exc)
                    stream = None

            if stream is None:
                log.info("[AUDIO] No active audio output backend available. Running in discard mode.")

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
                    if hasattr(stream, "stop_stream"):
                        stream.stop_stream()
                    elif hasattr(stream, "stop"):
                        stream.stop()
                except Exception:
                    pass
                try:
                    if hasattr(stream, "close"):
                        stream.close()
                except Exception:
                    pass
            if p is not None:
                try:
                    p.terminate()
                except Exception:
                    pass
