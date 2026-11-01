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
    c_float,
    c_int,
    c_ubyte,
    c_ulong,
    c_ushort,
    c_void_p,
    c_wchar_p,
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
CLSID_PolicyConfig = GUID("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}")
IID_IPolicyConfig = GUID("{F8679F50-850A-41CF-9C72-430F290290C8}")


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_ulong)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", c_ushort),
        ("wReserved1", c_ushort),
        ("wReserved2", c_ushort),
        ("wReserved3", c_ushort),
        ("pwszVal", c_wchar_p),
        ("dummy", c_ulong * 2),
    ]


PKEY_Device_FriendlyName = PROPERTYKEY(GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)


class EndpointMuteManager:
    """Manages muting, routing, and restoring Windows render audio state.

    Guarantees:
    - Never modifies master volume dB or scalar levels; only toggles hardware endpoint mute.
    - If a virtual render device (e.g. Steam Streaming Speakers) is active, routes system output
      mix to it while muting the physical speakers so Windows output is 100% silent and Mac receives
      the complete audio stream.
    - If no virtual render sink is present, directly mutes the default physical render endpoint.
    - Logs endpoint IDs, friendly names, HRESULT of operations, and verified read-back states.
    - Records the initial mute state and default endpoint before routing.
    - Restores the exact initial mute state and original default endpoint upon opt-out, disconnect,
      capture error, or exit.
    - If Windows was already muted prior to routing, keeps Windows muted when routing ends.
    - Thread-safe and idempotent.
    - Registers an atexit hook as a failsafe against unexpected termination.
    """

    _active_physical_id: str = ""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._is_muted_by_us: bool = False
        self._initial_mute_state: bool = False
        self._has_initial_state: bool = False
        self._physical_endpoint_id: str = ""
        self._physical_endpoint_name: str = ""
        self._routed_endpoint_id: str = ""
        self._routed_endpoint_name: str = ""
        atexit.register(self.restore_mute)

    @property
    def is_muted_by_us(self) -> bool:
        with self._lock:
            return self._is_muted_by_us

    def _get_device_and_vol(self, endpoint_id: Optional[str] = None):
        """Acquire IAudioEndpointVolume interface for a specific or default render endpoint.

        Returns (endpoint_volume_ptr, vol_vtable, endpoint_id_str, friendly_name, release_callback).
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
            raise AudioDeviceError(f"CoCreateInstance(MMDeviceEnumerator) failed: {hex(hr & 0xFFFFFFFF)}")

        enum_vt = ctypes.cast(enumerator, POINTER(POINTER(c_void_p))).contents
        device = c_void_p()
        if endpoint_id:
            # Index 5: GetDevice(LPCWSTR pwstrId, IMMDevice **ppDevice)
            GetDevice = WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, POINTER(c_void_p))(enum_vt[5])
            hr = GetDevice(enumerator, endpoint_id, byref(device))
        else:
            # Index 4: GetDefaultAudioEndpoint(EDataFlow dataFlow, ERole role, IMMDevice **ppEndpoint)
            GetDefaultAudioEndpoint = WINFUNCTYPE(
                HRESULT, c_void_p, c_int, c_int, POINTER(c_void_p)
            )(enum_vt[4])
            # 0 = eRender, 0 = eConsole (default system output)
            hr = GetDefaultAudioEndpoint(enumerator, 0, 0, byref(device))

        Release_enum = WINFUNCTYPE(c_ulong, c_void_p)(enum_vt[2])
        Release_enum(enumerator)
        if hr < 0 or not device.value:
            if hr_init in (0, 1):
                ole32.CoUninitialize()
            raise AudioDeviceError(f"GetDevice/GetDefaultAudioEndpoint failed: {hex(hr & 0xFFFFFFFF)}")

        dev_vt = ctypes.cast(device, POINTER(POINTER(c_void_p))).contents
        GetId = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_wchar_p))(dev_vt[5])
        dev_id = c_wchar_p()
        GetId(device, byref(dev_id))

        name = "Unknown"
        OpenPropertyStore = WINFUNCTYPE(HRESULT, c_void_p, c_ulong, POINTER(c_void_p))(dev_vt[4])
        prop_store = c_void_p()
        if OpenPropertyStore(device, 0, byref(prop_store)) == 0 and prop_store.value:
            ps_vt = ctypes.cast(prop_store, POINTER(POINTER(c_void_p))).contents
            GetValue = WINFUNCTYPE(HRESULT, c_void_p, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(ps_vt[5])
            pv = PROPVARIANT()
            if GetValue(prop_store, byref(PKEY_Device_FriendlyName), byref(pv)) == 0:
                name = pv.pwszVal or "Unknown"
            WINFUNCTYPE(c_ulong, c_void_p)(ps_vt[2])(prop_store)

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
            raise AudioDeviceError(f"Activate(IAudioEndpointVolume) failed: {hex(hr & 0xFFFFFFFF)}")

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

        return endpoint_vol, vol_vt, dev_id.value, name, release_all

    def get_mute_details(self, endpoint_id: Optional[str] = None) -> tuple[bool, float, str, str, int]:
        """Query current Windows endpoint mute state, scalar volume, ID, name, and HRESULT."""
        target_id = endpoint_id or self._physical_endpoint_id or EndpointMuteManager._active_physical_id or None
        vol, vol_vt, dev_id, name, release_fn = self._get_device_and_vol(target_id)
        try:
            # Index 15: GetMute(BOOL *pbMute)
            GetMute = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_int))(vol_vt[15])
            # Index 9: GetMasterVolumeLevelScalar(float *pfLevel)
            GetVolume = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_float))(vol_vt[9])
            is_muted = c_int()
            hr_m = GetMute(vol, byref(is_muted))
            volume = c_float()
            hr_v = GetVolume(vol, byref(volume))
            return bool(is_muted.value), volume.value, dev_id, name, hr_m
        finally:
            release_fn()

    def get_mute(self, endpoint_id: Optional[str] = None) -> bool:
        """Query current Windows endpoint mute state (returns boolean)."""
        is_muted, _, _, _, _ = self.get_mute_details(endpoint_id)
        return is_muted

    def set_mute(self, mute: bool, endpoint_id: Optional[str] = None) -> None:
        """Set Windows endpoint mute state with verification and logging."""
        target_id = endpoint_id or self._physical_endpoint_id or EndpointMuteManager._active_physical_id or None
        vol, vol_vt, dev_id, name, release_fn = self._get_device_and_vol(target_id)
        try:
            # Index 14: SetMute(BOOL bMute, LPCGUID pguidEventContext)
            SetMute = WINFUNCTYPE(HRESULT, c_void_p, c_int, c_void_p)(vol_vt[14])
            GetMute = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_int))(vol_vt[15])
            GetVolume = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_float))(vol_vt[9])

            hr = SetMute(vol, 1 if mute else 0, None)
            is_m = c_int()
            hr_m = GetMute(vol, byref(is_m))
            v = c_float()
            hr_v = GetVolume(vol, byref(v))
            log.info("[AUDIO-MUTE] SetMute(%s) on '%s' (%s): HRESULT=%s, verified GetMute=%s, vol=%.2f",
                     mute, name, dev_id, hex(hr & 0xFFFFFFFF), bool(is_m.value), v.value)
            if hr < 0:
                raise AudioDeviceError(f"SetMute failed: {hex(hr & 0xFFFFFFFF)}")
        finally:
            release_fn()

    def find_virtual_sink(self) -> Optional[tuple[str, str]]:
        """Find active virtual/streaming audio render endpoint if present."""
        ole32 = ctypes.oledll.ole32
        hr_init = ole32.CoInitialize(None)
        enumerator = c_void_p()
        hr = ole32.CoCreateInstance(
            byref(CLSID_MMDeviceEnumerator), None, 1, byref(IID_IMMDeviceEnumerator), byref(enumerator)
        )
        if hr < 0 or not enumerator.value:
            if hr_init in (0, 1):
                ole32.CoUninitialize()
            return None

        enum_vt = ctypes.cast(enumerator, POINTER(POINTER(c_void_p))).contents
        EnumAudioEndpoints = WINFUNCTYPE(HRESULT, c_void_p, c_int, c_ulong, POINTER(c_void_p))(enum_vt[3])
        collection = c_void_p()
        # 0 = eRender, 1 = DEVICE_STATE_ACTIVE
        EnumAudioEndpoints(enumerator, 0, 1, byref(collection))
        if not collection.value:
            WINFUNCTYPE(c_ulong, c_void_p)(enum_vt[2])(enumerator)
            if hr_init in (0, 1):
                ole32.CoUninitialize()
            return None

        col_vt = ctypes.cast(collection, POINTER(POINTER(c_void_p))).contents
        GetCount = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_ulong))(col_vt[3])
        Item = WINFUNCTYPE(HRESULT, c_void_p, c_ulong, POINTER(c_void_p))(col_vt[4])
        cnt = c_ulong()
        GetCount(collection, byref(cnt))

        virtual_sink = None
        for i in range(cnt.value):
            dev = c_void_p()
            Item(collection, i, byref(dev))
            if not dev.value:
                continue
            dev_vt = ctypes.cast(dev, POINTER(POINTER(c_void_p))).contents
            GetId = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_wchar_p))(dev_vt[5])
            dev_id = c_wchar_p()
            GetId(dev, byref(dev_id))

            name = ""
            OpenPropertyStore = WINFUNCTYPE(HRESULT, c_void_p, c_ulong, POINTER(c_void_p))(dev_vt[4])
            prop_store = c_void_p()
            if OpenPropertyStore(dev, 0, byref(prop_store)) == 0 and prop_store.value:
                ps_vt = ctypes.cast(prop_store, POINTER(POINTER(c_void_p))).contents
                GetValue = WINFUNCTYPE(HRESULT, c_void_p, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(ps_vt[5])
                pv = PROPVARIANT()
                if GetValue(prop_store, byref(PKEY_Device_FriendlyName), byref(pv)) == 0:
                    name = pv.pwszVal or ""
                WINFUNCTYPE(c_ulong, c_void_p)(ps_vt[2])(prop_store)
            WINFUNCTYPE(c_ulong, c_void_p)(dev_vt[2])(dev)

            # Check if this endpoint is a known virtual / software render sink
            if "Steam Streaming Speakers" in name or "VB-Audio" in name or "Virtual Audio" in name:
                virtual_sink = (dev_id.value, name)
                break

        WINFUNCTYPE(c_ulong, c_void_p)(col_vt[2])(collection)
        WINFUNCTYPE(c_ulong, c_void_p)(enum_vt[2])(enumerator)
        if hr_init in (0, 1):
            ole32.CoUninitialize()
        return virtual_sink

    def _set_default_endpoint(self, endpoint_id: str) -> bool:
        """Set Windows default render audio endpoint using IPolicyConfig."""
        ole32 = ctypes.oledll.ole32
        hr_init = ole32.CoInitialize(None)
        policy = c_void_p()
        hr = ole32.CoCreateInstance(byref(CLSID_PolicyConfig), None, 1, byref(IID_IPolicyConfig), byref(policy))
        if hr < 0 or not policy.value:
            if hr_init in (0, 1):
                ole32.CoUninitialize()
            log.warning("[AUDIO-ROUTING] CoCreateInstance(PolicyConfig) failed: %s", hex(hr & 0xFFFFFFFF))
            return False

        pol_vt = ctypes.cast(policy, POINTER(POINTER(c_void_p))).contents
        # Index 13: SetDefaultEndpoint(LPCWSTR wszDeviceId, ERole eRole)
        SetDefaultEndpoint = WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_int)(pol_vt[13])
        hr1 = SetDefaultEndpoint(policy, endpoint_id, 0)  # eConsole
        hr2 = SetDefaultEndpoint(policy, endpoint_id, 1)  # eMultimedia
        WINFUNCTYPE(c_ulong, c_void_p)(pol_vt[2])(policy)
        if hr_init in (0, 1):
            ole32.CoUninitialize()
        log.info("[AUDIO-ROUTING] SetDefaultEndpoint to '%s': HRESULT eConsole=%s, eMultimedia=%s",
                 endpoint_id, hex(hr1 & 0xFFFFFFFF), hex(hr2 & 0xFFFFFFFF))
        return hr1 >= 0 and hr2 >= 0

    def mute_local_output(self) -> bool:
        """Mute local Windows audio output if not already muted by us."""
        with self._lock:
            if self._is_muted_by_us:
                return True

        try:
            # 1. Query physical endpoint initial mute and volume
            init_m, init_v, cur_id, cur_name, hr_m = self.get_mute_details()
            with self._lock:
                if not self._has_initial_state:
                    self._initial_mute_state = init_m
                    self._has_initial_state = True
                    self._physical_endpoint_id = cur_id
                    self._physical_endpoint_name = cur_name
                    EndpointMuteManager._active_physical_id = cur_id
                    log.info("[AUDIO-MUTE] Saved initial endpoint '%s' (%s): Muted=%s, Vol=%.2f (hr=%s)",
                             cur_name, cur_id, init_m, init_v, hex(hr_m & 0xFFFFFFFF))

            # 2. Check for virtual audio sink (e.g. Steam Streaming Speakers)
            virt = self.find_virtual_sink()
            if virt and virt[0] != cur_id:
                virt_id, virt_name = virt
                log.info("[AUDIO-MUTE] Routing output to virtual sink '%s' (%s) so Windows speakers remain silent",
                         virt_name, virt_id)
                if self._set_default_endpoint(virt_id):
                    with self._lock:
                        self._routed_endpoint_id = virt_id
                        self._routed_endpoint_name = virt_name

            # 3. Always mute physical speakers so local output is guaranteed silent
            if not init_m:
                self.set_mute(True, cur_id)
                log.info("[AUDIO-MUTE] Local Windows physical speakers '%s' temporarily muted.", cur_name)
            else:
                log.info("[AUDIO-MUTE] Local Windows physical speakers were already muted; leaving muted.")

            with self._lock:
                self._is_muted_by_us = True
            return True
        except Exception as exc:
            log.warning("[AUDIO-MUTE] Failed to configure audio routing/mute: %s", exc)
            return False

    def restore_mute(self) -> None:
        """Restore initial Windows audio endpoint mute state and default routing."""
        with self._lock:
            if not self._is_muted_by_us and not self._has_initial_state:
                return
            target_mute = self._initial_mute_state
            phys_id = self._physical_endpoint_id
            routed_id = self._routed_endpoint_id
            self._is_muted_by_us = False
            self._has_initial_state = False
            self._routed_endpoint_id = ""
            EndpointMuteManager._active_physical_id = ""

        try:
            # 1. Restore original default endpoint if routed
            if routed_id and phys_id:
                self._set_default_endpoint(phys_id)
            # 2. Restore physical mute state
            if phys_id:
                self.set_mute(target_mute, phys_id)
                log.info("[AUDIO-MUTE] Restored Windows audio mute state to initial: %s on '%s'",
                         target_mute, phys_id)
            else:
                self.set_mute(target_mute)
                log.info("[AUDIO-MUTE] Restored Windows audio mute state to initial: %s", target_mute)
        except Exception as exc:
            log.warning("[AUDIO-MUTE] Failed to restore Windows audio state: %s", exc)


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

    def _find_render_device(self, p, loopback_dev):
        """Find the render output endpoint corresponding to the loopback capture device."""
        loop_name = loopback_dev.get("name", "")
        clean_name = loop_name.replace("[Loopback]", "").strip()
        host_api = loopback_dev.get("hostApi")
        for idx in range(p.get_device_count()):
            dev = p.get_device_info_by_index(idx)
            if dev.get("hostApi") == host_api and not dev.get("isLoopbackDevice"):
                if dev.get("maxOutputChannels", 0) > 0 and clean_name in dev.get("name", ""):
                    return dev
        return None

    def _run_capture(self) -> None:
        """Worker thread: capture loopback frames and enqueue them."""
        p = None
        stream = None
        silence_stream = None
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

            # Configure audio routing and mute local output before discovering loopback endpoint
            self._mute_manager.mute_local_output()

            p = pyaudio.PyAudio()
            loopback_dev = self._find_loopback_device(p)
            dev_name = loopback_dev.get("name", "Default Loopback")
            with self._lock:
                self._device_name = dev_name
            log.info("[AUDIO] Selected WASAPI loopback device: %s (index %d)",
                     dev_name, loopback_dev.get("index"))

            # Start background silence feeder on the render endpoint to maintain continuous WASAPI packet clock
            render_dev = self._find_render_device(p, loopback_dev)
            if render_dev is not None:
                try:
                    silence_stream = p.open(
                        format=pyaudio.paInt16,
                        channels=CHANNELS,
                        rate=SAMPLE_RATE,
                        output=True,
                        output_device_index=render_dev["index"],
                        frames_per_buffer=SAMPLES_PER_FRAME_PER_CH,
                    )
                    def _feed_silence():
                        silence_buf = b"\x00" * FRAME_BYTES
                        while not self._stop_event.is_set():
                            try:
                                silence_stream.write(silence_buf)
                            except Exception:
                                break
                    silence_t = threading.Thread(target=_feed_silence, daemon=True, name="AudioSilenceFeeder")
                    silence_t.start()
                except Exception as exc:
                    log.warning("[AUDIO] Could not start silence feeder: %s", exc)

            # Open WASAPI loopback stream in shared mode
            stream = p.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                input_device_index=loopback_dev.get("index"),
                frames_per_buffer=SAMPLES_PER_FRAME_PER_CH,
            )

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
            if silence_stream is not None:
                try:
                    silence_stream.stop_stream()
                except Exception:
                    pass
                try:
                    silence_stream.close()
                except Exception:
                    pass
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
