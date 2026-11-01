"""Windows WASAPI Loopback Audio Sharing for Mousehare.

Captures Windows system output mix using WASAPI loopback and transmits
signed 16-bit little-endian stereo PCM at 48,000 Hz in 20 ms frames (960 samples/ch,
3,840 bytes per frame, 5,120 Base64 characters) over the authenticated,
encrypted Mousehare SecureChannel.
"""
from __future__ import annotations

import atexit
import base64
import ctypes
import logging
import queue
import socket
import threading
import time
import uuid
from ctypes import (
    POINTER,
    WINFUNCTYPE,
    HRESULT,
    byref,
    c_int,
    c_ubyte,
    c_ulong,
    c_ushort,
    c_void_p,
)
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Wire specification constants
SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2  # 16-bit signed PCM (int16)
FRAME_DURATION_MS = 20
SAMPLES_PER_FRAME_PER_CH = int(SAMPLE_RATE * (FRAME_DURATION_MS / 1000.0))  # 960
FRAME_BYTES = SAMPLES_PER_FRAME_PER_CH * CHANNELS * SAMPLE_WIDTH            # 3,840 bytes
BASE64_FRAME_LEN = ((FRAME_BYTES + 2) // 3) * 4                              # 5,120 chars

# Small bounded queue to preserve real-time low latency and drop stale frames on overload
MAX_QUEUE_FRAMES = 5  # 100 ms max buffered audio


class AudioDeviceError(RuntimeError):
    """Raised when WASAPI loopback endpoint cannot be discovered or initialized."""


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", c_ulong),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", c_ubyte * 8),
    ]

    def __init__(self, guid_str: str) -> None:
        super().__init__()
        u = uuid.UUID(guid_str)
        self.Data1 = u.time_low
        self.Data2 = u.time_mid
        self.Data3 = u.time_hi_version
        self.Data4 = (c_ubyte * 8)(*u.bytes[8:])


CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioEndpointVolume = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")


class EndpointMuteManager:
    """Manages muting and restoring the Windows default render endpoint volume.

    Guarantees:
    - Never modifies master volume dB or scalar levels; only toggles hardware endpoint mute.
    - Records the initial mute state before muting.
    - Restores the exact initial mute state upon opt-out, disconnect, capture error, or exit.
    - If Windows was already muted prior to routing, keeps Windows muted when routing ends.
    - Thread-safe and idempotent.
    - Registers an atexit hook as a failsafe against unexpected termination.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._is_muted_by_us: bool = False
        self._initial_mute_state: bool = False
        self._has_initial_state: bool = False
        atexit.register(self.restore_mute)

    @property
    def is_muted_by_us(self) -> bool:
        with self._lock:
            return self._is_muted_by_us

    def _get_endpoint_volume_interface(self):
        """Acquire the IAudioEndpointVolume interface for default render endpoint.

        Returns (endpoint_volume_ptr, vol_vtable, release_callback).
        Caller MUST invoke release_callback() in a finally block.
        """
        ole32 = ctypes.oledll.ole32
        hr_init = ole32.CoInitialize(None)

        enumerator = c_void_p()
        hr = ole32.CoCreateInstance(
            byref(CLSID_MMDeviceEnumerator),
            None,
            1,  # CLSCTX_INPROC_SERVER
            byref(IID_IMMDeviceEnumerator),
            byref(enumerator),
        )
        if hr < 0 or not enumerator.value:
            if hr_init in (0, 1):
                ole32.CoUninitialize()
            raise AudioDeviceError(f"CoCreateInstance(MMDeviceEnumerator) failed: {hex(hr)}")

        enum_vt = ctypes.cast(enumerator, POINTER(POINTER(c_void_p))).contents
        # Index 4: GetDefaultAudioEndpoint(EDataFlow dataFlow, ERole role, IMMDevice **ppEndpoint)
        GetDefaultAudioEndpoint = WINFUNCTYPE(
            HRESULT, c_void_p, c_int, c_int, POINTER(c_void_p)
        )(enum_vt[4])
        # Index 2: Release
        Release_enum = WINFUNCTYPE(c_ulong, c_void_p)(enum_vt[2])

        device = c_void_p()
        # 0 = eRender, 0 = eConsole (default system output)
        hr = GetDefaultAudioEndpoint(enumerator, 0, 0, byref(device))
        Release_enum(enumerator)
        if hr < 0 or not device.value:
            if hr_init in (0, 1):
                ole32.CoUninitialize()
            raise AudioDeviceError(f"GetDefaultAudioEndpoint failed: {hex(hr)}")

        dev_vt = ctypes.cast(device, POINTER(POINTER(c_void_p))).contents
        # Index 3: Activate(REFIID, DWORD, PROPVARIANT*, void**)
        Activate = WINFUNCTYPE(
            HRESULT, c_void_p, POINTER(GUID), c_ulong, c_void_p, POINTER(c_void_p)
        )(dev_vt[3])
        Release_dev = WINFUNCTYPE(c_ulong, c_void_p)(dev_vt[2])

        endpoint_vol = c_void_p()
        hr = Activate(device, byref(IID_IAudioEndpointVolume), 1 | 2 | 4 | 16, None, byref(endpoint_vol))
        Release_dev(device)
        if hr < 0 or not endpoint_vol.value:
            if hr_init in (0, 1):
                ole32.CoUninitialize()
            raise AudioDeviceError(f"Activate(IAudioEndpointVolume) failed: {hex(hr)}")

        vol_vt = ctypes.cast(endpoint_vol, POINTER(POINTER(c_void_p))).contents
        Release_vol = WINFUNCTYPE(c_ulong, c_void_p)(vol_vt[2])

        def release_all():
            try:
                Release_vol(endpoint_vol)
            except Exception:
                pass
            if hr_init in (0, 1):
                try:
                    ole32.CoUninitialize()
                except Exception:
                    pass

        return endpoint_vol, vol_vt, release_all

    def get_mute(self) -> bool:
        """Query current Windows endpoint mute state."""
        vol, vol_vt, release_fn = self._get_endpoint_volume_interface()
        try:
            # Index 15: GetMute(BOOL *pbMute)
            GetMute = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_int))(vol_vt[15])
            is_muted = c_int()
            hr = GetMute(vol, byref(is_muted))
            if hr < 0:
                raise AudioDeviceError(f"GetMute failed: {hex(hr)}")
            return bool(is_muted.value)
        finally:
            release_fn()

    def set_mute(self, mute: bool) -> None:
        """Set Windows endpoint mute state."""
        vol, vol_vt, release_fn = self._get_endpoint_volume_interface()
        try:
            # Index 14: SetMute(BOOL bMute, LPCGUID pguidEventContext)
            SetMute = WINFUNCTYPE(HRESULT, c_void_p, c_int, c_void_p)(vol_vt[14])
            hr = SetMute(vol, 1 if mute else 0, None)
            if hr < 0:
                raise AudioDeviceError(f"SetMute failed: {hex(hr)}")
        finally:
            release_fn()

    def mute_local_output(self) -> bool:
        """Mute local Windows audio output if not already muted by us."""
        with self._lock:
            if self._is_muted_by_us:
                return True
            try:
                current_mute = self.get_mute()
                if not self._has_initial_state:
                    self._initial_mute_state = current_mute
                    self._has_initial_state = True
                    log.info("[AUDIO-MUTE] Saved initial Windows audio mute state: %s", current_mute)

                if not current_mute:
                    self.set_mute(True)
                    log.info("[AUDIO-MUTE] Local Windows audio output temporarily muted.")
                else:
                    log.info("[AUDIO-MUTE] Local Windows audio was already muted; leaving muted.")
                self._is_muted_by_us = True
                return True
            except Exception as exc:
                log.warning("[AUDIO-MUTE] Failed to mute local Windows audio: %s", exc)
                return False

    def restore_mute(self) -> None:
        """Restore initial Windows audio endpoint mute state."""
        with self._lock:
            if not self._is_muted_by_us and not self._has_initial_state:
                return
            target_mute = self._initial_mute_state
            self._is_muted_by_us = False
            self._has_initial_state = False

        try:
            self.set_mute(target_mute)
            log.info("[AUDIO-MUTE] Restored Windows audio mute state to initial: %s", target_mute)
        except Exception as exc:
            log.warning("[AUDIO-MUTE] Failed to restore Windows audio mute state: %s", exc)


class AudioCaptureServer:
    """Manages WASAPI loopback audio capture and transmission over SecureChannel.

    Guarantees:
    - No audio device is opened or started on import or when disabled.
    - Captures system output mix only; never captures microphone.
    - Never changes Windows system or application volume.
    - Mutes local Windows output only while actively routing audio to Mac.
    - Restores Windows mute state promptly on stop, opt-out, disconnect, or exit.
    - Dedicated capture and send workers; never blocks mouse/keyboard injection.
    - Small bounded queue: drops stale frames on congestion.
    - Fails gracefully without crashing the server.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._send_thread: Optional[threading.Thread] = None
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=MAX_QUEUE_FRAMES)
        self._is_running = False
        self._mute_manager = EndpointMuteManager()

        self._sock: Optional[socket.socket] = None
        self._channel = None
        self._send_fn: Optional[Callable] = None
        self._status_cb: Optional[Callable[[str, str], None]] = None
        self._device_name: str = ""

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_running and not self._stop_event.is_set()

    @property
    def is_muted_locally(self) -> bool:
        return self._mute_manager.is_muted_by_us

    @property
    def mute_manager(self) -> EndpointMuteManager:
        return self._mute_manager

    @property
    def device_name(self) -> str:
        with self._lock:
            return self._device_name

    def start(
        self,
        sock: socket.socket,
        channel,
        send_message_fn: Callable,
        status_callback: Optional[Callable[[str, str], None]] = None,
    ) -> bool:
        """Start WASAPI loopback capture and transmission if not already active."""
        with self._lock:
            if self._is_running and not self._stop_event.is_set():
                return True

            if channel is None or not getattr(channel, "is_encrypted", False):
                log.warning("[AUDIO] Refusing audio start: session is not encrypted.")
                return False

            self._sock = sock
            self._channel = channel
            self._send_fn = send_message_fn
            self._status_cb = status_callback
            self._stop_event.clear()

            # Drain any residual frames
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

            self._capture_thread = threading.Thread(
                target=self._run_capture,
                daemon=True,
                name="AudioWASAPICapture",
            )
            self._send_thread = threading.Thread(
                target=self._run_send,
                daemon=True,
                name="AudioSecureSender",
            )
            self._is_running = True
            self._capture_thread.start()
            self._send_thread.start()
            log.info("[AUDIO] Audio capture and send workers started.")
            return True

    def stop(self) -> None:
        """Promptly stop audio capture and send workers and restore local audio."""
        with self._lock:
            if not self._is_running:
                self._mute_manager.restore_mute()
                return
            self._is_running = False
            self._stop_event.set()

        # Wake up sender by putting a dummy sentinel if needed
        # (send loop also checks timeout)
        cap_thread = self._capture_thread
        snd_thread = self._send_thread

        if cap_thread and cap_thread.is_alive() and cap_thread is not threading.current_thread():
            cap_thread.join(timeout=0.6)
        if snd_thread and snd_thread.is_alive() and snd_thread is not threading.current_thread():
            snd_thread.join(timeout=0.6)

        # Promptly restore Windows endpoint volume mute state
        self._mute_manager.restore_mute()

        with self._lock:
            self._capture_thread = None
            self._send_thread = None
            self._sock = None
            self._channel = None
            self._send_fn = None
            # Drain queue
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

        log.info("[AUDIO] Audio capture stopped and local audio restored.")

    def _notify(self, status: str, message: str) -> None:
        cb = self._status_cb
        if cb is not None:
            try:
                cb(status, message)
            except Exception:
                log.exception("[AUDIO] Status callback failed")

    def _find_loopback_device(self, p):
        """Locate the default WASAPI render loopback endpoint."""
        wasapi_type = getattr(p, "paWASAPI", None)
        if wasapi_type is None:
            import pyaudiowpatch as _paw
            wasapi_type = getattr(_paw, "paWASAPI", 3)
        try:
            wasapi_info = p.get_host_api_info_by_type(wasapi_type)
        except Exception:
            # Look up WASAPI host API index by iterating
            wasapi_info = None
            for idx in range(p.get_host_api_count()):
                api_info = p.get_host_api_info_by_index(idx)
                if "WASAPI" in api_info.get("name", "").upper():
                    wasapi_info = api_info
                    break

        if wasapi_info is None:
            raise AudioDeviceError("Windows Audio Session API (WASAPI) is not available.")

        default_out_idx = wasapi_info.get("defaultOutputDevice")
        if default_out_idx is None or default_out_idx < 0:
            raise AudioDeviceError("No default Windows audio output device is enabled.")

        default_speakers = p.get_device_info_by_index(default_out_idx)
        spk_name = default_speakers.get("name", "")

        # Check if default speakers itself is marked as loopback
        if default_speakers.get("isLoopbackDevice"):
            return default_speakers

        # Match loopback device by name
        matched_loopback = None
        if hasattr(p, "get_loopback_device_info_generator"):
            for dev in p.get_loopback_device_info_generator():
                if spk_name and spk_name in dev.get("name", ""):
                    matched_loopback = dev
                    break
            if matched_loopback is None:
                # Fallback to the first loopback device
                for dev in p.get_loopback_device_info_generator():
                    matched_loopback = dev
                    break
        else:
            # Fallback for standard device enumeration
            for idx in range(p.get_device_count()):
                dev = p.get_device_info_by_index(idx)
                if dev.get("hostApi") == wasapi_info.get("index") and dev.get("isLoopbackDevice"):
                    if spk_name and spk_name in dev.get("name", ""):
                        matched_loopback = dev
                        break
                    if matched_loopback is None:
                        matched_loopback = dev

        if matched_loopback is None:
            raise AudioDeviceError(
                f"No WASAPI loopback device found for output endpoint '{spk_name}'."
            )
        return matched_loopback

    def _run_capture(self) -> None:
        """Worker thread: capture loopback frames and enqueue them."""
        p = None
        stream = None
        try:
            try:
                import pyaudiowpatch as pyaudio
            except ImportError:
                try:
                    import pyaudio
                except ImportError as exc:
                    raise AudioDeviceError(
                        "WASAPI audio capture library (pyaudiowpatch) is not installed."
                    ) from exc

            p = pyaudio.PyAudio()
            loopback_dev = self._find_loopback_device(p)
            dev_name = loopback_dev.get("name", "Default Loopback")
            with self._lock:
                self._device_name = dev_name
            log.info("[AUDIO] Selected WASAPI loopback device: %s (index %d)",
                     dev_name, loopback_dev.get("index"))

            # Open WASAPI loopback stream in shared mode
            stream = p.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                input_device_index=loopback_dev.get("index"),
                frames_per_buffer=SAMPLES_PER_FRAME_PER_CH,
            )

            # Stream successfully opened: mute local Windows output so sound plays ONLY on Mac
            self._mute_manager.mute_local_output()

            buffer = bytearray()
            while not self._stop_event.is_set():
                try:
                    # Read 960 frames (20 ms at 48 kHz stereo int16 = 3,840 bytes)
                    raw = stream.read(SAMPLES_PER_FRAME_PER_CH, exception_on_overflow=False)
                    if not raw:
                        time.sleep(0.005)
                        continue

                    buffer.extend(raw)
                    while len(buffer) >= FRAME_BYTES:
                        frame = bytes(buffer[:FRAME_BYTES])
                        del buffer[:FRAME_BYTES]

                        # Non-blocking put to bounded queue; drop oldest on overload
                        try:
                            self._queue.put_nowait(frame)
                        except queue.Full:
                            try:
                                self._queue.get_nowait()
                            except queue.Empty:
                                pass
                            try:
                                self._queue.put_nowait(frame)
                            except queue.Full:
                                pass
                except (OSError, IOError) as exc:
                    if self._stop_event.is_set():
                        break
                    log.warning("[AUDIO] Loopback read glitch: %s", exc)
                    time.sleep(0.01)

        except AudioDeviceError as exc:
            log.error("[AUDIO] Device error: %s", exc)
            self._notify("audio_device_error", f"Audio device error: {exc}")
            self._stop_event.set()
        except Exception as exc:
            log.error("[AUDIO] Capture loop failed: %s", exc, exc_info=True)
            self._notify("audio_device_error", f"Audio capture error: {exc}")
            self._stop_event.set()
        finally:
            self._mute_manager.restore_mute()
            if stream is not None:
                try:
                    stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            if p is not None:
                try:
                    p.terminate()
                except Exception:
                    pass
            log.debug("[AUDIO] Capture worker thread exiting.")

    def _run_send(self) -> None:
        """Worker thread: dequeue 20 ms frames and transmit over SecureChannel."""
        log.debug("[AUDIO] Send worker thread started.")
        while not self._stop_event.is_set():
            try:
                frame = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue

            # Validate wire frame constraints: signed int16, 48kHz, 2ch, 20ms -> 3,840 bytes
            if len(frame) != FRAME_BYTES:
                log.warning("[AUDIO] Invalid frame size %d != %d. Dropped.", len(frame), FRAME_BYTES)
                continue

            b64_payload = base64.b64encode(frame).decode("ascii")
            if len(b64_payload) != BASE64_FRAME_LEN:
                log.warning("[AUDIO] Invalid base64 length %d != %d. Dropped.", len(b64_payload), BASE64_FRAME_LEN)
                continue

            msg = {
                "type": "audio_pcm",
                "pcm": b64_payload,
            }

            sock = self._sock
            channel = self._channel
            send_fn = self._send_fn
            if sock is None or channel is None or send_fn is None:
                log.warning("[AUDIO] Missing sock/channel/send_fn: sock=%s, channel=%s, send_fn=%s",
                            sock, channel, send_fn)
                break

            try:
                ok = channel.send(sock, msg, send_fn)
                if not ok:
                    log.warning("[AUDIO] SecureChannel send failed. Stopping audio worker.")
                    break
            except Exception as exc:
                log.error("[AUDIO] Socket send error: %s", exc)
                break

        # If send worker stopped while stop event was not set, trigger stop
        if not self._stop_event.is_set():
            log.debug("[AUDIO] Send worker setting stop event because loop broke.")
            self._stop_event.set()
        log.debug("[AUDIO] Send worker thread exiting.")
