"""H.264 helpers (platform independent).

- AVCC (length-prefixed, VideoToolbox output) -> Annex B (start codes) conversion
- software encoder with PyAV (x264, zero-latency tuning) — the fallback when the Mac's hardware encoder
  is unavailable, also used as a source in tests
- decoder with PyAV (Windows): scales the frame to the window size and converts it to an RGBA array

Colour: the encoders use BT.709, limited range (16-235); the decoder converts accordingly.
"""
from __future__ import annotations

import logging
import struct
import time
from fractions import Fraction
from typing import List, Optional, Tuple

log = logging.getLogger("mirrorway.h264")

START = b"\x00\x00\x00\x01"
FLAG_KEY = 0x01          # MSG_FRAME_H264 flag: key frame (IDR)


# ---------------------------------------------------------------------------
# Bitstream helpers
# ---------------------------------------------------------------------------
def parse_avcc_config(avcc: bytes) -> Tuple[List[bytes], List[bytes], int]:
    """avcC record (AVCDecoderConfigurationRecord) -> (SPS list, PPS list, NAL length size)."""
    if len(avcc) < 7 or avcc[0] != 1:
        raise ValueError("invalid avcC")
    nal_len = (avcc[4] & 0x03) + 1
    pos = 5
    sps, pps = [], []
    n = avcc[pos] & 0x1F
    pos += 1
    for _ in range(n):
        (ln,) = struct.unpack_from(">H", avcc, pos)
        pos += 2
        sps.append(bytes(avcc[pos:pos + ln]))
        pos += ln
    n = avcc[pos]
    pos += 1
    for _ in range(n):
        (ln,) = struct.unpack_from(">H", avcc, pos)
        pos += 2
        pps.append(bytes(avcc[pos:pos + ln]))
        pos += ln
    if not sps or not pps:
        raise ValueError("no SPS/PPS in avcC")
    return sps, pps, nal_len


def avcc_to_annexb(data: bytes, nal_len: int = 4) -> bytes:
    """Converts length-prefixed NAL units to start-code format."""
    out = bytearray()
    pos, n = 0, len(data)
    while pos + nal_len <= n:
        ln = int.from_bytes(data[pos:pos + nal_len], "big")
        pos += nal_len
        if ln <= 0 or pos + ln > n:
            raise ValueError("corrupt AVCC data")
        out += START
        out += data[pos:pos + ln]
        pos += ln
    return bytes(out)


def build_annexb(nals: List[bytes]) -> bytes:
    return b"".join(START + n for n in nals)


def split_annexb(data: bytes) -> List[bytes]:
    """Splits an Annex B stream into NAL units (3- and 4-byte start codes)."""
    out, i, n = [], 0, len(data)
    starts = []
    while i < n - 2:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                starts.append((i, i + 3))
                i += 3
                continue
            if i < n - 3 and data[i + 2] == 0 and data[i + 3] == 1:
                starts.append((i, i + 4))
                i += 4
                continue
        i += 1
    for k, (_, s) in enumerate(starts):
        e = starts[k + 1][0] if k + 1 < len(starts) else n
        out.append(bytes(data[s:e]))
    return out


def nal_types(data: bytes) -> List[int]:
    return [n[0] & 0x1F for n in split_annexb(data) if n]


def is_keyframe(data: bytes) -> bool:
    return 5 in nal_types(data)


# ---------------------------------------------------------------------------
# PyAV
# ---------------------------------------------------------------------------
def _av():
    import av  # noqa: F401  (ImportError when missing)
    return av


def decoder_available() -> bool:
    try:
        av = _av()
        av.codec.Codec("h264", "r")
        return True
    except Exception:
        return False


def encoder_available(name: str = "libx264") -> bool:
    try:
        av = _av()
        av.codec.Codec(name, "w")
        return True
    except Exception:
        return False


class AVEncoder:
    """H.264 encoder with PyAV. Input: a BGRA/BGRX or RGB numpy array, or a PIL image."""

    hardware = False

    def __init__(self, width: int, height: int, fps: int, kbps: int, codec: str = "libx264"):
        av = _av()
        from av.video.reformatter import ColorRange, Colorspace, VideoReformatter
        self._av = av
        self._cs, self._cr = Colorspace, ColorRange
        self.width, self.height = int(width), int(height)
        self.name = "x264 (software)" if codec == "libx264" else codec
        ctx = av.CodecContext.create(codec, "w")
        ctx.width, ctx.height = self.width, self.height
        ctx.pix_fmt = "yuv420p"
        ctx.time_base = Fraction(1, 1000)
        ctx.framerate = Fraction(int(fps), 1)
        ctx.bit_rate = int(kbps) * 1000
        ctx.max_bit_rate = int(kbps) * 1000             # VBV: bitrate changes take effect at once
        ctx.rc_buffer_size = int(kbps) * 1000 // 2
        ctx.gop_size = max(1, int(fps)) * 600          # key frames only on request
        ctx.max_b_frames = 0
        ctx.colorspace = Colorspace.ITU709
        ctx.color_range = ColorRange.MPEG
        if codec == "libx264":
            ctx.options = {"preset": "ultrafast", "tune": "zerolatency", "repeat-headers": "1",
                           "annexb": "1", "profile": "high", "threads": "auto"}
        ctx.open()
        self.ctx = ctx
        self._rf = VideoReformatter()
        self._t0 = time.monotonic()
        self._last_pts = -1

    def _frame(self, img):
        av = self._av
        try:
            import numpy as np
        except Exception:  # pragma: no cover
            np = None
        if np is not None and isinstance(img, np.ndarray):
            fmt = "bgra" if img.ndim == 3 and img.shape[2] == 4 else "rgb24"
            return av.VideoFrame.from_ndarray(np.ascontiguousarray(img), format=fmt)
        return av.VideoFrame.from_image(img.convert("RGB") if img.mode != "RGB" else img)

    def encode(self, img, force_key: bool = False) -> Optional[Tuple[bytes, bool]]:
        f = self._frame(img)
        if (f.width, f.height) != (self.width, self.height):
            raise ValueError(f"frame size changed: {f.width}x{f.height}")
        y = self._rf.reformat(f, format="yuv420p", src_colorspace=self._cs.ITU709,
                              dst_colorspace=self._cs.ITU709, src_color_range=self._cr.JPEG,
                              dst_color_range=self._cr.MPEG)
        pts = int((time.monotonic() - self._t0) * 1000)
        if pts <= self._last_pts:
            pts = self._last_pts + 1
        self._last_pts = pts
        y.pts = pts
        if force_key:
            y.pict_type = self._av.video.frame.PictureType.I
        out, key = [], False
        for p in self.ctx.encode(y):
            out.append(bytes(p))
            key = key or bool(p.is_keyframe)
        if not out:
            return None
        data = b"".join(out)
        return data, key or is_keyframe(data)

    def set_bitrate(self, kbps: int) -> None:
        """Change the bitrate while running (x264 is reconfigured)."""
        if self.ctx is not None:
            self.ctx.bit_rate = int(kbps) * 1000
            self.ctx.max_bit_rate = int(kbps) * 1000
            self.ctx.rc_buffer_size = int(kbps) * 1000 // 2

    def close(self) -> None:
        self.ctx = None


class H264Decoder:
    """H.264 (Annex B) decoder; converts the frame to an RGBA array of the requested size."""

    def __init__(self, threads: int = 0, hw: Optional[str] = None):
        """hw: hardware decoding type such as "d3d11va" (Windows); falls back to software if it cannot be opened."""
        av = _av()
        from av.codec.context import Flags
        from av.video.reformatter import ColorRange, Colorspace, Interpolation, VideoReformatter
        self._av = av
        self._cs, self._cr, self._ip = Colorspace, ColorRange, Interpolation
        self._Flags = Flags
        self._threads = threads
        self._rf = VideoReformatter()
        self.hw = hw
        self.hw_failed = False
        self.kind = "software"
        self.ctx = None
        self.reset()

    def reset(self) -> None:
        ctx = None
        if self.hw and not self.hw_failed:
            try:
                from av.codec.hwaccel import HWAccel
                ctx = self._av.CodecContext.create("h264", "r",
                                                   hwaccel=HWAccel(self.hw, allow_software_fallback=True))
                self.kind = self.hw.upper()
            except Exception as exc:
                log.info("Could not open the hardware decoder (%s): %s; using software", self.hw, exc)
                self.hw_failed = True
                ctx = None
        if ctx is None:
            ctx = self._av.CodecContext.create("h264", "r")
            self.kind = "software"
        ctx.flags = ctx.flags | self._Flags.low_delay
        ctx.thread_type = "SLICE"          # frame threading adds latency
        if self._threads:
            ctx.thread_count = self._threads
        self.ctx = ctx

    def fail_hw(self, reason) -> None:
        """Hardware decoding failed: use software from now on."""
        if self.kind != "software":
            log.warning("Hardware decoding (%s) abandoned: %s", self.kind, reason)
            self.hw_failed = True
            self.reset()

    def decode(self, data: bytes) -> list:
        frames = list(self.ctx.decode(self._av.Packet(data)))
        if frames and self.kind != "software" and frames[-1].format.name not in ("nv12", "p010le"):
            # the hardware decoder opened but the decoder fell back to software (allow_software_fallback)
            self.kind = "software"
        return frames

    def to_rgba(self, frame, size: Optional[Tuple[int, int]] = None):
        w, h = size if size else (frame.width, frame.height)
        # Follow the stream's colour information if present; otherwise our encoders' default (BT.709, 16-235)
        try:
            cs = int(frame.colorspace)
            rng = int(frame.color_range)
        except Exception:
            cs, rng = 2, 0
        src_cs = self._cs.ITU601 if cs in (5, 6) else self._cs.ITU709
        src_rng = self._cr.JPEG if rng == 2 else self._cr.MPEG
        out = self._rf.reformat(frame, width=int(w), height=int(h), format="rgba",
                                src_colorspace=src_cs, dst_colorspace=src_cs,
                                src_color_range=src_rng, dst_color_range=self._cr.JPEG,
                                interpolation=self._ip.BILINEAR)
        arr = out.to_ndarray()
        # Rows may be padded for alignment; a contiguous array speeds up both comparison (~10×) and
        # the PIL conversion
        import numpy as np
        return np.ascontiguousarray(arr)
