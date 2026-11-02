"""Bounded PCM playback for audio received from Windows on macOS."""
from __future__ import annotations

from array import array
import base64
import binascii
import ctypes
import ctypes.util
import logging
import sys
import threading
from collections import deque

SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLES_PER_FRAME = 960
FRAME_BYTES = SAMPLES_PER_FRAME * CHANNELS * 2
MAX_QUEUED_FRAMES = 8
_BUFFER_COUNT = 3
_LOG = logging.getLogger(__name__)


class _AudioStreamBasicDescription(ctypes.Structure):
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


class _AudioQueueBuffer(ctypes.Structure):
    _fields_ = [
        ("mAudioDataBytesCapacity", ctypes.c_uint32),
        ("mAudioData", ctypes.c_void_p),
        ("mAudioDataByteSize", ctypes.c_uint32),
        ("mUserData", ctypes.c_void_p),
        ("mPacketDescriptionCapacity", ctypes.c_uint32),
        ("mPacketDescriptions", ctypes.c_void_p),
        ("mPacketDescriptionCount", ctypes.c_uint32),
    ]


_AudioQueueOutputCallback = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)
_AudioQueueRef = ctypes.c_void_p
_AudioQueueBufferRef = ctypes.c_void_p


def _load_audio_queue():
    if sys.platform != "darwin":
        raise RuntimeError("CoreAudio AudioQueue playback is only available on macOS")
    path = ctypes.util.find_library("AudioToolbox")
    if not path:
        path = "/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox"
    library = ctypes.CDLL(path)
    library.AudioQueueNewOutput.argtypes = [
        ctypes.POINTER(_AudioStreamBasicDescription),
        _AudioQueueOutputCallback,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_AudioQueueRef),
    ]
    library.AudioQueueNewOutput.restype = ctypes.c_int32
    library.AudioQueueAllocateBuffer.argtypes = [
        _AudioQueueRef, ctypes.c_uint32, ctypes.POINTER(_AudioQueueBufferRef)
    ]
    library.AudioQueueAllocateBuffer.restype = ctypes.c_int32
    library.AudioQueueEnqueueBuffer.argtypes = [
        _AudioQueueRef, _AudioQueueBufferRef, ctypes.c_uint32, ctypes.c_void_p
    ]
    library.AudioQueueEnqueueBuffer.restype = ctypes.c_int32
    library.AudioQueueStart.argtypes = [_AudioQueueRef, ctypes.c_void_p]
    library.AudioQueueStart.restype = ctypes.c_int32
    library.AudioQueueStop.argtypes = [_AudioQueueRef, ctypes.c_uint8]
    library.AudioQueueStop.restype = ctypes.c_int32
    library.AudioQueueDispose.argtypes = [_AudioQueueRef, ctypes.c_uint8]
    library.AudioQueueDispose.restype = ctypes.c_int32
    return library


def _check_status(operation: str, status: int) -> None:
    if status != 0:
        raise RuntimeError(f"{operation} failed with CoreAudio OSStatus {status}")


class PCMPlayback:
    """Play validated 20 ms stereo int16 frames on the default Mac output."""

    def __init__(self, gain: float = 0.8):
        if array("h").itemsize != 2:
            raise RuntimeError("This platform does not support 16-bit PCM samples")

        self._frames: deque[bytes] = deque()
        self._lock = threading.Lock()
        self._gain = max(0.0, min(1.0, float(gain)))
        self._closed = False
        self._reported_non_silent = False
        self._queue: _AudioQueueRef = _AudioQueueRef()
        self._buffers: list[_AudioQueueBufferRef] = []
        self._library = _load_audio_queue()
        self._callback_ref = _AudioQueueOutputCallback(self._handle_output_callback)

        audio_format = _AudioStreamBasicDescription(
            mSampleRate=float(SAMPLE_RATE),
            mFormatID=int.from_bytes(b"lpcm", "big"),
            # Signed, packed, interleaved little-endian int16 PCM.
            mFormatFlags=(1 << 2) | (1 << 3),
            mBytesPerPacket=CHANNELS * 2,
            mFramesPerPacket=1,
            mBytesPerFrame=CHANNELS * 2,
            mChannelsPerFrame=CHANNELS,
            mBitsPerChannel=16,
            mReserved=0,
        )

        try:
            _check_status(
                "AudioQueueNewOutput",
                self._library.AudioQueueNewOutput(
                    ctypes.byref(audio_format), self._callback_ref,
                    None, None, None, 0, ctypes.byref(self._queue),
                ),
            )
            for _ in range(_BUFFER_COUNT):
                buffer_ref = _AudioQueueBufferRef()
                _check_status(
                    "AudioQueueAllocateBuffer",
                    self._library.AudioQueueAllocateBuffer(
                        self._queue, FRAME_BYTES, ctypes.byref(buffer_ref)
                    ),
                )
                self._buffers.append(buffer_ref)
                buffer = ctypes.cast(
                    buffer_ref, ctypes.POINTER(_AudioQueueBuffer)
                ).contents
                ctypes.memset(buffer.mAudioData, 0, FRAME_BYTES)
                buffer.mAudioDataByteSize = FRAME_BYTES
                _check_status(
                    "AudioQueueEnqueueBuffer",
                    self._library.AudioQueueEnqueueBuffer(
                        self._queue, buffer_ref, 0, None
                    ),
                )
            _check_status(
                "AudioQueueStart",
                self._library.AudioQueueStart(self._queue, None),
            )
        except Exception:
            self._dispose_queue()
            raise

    def set_gain(self, gain: float) -> None:
        with self._lock:
            self._gain = max(0.0, min(1.0, float(gain)))

    def push_base64(self, payload: str) -> bool:
        if not isinstance(payload, str) or len(payload) != 5120:
            return False
        try:
            frame = base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error):
            return False
        if len(frame) != FRAME_BYTES:
            return False
        with self._lock:
            if self._closed:
                return False
            if len(self._frames) >= MAX_QUEUED_FRAMES:
                self._frames.popleft()
            self._frames.append(frame)
        return True

    def _apply_gain(self, frame: bytes, gain: float) -> bytes:
        if gain >= 0.999:
            return frame
        if gain <= 0.001:
            return bytes(FRAME_BYTES)
        samples = array("h")
        samples.frombytes(frame)
        if sys.byteorder != "little":
            samples.byteswap()
        for index, sample in enumerate(samples):
            samples[index] = int(sample * gain)
        if sys.byteorder != "little":
            samples.byteswap()
        return samples.tobytes()

    def _handle_output_callback(self, _user_data, queue_ref, buffer_ref) -> None:
        try:
            with self._lock:
                if self._closed:
                    return
                frame = self._frames.popleft() if self._frames else None
                gain = self._gain

            buffer = ctypes.cast(
                buffer_ref, ctypes.POINTER(_AudioQueueBuffer)
            ).contents
            if frame is None:
                ctypes.memset(buffer.mAudioData, 0, FRAME_BYTES)
            else:
                if not self._reported_non_silent and any(frame):
                    self._reported_non_silent = True
                    _LOG.info("First non-silent Windows PCM frame queued to Mac CoreAudio")
                frame = self._apply_gain(frame, gain)
                ctypes.memmove(buffer.mAudioData, frame, FRAME_BYTES)
            buffer.mAudioDataByteSize = FRAME_BYTES
            status = self._library.AudioQueueEnqueueBuffer(
                queue_ref, buffer_ref, 0, None
            )
            if status != 0:
                with self._lock:
                    closed = self._closed
                if not closed:
                    _LOG.error("AudioQueueEnqueueBuffer callback failed: %s", status)
        except Exception:
            _LOG.exception("Mac AudioQueue output callback failed")

    def _dispose_queue(self) -> None:
        queue_ref, self._queue = self._queue, _AudioQueueRef()
        if queue_ref:
            try:
                self._library.AudioQueueStop(queue_ref, 1)
            except Exception:
                pass
            try:
                self._library.AudioQueueDispose(queue_ref, 1)
            except Exception:
                pass
        self._buffers.clear()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._frames.clear()
        self._dispose_queue()
