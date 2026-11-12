"""Audio streaming (Mac -> Windows), the platform-independent part.

Sound playing on the Mac is captured with ScreenCaptureKit (the Screen Recording permission is enough) and sent to
Windows as 48 kHz stereo 16-bit PCM over a separate encrypted connection (ROLE_AUDIO); Windows plays it through
SDL2's audio queue. 1.5 Mbit/s of uncompressed audio is no problem on a home network, and there is no codec delay.

Latency: Windows starts playing after PREBUFFER_MS have arrived; if the queue grows past LIMIT_MS (audio that piled
up while the network stalled), it is cleared and playback continues from the newest packet, so sound never lags.
"""
from __future__ import annotations

import math
import struct
from typing import Optional

FEATURE = "audio"
RATE, CHANNELS, SAMPLE_BYTES = 48000, 2, 2
FORMAT = "s16le"
PREBUFFER_MS = 40          # audio collected before playback starts
LIMIT_MS = 180             # older audio is dropped when the queue grows past this

# messages
MSG_AUDIO_STATE = 33       # control channel s->c JSON {"on": bool}
MSG_AUDIO_INFO = 50        # audio channel s->c JSON {"rate", "channels", "format"} or {"error"}
MSG_AUDIO = 51             # audio channel s->c >I sequence + PCM (s16le, interleaved)
MSG_AUDIO_STATS = 52       # audio channel c->s JSON (queue, underruns)
MSG_AUDIO_IDLE = 53        # audio channel s->c empty: no sound on the Mac (ScreenCaptureKit may send nothing in silence)
IDLE_EVERY_S = 2.0         # MSG_AUDIO_IDLE interval during silence (keeps the connection alive)

_SEQ = struct.Struct(">I")


def pack_audio(seq: int, pcm: bytes) -> bytes:
    return _SEQ.pack(seq & 0xFFFFFFFF) + pcm


def unpack_audio(payload: bytes):
    (seq,) = _SEQ.unpack_from(payload)
    return seq, payload[_SEQ.size:]


def bytes_per_ms(rate: int = RATE, channels: int = CHANNELS) -> float:
    return rate * channels * SAMPLE_BYTES / 1000.0


def to_s16(data: bytes, frames: int, channels: int, is_float: bool, planar: bool, bits: int = 32,
           out_channels: int = CHANNELS) -> bytes:
    """Captured audio -> 16-bit interleaved PCM. planar: channels one after the other (as ScreenCaptureKit delivers)."""
    import numpy as np
    if frames <= 0 or channels <= 0:
        return b""
    if is_float:
        dt = np.float32 if bits == 32 else np.float64
    else:
        dt = {16: np.int16, 32: np.int32}.get(bits, np.int16)
    frames = min(frames, len(data) // (np.dtype(dt).itemsize * channels))    # short buffer: use what is there
    if frames <= 0:
        return b""
    a = np.frombuffer(data, dtype=dt, count=frames * channels)
    a = a.reshape(channels, frames).T if planar else a.reshape(frames, channels)
    if channels == 1 and out_channels == 2:
        a = np.repeat(a, 2, axis=1)
    elif channels > out_channels:
        a = a[:, :out_channels]
    if is_float:
        a = np.clip(a, -1.0, 1.0) * 32767.0
    elif dt == np.int32:
        a = a / 65536.0
    return np.ascontiguousarray(a.astype("<i2")).tobytes()


def level_dbfs(pcm: bytes) -> float:
    """Level of 16-bit PCM in dBFS; silence is -120."""
    import numpy as np
    if not pcm:
        return -120.0
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if not len(a):
        return -120.0
    rms = math.sqrt(float(np.mean(a * a))) / 32768.0
    return 20.0 * math.log10(rms) if rms > 1e-6 else -120.0


class QueuePolicy:
    """Decisions for the playback queue on Windows (pure logic, tested without SDL)."""

    def __init__(self, rate: int = RATE, channels: int = CHANNELS, prebuffer_ms: int = PREBUFFER_MS,
                 limit_ms: int = LIMIT_MS):
        self.bpm = bytes_per_ms(rate, channels)
        self.prebuffer = int(prebuffer_ms * self.bpm)
        self.limit = int(limit_ms * self.bpm)
        self.playing = False
        self.underruns = 0
        self.trims = 0

    def on_packet(self, queued: int, size: int) -> str:
        """queued: bytes in the queue (before this packet). Returns:
        "queue"    add to the queue
        "start"    add and start playing (enough has been collected)
        "rebuffer" the queue had run dry: pause, add, collect again (a short gap beats choppy playback)
        "trim"     the queue is over its limit: drop the old audio, add, keep playing (sound must not lag)"""
        if queued + size > self.limit:
            self.trims += 1
            self.playing = True
            return "trim"
        if self.playing and queued == 0:
            self.underruns += 1
            self.playing = False
            return "rebuffer"
        if not self.playing and queued + size >= self.prebuffer:
            self.playing = True
            return "start"
        return "queue"

    def idle(self) -> None:
        """Sound stopped on the Mac: the queue running dry is not an underrun; collect again when sound returns."""
        self.playing = False

    def ms(self, queued: int) -> float:
        return queued / self.bpm if self.bpm else 0.0


SOUND_DBFS = -60.0          # a packet louder than this counts as sound (below it: silence or noise)


class LevelMeter:
    """The level of the sound sent in a period, for the log. Averaging dB values packet by packet (as before 3.2.4)
    let the silent packets ScreenCaptureKit sends between songs pull the average to "silent" even while music
    played; here the level is the energy average of the packets with sound, with the share of time that had sound
    and the loudest sample."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.packets = 0
        self.sound = 0
        self.energy = 0.0           # sum of squared samples (full scale = 1) over the packets with sound
        self.samples = 0
        self.peak = 0.0             # the loudest sample (full scale = 1)

    def add(self, pcm: bytes) -> None:
        import numpy as np
        a = np.frombuffer(pcm, dtype="<i2", count=len(pcm) // 2).astype(np.float32) / 32768.0
        if not len(a):
            return
        self.packets += 1
        sq = float(np.dot(a, a))
        self.peak = max(self.peak, float(np.max(np.abs(a))))
        if sq / len(a) > 10 ** (SOUND_DBFS / 10):
            self.sound += 1
            self.energy += sq
            self.samples += len(a)

    def level(self) -> Optional[float]:
        """dBFS while sound played; None if there was no sound."""
        if not self.samples or self.energy <= 0:
            return None
        return 10 * math.log10(self.energy / self.samples)

    def describe(self) -> str:
        if not self.packets:
            return "no audio"
        lvl = self.level()
        if lvl is None:
            return "silent"
        peak = 20 * math.log10(self.peak) if self.peak > 0 else -120.0
        return (f"level {lvl:.0f} dBFS while playing ({100.0 * self.sound / self.packets:.0f}% of the time), "
                f"peak {peak:.0f} dBFS")


def describe_level(db: Optional[float]) -> str:
    if db is None:
        return "?"
    return "silent" if db <= -90 else f"{db:.0f} dBFS"
