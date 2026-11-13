"""Audio streaming, the platform-independent part.

Where sound plays is one choice on the Mac (ROUTES, 3.4): each computer on its own speakers, all on Windows, or all
on the Mac.
  - All on Windows: sound playing on the Mac is captured with ScreenCaptureKit (the Screen Recording permission is
    enough) and sent to Windows over a separate encrypted connection (ROLE_AUDIO); Windows plays it through SDL2's
    audio queue.
  - All on the Mac (3.4): Windows captures its own sound with WASAPI (process loopback: every app except
    Mirrorway) and sends it to the Mac over ROLE_WINAUDIO; the Mac plays it through an AudioQueue.
Both directions send 48 kHz stereo 16-bit PCM: 1.5 Mbit/s of uncompressed audio is no problem on a home network,
and there is no codec delay. The receiving side is the same code (Receiver).

Latency: the receiver starts playing after PREBUFFER_MS have arrived; if the queue grows past LIMIT_MS (audio that
piled up while the network stalled), it is cleared and playback continues from the newest packet, so sound never
lags.
"""
from __future__ import annotations

import math
import struct
from typing import Optional

FEATURE = "audio"
WINAUDIO_FEATURE = "winaudio"   # 3.4: Windows can send its sound (Windows' hello) / the Mac can play it (the Mac's)
ROUTES = ("own", "windows", "mac")   # where sound plays: each computer on its own, all on Windows, all on the Mac
RATE, CHANNELS, SAMPLE_BYTES = 48000, 2, 2
FORMAT = "s16le"
PREBUFFER_MS = 40          # audio collected before playback starts
LIMIT_MS = 180             # older audio is dropped when the queue grows past this

# messages (on ROLE_AUDIO the Mac sends and Windows plays; on ROLE_WINAUDIO the other way round)
MSG_AUDIO_STATE = 33       # control channel s->c JSON {"on": bool (play the Mac's sound), "route": ROUTES (3.4),
                           #   "capture": bool (3.4: send Windows' sound to the Mac)}
MSG_AUDIO_INFO = 50        # audio channel sender->player JSON {"rate", "channels", "format"} or {"error"}
MSG_AUDIO = 51             # audio channel sender->player >I sequence + PCM (s16le, interleaved)
MSG_AUDIO_STATS = 52       # audio channel player->sender JSON (queue, underruns)
MSG_AUDIO_IDLE = 53        # audio channel sender->player empty: silence (the capture may send nothing in silence)
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


class SourceError(RuntimeError):
    """The sending side cannot provide sound (off, no permission…): retrying is pointless until it is turned on again."""


class Receiver:
    """Plays an incoming audio stream (MSG_AUDIO_INFO / MSG_AUDIO / MSG_AUDIO_IDLE) through a queue-based player
    with QueuePolicy. Windows uses it for the Mac's sound, the Mac for Windows' sound (3.4).
    player_factory(rate, channels) -> an object with queued(), queue(pcm), clear(), play(on), close()."""

    def __init__(self, player_factory, on_open: Optional[callable] = None):
        self.player_factory = player_factory
        self.on_open = on_open                  # called with (rate, channels, player) when a device opens
        self.player = None
        self.policy: Optional[QueuePolicy] = None
        self.fmt = None
        self.packets = 0
        self.bytes = 0
        self.idles = 0
        self.meter = LevelMeter()
        self._qsum = 0
        self._qn = 0

    def handle(self, mtype: int, payload: bytes) -> bool:
        """One message from the sender. True if it was an audio message. Raises SourceError for an error report."""
        if mtype == MSG_AUDIO_INFO:
            import json
            info = json.loads(payload.decode("utf-8"))
            if info.get("error"):
                raise SourceError(str(info["error"]))
            new = (int(info.get("rate") or RATE), int(info.get("channels") or CHANNELS))
            if new != self.fmt:
                self.close()
                self.player = self.player_factory(*new)
                self.policy = QueuePolicy(*new)
                self.fmt = new
                if self.on_open is not None:
                    self.on_open(new[0], new[1], self.player)
            return True
        if mtype == MSG_AUDIO_IDLE:
            self.idles += 1
            pl = self.player
            if pl is not None and self.policy is not None and pl.queued() == 0:
                self.policy.idle()              # the sender is silent: not an underrun; collect again when sound returns
                pl.play(False)
            return True
        if mtype == MSG_AUDIO:
            pl = self.player
            if pl is None:
                return True
            _seq, pcm = unpack_audio(payload)
            queued = pl.queued()
            act = self.policy.on_packet(queued, len(pcm))
            if act == "trim":
                pl.clear()
            elif act == "rebuffer":
                pl.play(False)
            pl.queue(pcm)
            if act in ("start", "trim"):
                pl.play(True)
            self.packets += 1
            self.bytes += len(pcm)
            self.meter.add(pcm)
            self._qsum += queued
            self._qn += 1
            return True
        return False

    def stats(self) -> dict:
        """For MSG_AUDIO_STATS: the average queue since the last call, underruns and trims so far."""
        if self.policy is None:
            return {}
        out = {"queue_ms": round(self.policy.ms(self._qsum / self._qn), 1) if self._qn else 0.0,
               "underruns": self.policy.underruns, "trims": self.policy.trims}
        self._qsum = self._qn = 0
        return out

    def close(self) -> None:
        pl, self.player = self.player, None
        self.fmt = None
        if pl is not None:
            try:
                pl.close()
            except Exception:
                pass
