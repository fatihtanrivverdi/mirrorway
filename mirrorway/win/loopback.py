"""Capturing the sound that plays on Windows (WASAPI loopback) and muting Windows' speakers meanwhile (3.4).

Two ways, both with plain ctypes (COM by vtable index, no extra packages):
  - "process" (Windows 10 build 20348 / Windows 11 and later): process loopback, the sound of every app except
    Mirrorway's own process tree, delivered in the format we ask for (48 kHz stereo 16-bit). It is taken from the
    apps before Windows mixes it for the speakers, so the speakers can be muted while the sound plays on the Mac
    (CI checks this with a virtual sound card).
  - "endpoint" (older Windows): the default speakers' loopback, everything that plays there, in the mixer's format
    (converted to 16-bit). Muting the speakers would silence it too, so they stay on.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import uuid
from ctypes import POINTER, byref, c_int, c_int64, c_long, c_uint16, c_uint32, c_uint64, c_void_p
from typing import Callable, Optional

from .. import audio as A

log = logging.getLogger("mirrorway.audio")

_FN = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

S_OK = 0
E_NOINTERFACE = -2147467262                 # 0x80004002
COINIT_MULTITHREADED = 0
CLSCTX_ALL = 0x17
VT_BLOB = 65
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
PROCESS_LOOPBACK = 1                        # AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
EXCLUDE_TARGET_PROCESS_TREE = 1
INCLUDE_TARGET_PROCESS_TREE = 0
E_RENDER, E_CONSOLE = 0, 0
WAIT_TIMEOUT = 0x102
PROCESS_LOOPBACK_DEVICE = "VAD\\Process_Loopback"
PROCESS_LOOPBACK_BUILD = 20348              # the first Windows build with process loopback
BUFFER_HNS = 200000                         # 20 ms (100 ns units), as in Microsoft's process loopback sample


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_uint32), ("Data2", c_uint16), ("Data3", c_uint16), ("Data4", ctypes.c_ubyte * 8)]


def guid(s: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(s).bytes_le)


IID_IUnknown = guid("00000000-0000-0000-C000-000000000046")
IID_IAgileObject = guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")
IID_IActivateAudioInterfaceCompletionHandler = guid("41D949AB-9862-444A-80F6-C261334DA5EB")
IID_IAudioClient = guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_IAudioCaptureClient = guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
IID_IMMDeviceEnumerator = guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
CLSID_MMDeviceEnumerator = guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IAudioEndpointVolume = guid("5CDF2C82-841E-4546-9722-0CF74078229A")


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", c_uint16), ("nChannels", c_uint16), ("nSamplesPerSec", c_uint32),
                ("nAvgBytesPerSec", c_uint32), ("nBlockAlign", c_uint16), ("wBitsPerSample", c_uint16),
                ("cbSize", c_uint16)]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("Format", WAVEFORMATEX), ("wValidBitsPerSample", c_uint16), ("dwChannelMask", c_uint32),
                ("SubFormat", GUID)]


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", c_uint32), ("ProcessLoopbackMode", c_int)]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", c_int), ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS)]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", c_uint32), ("pBlobData", c_void_p)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", c_uint16), ("r1", c_uint16), ("r2", c_uint16), ("r3", c_uint16), ("blob", BLOB)]


def pcm_format(rate: int = A.RATE, channels: int = A.CHANNELS) -> WAVEFORMATEX:
    block = channels * A.SAMPLE_BYTES
    return WAVEFORMATEX(WAVE_FORMAT_PCM, channels, rate, rate * block, block, 8 * A.SAMPLE_BYTES, 0)


def activation_params(pid: int, mode: int = EXCLUDE_TARGET_PROCESS_TREE):
    """The PROPVARIANT (VT_BLOB) for ActivateAudioInterfaceAsync, and the struct it points to (keep both alive)."""
    params = AUDIOCLIENT_ACTIVATION_PARAMS(PROCESS_LOOPBACK, AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(pid, mode))
    pv = PROPVARIANT(VT_BLOB, 0, 0, 0, BLOB(ctypes.sizeof(params), ctypes.addressof(params)))
    return pv, params


def windows_build() -> int:
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        return 0


def process_loopback_available(build: Optional[int] = None) -> bool:
    return (windows_build() if build is None else build) >= PROCESS_LOOPBACK_BUILD


class ComError(OSError):
    pass


def check(hr: int, what: str) -> None:
    if hr < 0:
        raise ComError(f"{what} failed: 0x{hr & 0xFFFFFFFF:08X}")


def method(ptr, index: int, restype, *argtypes):
    """A COM method of the interface pointer ptr by its vtable index."""
    p = ptr.value if isinstance(ptr, c_void_p) else ptr
    vtbl = ctypes.cast(p, POINTER(POINTER(c_void_p))).contents
    fn = _FN(restype, c_void_p, *argtypes)(vtbl[index])
    return lambda *args: fn(p, *args)


def release(ptr) -> None:
    p = ptr.value if isinstance(ptr, c_void_p) else ptr
    if p:
        method(p, 2, c_uint32)()


# ---- the completion handler ActivateAudioInterfaceAsync calls (a tiny COM object made in Python) ---------------
_QI = _FN(c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
_REF = _FN(c_uint32, c_void_p)
_DONE = _FN(c_long, c_void_p, c_void_p)


class _HandlerVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", _QI), ("AddRef", _REF), ("Release", _REF), ("ActivateCompleted", _DONE)]


class _HandlerObj(ctypes.Structure):
    _fields_ = [("lpVtbl", POINTER(_HandlerVtbl))]


_KEEP = []                                  # handlers stay alive: Windows may still hold them after completion


class ActivationHandler:
    """IActivateAudioInterfaceCompletionHandler (+ IAgileObject, which Windows asks for: it is called on a worker
    thread). Its lifetime is Python's, so AddRef/Release only count."""

    def __init__(self):
        self.done = threading.Event()
        self.hr = 0
        self.client = c_void_p()
        self._refs = 1
        self._cbs = (_QI(self._qi), _REF(self._addref), _REF(self._release), _DONE(self._completed))
        self._vtbl = _HandlerVtbl(*self._cbs)
        self._obj = _HandlerObj(ctypes.pointer(self._vtbl))
        _KEEP.append(self)
        del _KEEP[:-8]

    @property
    def ptr(self) -> int:
        return ctypes.addressof(self._obj)

    def _qi(self, this, riid, ppv):
        want = bytes(riid.contents)
        if want in (bytes(IID_IUnknown), bytes(IID_IAgileObject), bytes(IID_IActivateAudioInterfaceCompletionHandler)):
            ppv[0] = this
            self._refs += 1
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _addref(self, this):
        self._refs += 1
        return self._refs

    def _release(self, this):
        self._refs = max(0, self._refs - 1)
        return self._refs

    def _completed(self, this, op):
        try:
            hr, unk = c_long(0), c_void_p()
            r = method(op, 3, c_long, POINTER(c_long), POINTER(c_void_p))(byref(hr), byref(unk))
            self.hr = r if r < 0 else hr.value
            self.client = unk
        except Exception as exc:              # never let an exception cross into Windows
            log.warning("Audio activation callback: %s", exc)
            self.hr = -1
        finally:
            self.done.set()
        return S_OK


def _co_init() -> bool:
    """COM on this thread (multithreaded). True if this call initialised it (then CoUninitialize at the end)."""
    hr = ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    return hr in (0, 1)                     # S_OK, S_FALSE; RPC_E_CHANGED_MODE: already STA, still usable


def _default_device(iid: GUID):
    """IMMDevice::Activate(iid) on the default speakers."""
    ole32 = ctypes.windll.ole32
    ole32.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, c_uint32, POINTER(GUID), POINTER(c_void_p)]
    ole32.CoCreateInstance.restype = c_long
    enum = c_void_p()
    check(ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL, byref(IID_IMMDeviceEnumerator),
                                 byref(enum)), "MMDeviceEnumerator")
    try:
        dev = c_void_p()
        check(method(enum, 4, c_long, c_int, c_int, POINTER(c_void_p))(E_RENDER, E_CONSOLE, byref(dev)),
              "GetDefaultAudioEndpoint")
        try:
            out = c_void_p()
            check(method(dev, 3, c_long, POINTER(GUID), c_uint32, c_void_p, POINTER(c_void_p))(
                byref(iid), CLSCTX_ALL, None, byref(out)), "IMMDevice::Activate")
            return out
        finally:
            release(dev)
    finally:
        release(enum)


class WasapiLoopback:
    """start(on_pcm) -> on_pcm(pcm, silent) is called on the capture thread with 16-bit interleaved PCM;
    silent: Windows marked the packet as silence. rate/channels: the format sent (known after start).
    mode: "process" (everything except the process tree of exclude_pid), "endpoint" (the default speakers), or
    "auto" (process where Windows has it)."""

    def __init__(self, mode: str = "auto", exclude_pid: Optional[int] = None,
                 include_pid: Optional[int] = None):
        if mode == "auto":
            mode = "process" if process_loopback_available() else "endpoint"
        self.mode = mode
        self.exclude_pid = os.getpid() if exclude_pid is None else exclude_pid
        self.include_pid = include_pid          # tests: only this process tree
        self.rate, self.channels = A.RATE, A.CHANNELS
        self.mutes_ok = mode == "process"       # the speakers can be muted without silencing the capture
        self.name = "WASAPI " + ("process loopback" if mode == "process" else "loopback")
        self.packets = 0
        self.silent_packets = 0
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None
        self.error: Optional[BaseException] = None     # set when the capture stopped on its own
        self._thread: Optional[threading.Thread] = None
        self._convert = None                    # endpoint mode: (channels, is_float, bits) of the mixer format

    def start(self, on_pcm: Callable[[bytes, bool], None], timeout: float = 10.0) -> None:
        self._stop.clear()
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, args=(on_pcm,), daemon=True, name="mirrorway-loopback")
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise RuntimeError("audio capture did not start (timed out)")
        if self._error is not None:
            raise self._error

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(3.0)

    # -- on the capture thread --
    def _activate_process(self):
        handler = ActivationHandler()
        mm = ctypes.windll.mmdevapi
        mm.ActivateAudioInterfaceAsync.argtypes = [ctypes.c_wchar_p, POINTER(GUID), POINTER(PROPVARIANT), c_void_p,
                                                   POINTER(c_void_p)]
        mm.ActivateAudioInterfaceAsync.restype = c_long
        if self.include_pid is not None:
            pv, params = activation_params(self.include_pid, INCLUDE_TARGET_PROCESS_TREE)
        else:
            pv, params = activation_params(self.exclude_pid, EXCLUDE_TARGET_PROCESS_TREE)
        op = c_void_p()
        check(mm.ActivateAudioInterfaceAsync(PROCESS_LOOPBACK_DEVICE, byref(IID_IAudioClient), byref(pv),
                                             handler.ptr, byref(op)), "ActivateAudioInterfaceAsync")
        try:
            if not handler.done.wait(5):
                raise RuntimeError("process loopback activation timed out")
            check(handler.hr, "process loopback activation")
            if not handler.client:
                raise RuntimeError("process loopback activation returned nothing")
            return handler.client
        finally:
            release(op)
            del params

    def _initialize(self, client) -> None:
        init = method(client, 3, c_long, c_int, c_uint32, c_int64, c_int64, c_void_p, c_void_p)
        if self.mode == "process":
            fmt = pcm_format()
            check(init(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                       | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM, BUFFER_HNS, 0, ctypes.addressof(fmt), None),
                  "IAudioClient::Initialize")
            return
        mix = c_void_p()
        check(method(client, 8, c_long, POINTER(c_void_p))(byref(mix)), "GetMixFormat")
        try:
            wf = ctypes.cast(mix, POINTER(WAVEFORMATEX)).contents
            tag, bits = wf.wFormatTag, wf.wBitsPerSample
            if tag == WAVE_FORMAT_EXTENSIBLE:
                sub = ctypes.cast(mix, POINTER(WAVEFORMATEXTENSIBLE)).contents.SubFormat
                tag = sub.Data1                 # KSDATAFORMAT_SUBTYPE_PCM / _IEEE_FLOAT carry the tag in Data1
            self.rate, src_channels = int(wf.nSamplesPerSec), int(wf.nChannels)
            self._convert = (src_channels, tag == WAVE_FORMAT_IEEE_FLOAT, bits)
            check(init(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK, BUFFER_HNS, 0, mix.value, None),
                  "IAudioClient::Initialize")
        finally:
            ctypes.windll.ole32.CoTaskMemFree(mix)

    def _run(self, on_pcm) -> None:
        kernel32 = ctypes.windll.kernel32
        inited = _co_init()
        client, cap, event = c_void_p(), c_void_p(), None
        try:
            try:
                if self.mode == "process":
                    client = self._activate_process()
                else:
                    client = _default_device(IID_IAudioClient)
                self._initialize(client)
                if self.mode == "process":
                    kernel32.CreateEventW.restype = c_void_p
                    event = kernel32.CreateEventW(None, False, False, None)
                    check(method(client, 13, c_long, c_void_p)(event), "SetEventHandle")
                check(method(client, 14, c_long, POINTER(GUID), POINTER(c_void_p))(byref(IID_IAudioCaptureClient),
                                                                                   byref(cap)), "GetService")
                check(method(client, 10, c_long)(), "IAudioClient::Start")
            except BaseException as exc:
                self._error = exc
                return
            finally:
                self._ready.set()
            log.info("Capturing Windows' sound: %s, %d Hz%s", self.name, self.rate,
                     f" (everything except process {self.exclude_pid} and its children)"
                     if self.mode == "process" and self.include_pid is None else "")
            next_size = method(cap, 5, c_long, POINTER(c_uint32))
            get_buffer = method(cap, 3, c_long, POINTER(c_void_p), POINTER(c_uint32), POINTER(c_uint32),
                                POINTER(c_uint64), POINTER(c_uint64))
            release_buffer = method(cap, 4, c_long, c_uint32)
            if event is not None:
                kernel32.WaitForSingleObject.argtypes = [c_void_p, c_uint32]
            n, data, frames, flags = c_uint32(), c_void_p(), c_uint32(), c_uint32()
            frame_bytes = self.channels * A.SAMPLE_BYTES if self._convert is None else \
                self._convert[0] * self._convert[2] // 8
            while not self._stop.is_set():
                if event is not None:
                    if kernel32.WaitForSingleObject(event, 200) == WAIT_TIMEOUT:
                        continue
                else:
                    self._stop.wait(0.01)
                while not self._stop.is_set():
                    check(next_size(byref(n)), "GetNextPacketSize")
                    if n.value == 0:
                        break
                    check(get_buffer(byref(data), byref(frames), byref(flags), None, None), "GetBuffer")
                    count = frames.value
                    silent = bool(flags.value & AUDCLNT_BUFFERFLAGS_SILENT) or not data.value
                    raw = b"" if silent else ctypes.string_at(data.value, count * frame_bytes)
                    release_buffer(count)
                    if not count:
                        continue
                    if silent:
                        pcm = bytes(count * self.channels * A.SAMPLE_BYTES)
                        self.silent_packets += 1
                    elif self._convert is None:
                        pcm = raw
                    else:
                        ch, is_float, bits = self._convert
                        pcm = A.to_s16(raw, count, ch, is_float, False, bits)
                    self.packets += 1
                    on_pcm(pcm, silent)
        except Exception as exc:
            if not self._stop.is_set():
                log.warning("Windows' sound capture stopped: %s", exc)
                self._error = self.error = exc
        finally:
            if client:
                try:
                    method(client, 11, c_long)()     # Stop
                except Exception:
                    pass
            release(cap)
            release(client)
            if event:
                kernel32.CloseHandle(c_void_p(event))
            if inited:
                ctypes.windll.ole32.CoUninitialize()


# ---- Windows' speakers: muted while the sound plays on the Mac --------------------------------------------
class SpeakerMute:
    """Mute of the default speakers (IAudioEndpointVolume). get() -> True/False, None if unknown."""

    def _call(self, fn):
        inited = _co_init()
        try:
            vol = _default_device(IID_IAudioEndpointVolume)
            try:
                return fn(vol)
            finally:
                release(vol)
        finally:
            if inited:
                ctypes.windll.ole32.CoUninitialize()

    def get(self) -> Optional[bool]:
        try:
            def get_mute(vol):
                b = c_int(0)
                check(method(vol, 15, c_long, POINTER(c_int))(byref(b)), "GetMute")
                return bool(b.value)
            return self._call(get_mute)
        except Exception as exc:
            log.info("Could not read the speakers' mute: %s", exc)
            return None

    def set(self, muted: bool) -> bool:
        try:
            self._call(lambda vol: check(method(vol, 14, c_long, c_int, c_void_p)(int(bool(muted)), None),
                                         "SetMute"))
            return True
        except Exception as exc:
            log.info("Could not %s the speakers: %s", "mute" if muted else "unmute", exc)
            return False
