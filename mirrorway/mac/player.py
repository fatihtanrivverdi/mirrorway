"""Playing Windows' sound on the Mac (3.4): an AudioQueue (AudioToolbox) driven with plain ctypes.

The same queue interface as the SDL player on Windows (queued, queue, clear, play, close), so mirrorway.audio's
Receiver and QueuePolicy decide when to start, rebuffer and trim. The AudioQueue keeps BUFFERS small buffers of
BUFFER_MS each in flight; its callback (on the queue's own thread) refills one from our byte queue, padding with
silence when it runs dry. Paused while Windows is silent, so the Mac's audio hardware can go idle.
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import POINTER, byref, c_double, c_float, c_int32, c_uint32, c_void_p

from .. import audio as A

log = logging.getLogger("mirrorway.audio")

AUDIO_TOOLBOX = "/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox"
K_AUDIO_FORMAT_LINEAR_PCM = 0x6C70636D          # 'lpcm'
K_FLAG_SIGNED_INTEGER = 0x4
K_FLAG_PACKED = 0x8
K_PARAM_VOLUME = 1
BUFFERS = 3
BUFFER_MS = 10


class AudioStreamBasicDescription(ctypes.Structure):
    _fields_ = [("mSampleRate", c_double), ("mFormatID", c_uint32), ("mFormatFlags", c_uint32),
                ("mBytesPerPacket", c_uint32), ("mFramesPerPacket", c_uint32), ("mBytesPerFrame", c_uint32),
                ("mChannelsPerFrame", c_uint32), ("mBitsPerChannel", c_uint32), ("mReserved", c_uint32)]


class AudioQueueBuffer(ctypes.Structure):
    _fields_ = [("mAudioDataBytesCapacity", c_uint32), ("mAudioData", c_void_p), ("mAudioDataByteSize", c_uint32),
                ("mUserData", c_void_p), ("mPacketDescriptionCapacity", c_uint32),
                ("mPacketDescriptions", c_void_p), ("mPacketDescriptionCount", c_uint32)]


_CALLBACK = ctypes.CFUNCTYPE(None, c_void_p, c_void_p, POINTER(AudioQueueBuffer))
_AT = None


def _toolbox():
    global _AT
    if _AT is None:
        at = ctypes.CDLL(AUDIO_TOOLBOX)
        at.AudioQueueNewOutput.argtypes = [POINTER(AudioStreamBasicDescription), _CALLBACK, c_void_p, c_void_p,
                                           c_void_p, c_uint32, POINTER(c_void_p)]
        at.AudioQueueAllocateBuffer.argtypes = [c_void_p, c_uint32, POINTER(POINTER(AudioQueueBuffer))]
        at.AudioQueueEnqueueBuffer.argtypes = [c_void_p, POINTER(AudioQueueBuffer), c_uint32, c_void_p]
        at.AudioQueueStart.argtypes = [c_void_p, c_void_p]
        at.AudioQueuePause.argtypes = [c_void_p]
        at.AudioQueueStop.argtypes = [c_void_p, ctypes.c_bool]
        at.AudioQueueDispose.argtypes = [c_void_p, ctypes.c_bool]
        at.AudioQueueSetParameter.argtypes = [c_void_p, c_uint32, c_float]
        for name in ("AudioQueueNewOutput", "AudioQueueAllocateBuffer", "AudioQueueEnqueueBuffer", "AudioQueueStart",
                     "AudioQueuePause", "AudioQueueStop", "AudioQueueDispose", "AudioQueueSetParameter"):
            getattr(at, name).restype = c_int32
        _AT = at
    return _AT


def _check(status: int, what: str) -> None:
    if status != 0:
        raise RuntimeError(f"{what} failed (OSStatus {status})")


class Ring:
    """The byte queue between the network and the AudioQueue callback (pure logic, tested without Core Audio)."""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.underruns = 0                      # callbacks that found less than a buffer while playing
        self.fills = 0

    def __len__(self) -> int:
        return len(self._buf)

    def add(self, pcm: bytes) -> None:
        with self._lock:
            self._buf += pcm

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def take(self, n: int) -> bytes:
        """n bytes for the device: what is there, padded with silence."""
        with self._lock:
            got = bytes(self._buf[:n])
            del self._buf[:n]
        self.fills += 1
        if len(got) < n:
            self.underruns += 1
            got += bytes(n - len(got))
        return got


class AudioQueuePlayer:
    """16-bit interleaved PCM to the Mac's current output device."""

    def __init__(self, rate: int = A.RATE, channels: int = A.CHANNELS):
        at = _toolbox()
        self.rate, self.channels = rate, channels
        self.driver = "AudioQueue"
        self.ring = Ring()
        frame = channels * A.SAMPLE_BYTES
        self.buffer_bytes = max(frame, int(rate * BUFFER_MS / 1000) * frame)
        self.asbd = AudioStreamBasicDescription(float(rate), K_AUDIO_FORMAT_LINEAR_PCM,
                                                K_FLAG_SIGNED_INTEGER | K_FLAG_PACKED, frame, 1, frame, channels,
                                                8 * A.SAMPLE_BYTES, 0)
        self._cb = _CALLBACK(self._callback)
        self.aq = c_void_p()
        _check(at.AudioQueueNewOutput(byref(self.asbd), self._cb, None, None, None, 0, byref(self.aq)),
               "AudioQueueNewOutput")
        self.playing = False
        self.closed = False
        self.callbacks = 0
        try:
            at.AudioQueueSetParameter(self.aq, K_PARAM_VOLUME, 1.0)
            for _ in range(BUFFERS):
                buf = POINTER(AudioQueueBuffer)()
                _check(at.AudioQueueAllocateBuffer(self.aq, self.buffer_bytes, byref(buf)), "AudioQueueAllocateBuffer")
                self._fill(buf, bytes(self.buffer_bytes))      # silence until there is sound
        except Exception:
            at.AudioQueueDispose(self.aq, True)
            raise

    def _fill(self, buf, data: bytes) -> None:
        b = buf.contents
        n = min(len(data), b.mAudioDataBytesCapacity)
        ctypes.memmove(b.mAudioData, data, n)
        b.mAudioDataByteSize = n
        _toolbox().AudioQueueEnqueueBuffer(self.aq, buf, 0, None)

    def _callback(self, user, aq, buf) -> None:
        try:
            self.callbacks += 1
            if self.closed:
                return
            self._fill(buf, self.ring.take(self.buffer_bytes) if self.playing else bytes(self.buffer_bytes))
        except Exception:                       # never let an exception cross into Core Audio
            pass

    # the queue interface (mirrorway.audio.Receiver)
    def queued(self) -> int:
        return len(self.ring)

    def queue(self, pcm: bytes) -> None:
        self.ring.add(pcm)

    def clear(self) -> None:
        self.ring.clear()

    def play(self, on: bool = True) -> None:
        if self.closed or on == self.playing:
            return
        at = _toolbox()
        if on:
            _check(at.AudioQueueStart(self.aq, None), "AudioQueueStart")
        else:
            at.AudioQueuePause(self.aq)
        self.playing = on

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        at = _toolbox()
        at.AudioQueueStop(self.aq, True)
        at.AudioQueueDispose(self.aq, True)
