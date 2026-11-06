"""The Mac's hardware H.264 encoder (VideoToolbox, through pyobjc).

The pixel buffer from ScreenCaptureKit (an IOSurface) goes straight to the encoder; there is no copy and no
colour conversion on the CPU. Low-latency mode (EnableLowLatencyRateControl, macOS 11.3+): no B-frames,
no look-ahead. VTCompressionSessionCompleteFrames is called after every frame, so each frame is sent
as soon as it is encoded (without waiting for the next one).

VideoToolbox outputs AVCC (length-prefixed); here it is converted to Annex B and SPS/PPS are put in front of
key frames, so every packet is self-sufficient for the decoder on Windows.
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

from ..h264 import avcc_to_annexb, build_annexb, is_keyframe, parse_avcc_config

log = logging.getLogger("mirrorway.vtenc")

_REGISTERED = False


def _vt():
    import VideoToolbox as VT
    return VT


def _register_session_type() -> None:
    """pyobjc does not know VTCompressionSessionRef as a CF type; unless it is registered, the session object
    cannot be passed to functions that expect an `id`, such as VTSessionSetProperty."""
    global _REGISTERED
    if _REGISTERED:
        return
    import objc
    VT = _vt()
    try:
        objc.registerCFSignature("VTCompressionSessionRef", b"^{OpaqueVTCompressionSession=}",
                                 VT.VTCompressionSessionGetTypeID())
    except Exception as exc:  # already registered is fine
        log.debug("VTCompressionSessionRef registration: %s", exc)
    _REGISTERED = True


def _k(module, name: str, fallback: str):
    """The pyobjc constant; if missing (older version), a CFString whose value is its own name."""
    try:
        v = getattr(module, name)
        return v if v is not None else fallback
    except Exception:
        return fallback


def _ok(res) -> int:
    """pyobjc returns are sometimes (status, output...) tuples."""
    return int(res[0] if isinstance(res, tuple) else res)


class VTEncoder:
    hardware = True

    def __init__(self, width: int, height: int, fps: int, kbps: int):
        import CoreMedia as CM
        VT = _vt()
        _register_session_type()
        self.CM, self.VT = CM, VT
        self.width, self.height = int(width), int(height)
        self.fps = max(1, int(fps))
        self.kbps = int(kbps)
        self._avcc_cache = (None, None)       # (avcC bytes, Annex B SPS+PPS)
        self._t0 = time.monotonic()
        self._last_ms = -1
        self.low_latency = True
        self.session = None
        self.mode = "handler"

        spec = {
            _k(VT, "kVTVideoEncoderSpecification_EnableLowLatencyRateControl",
               "EnableLowLatencyRateControl"): True,
            _k(VT, "kVTVideoEncoderSpecification_EnableHardwareAcceleratedVideoEncoder",
               "EnableHardwareAcceleratedVideoEncoder"): True,
        }
        codec = int(_k(CM, "kCMVideoCodecType_H264", 0x61766331))
        session, err = self._create(codec, spec)
        if session is None:
            # If low-latency mode is not supported, normal real-time encoding
            self.low_latency = False
            spec.pop(_k(VT, "kVTVideoEncoderSpecification_EnableLowLatencyRateControl",
                        "EnableLowLatencyRateControl"), None)
            session, err2 = self._create(codec, spec)
            if session is None:
                raise RuntimeError(f"VTCompressionSessionCreate failed: {err} / {err2}")
        self.session = session
        self._configure()
        st = _ok(VT.VTCompressionSessionPrepareToEncodeFrames(session))
        if st != 0:
            log.warning("VTCompressionSessionPrepareToEncodeFrames: %d", st)
        self.hardware = self._copy_bool("kVTCompressionPropertyKey_UsingHardwareAcceleratedVideoEncoder",
                                        "UsingHardwareAcceleratedVideoEncoder")
        self.name = "VideoToolbox " + ("(hardware)" if self.hardware is not False else "(software)")
        if self.low_latency:
            self.name += ", low latency"
        log.info("H.264 encoder ready: %s, %dx%d, %d fps, %d kbit/s", self.name, self.width,
                 self.height, self.fps, self.kbps)

    # ---- setup ----------------------------------------------------------------
    def _create(self, codec: int, spec: dict):
        VT = self.VT
        self._callback_out = []
        try:
            res = VT.VTCompressionSessionCreate(None, self.width, self.height, codec, spec, None, None,
                                                None, None, None)
            self.mode = "handler"
        except (TypeError, ValueError) as exc:
            # Some pyobjc versions may not accept a NULL callback: the callback path
            log.info("Could not open a session without an output handler (%s); trying the callback path", exc)
            res = VT.VTCompressionSessionCreate(None, self.width, self.height, codec, spec, None, None,
                                                self._on_output_cb, None, None)
            self.mode = "callback"
        status, session = (res[0], res[-1]) if isinstance(res, tuple) else (res, None)
        if int(status) != 0 or session is None:
            return None, int(status)
        return session, 0

    def _set(self, name: str, fallback: str, value) -> bool:
        key = _k(self.VT, name, fallback)
        try:
            st = _ok(self.VT.VTSessionSetProperty(self.session, key, value))
        except Exception as exc:
            log.info("Could not set an H.264 property: %s (%s)", fallback, exc)
            return False
        if st != 0:
            log.info("H.264 property rejected: %s = %r (%d)", fallback, value, st)
        return st == 0

    def _configure(self) -> None:
        VT, CM = self.VT, self.CM
        self._set("kVTCompressionPropertyKey_RealTime", "RealTime", True)
        self._set("kVTCompressionPropertyKey_AllowFrameReordering", "AllowFrameReordering", False)
        for name, val in (("kVTProfileLevel_H264_ConstrainedHigh_AutoLevel", "H264_ConstrainedHigh_AutoLevel"),
                          ("kVTProfileLevel_H264_High_AutoLevel", "H264_High_AutoLevel"),
                          ("kVTProfileLevel_H264_Main_AutoLevel", "H264_Main_AutoLevel")):
            if self._set("kVTCompressionPropertyKey_ProfileLevel", "ProfileLevel", _k(VT, name, val)):
                self.profile = val
                break
        self._set("kVTCompressionPropertyKey_AverageBitRate", "AverageBitRate", self.kbps * 1000)
        self._set("kVTCompressionPropertyKey_ExpectedFrameRate", "ExpectedFrameRate", self.fps)
        self._set("kVTCompressionPropertyKey_MaxKeyFrameInterval", "MaxKeyFrameInterval", self.fps * 600)
        self._set("kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration", "MaxKeyFrameIntervalDuration", 600)
        bt709 = "ITU_R_709_2"
        self._set("kVTCompressionPropertyKey_ColorPrimaries", "ColorPrimaries",
                  _k(CM, "kCMFormatDescriptionColorPrimaries_ITU_R_709_2", bt709))
        self._set("kVTCompressionPropertyKey_TransferFunction", "TransferFunction",
                  _k(CM, "kCMFormatDescriptionTransferFunction_ITU_R_709_2", bt709))
        self._set("kVTCompressionPropertyKey_YCbCrMatrix", "YCbCrMatrix",
                  _k(CM, "kCMFormatDescriptionYCbCrMatrix_ITU_R_709_2", bt709))

    def _copy_bool(self, name: str, fallback: str):
        try:
            res = self.VT.VTSessionCopyProperty(self.session, _k(self.VT, name, fallback), None, None)
            if isinstance(res, tuple) and int(res[0]) == 0:
                return bool(res[1])
        except Exception as exc:
            log.debug("could not read %s: %s", fallback, exc)
        return None

    # ---- encoding ----------------------------------------------------------------
    def _on_output_cb(self, refcon, frame_refcon, status, flags, sbuf):
        self._callback_out.append((status, flags, sbuf))

    def _pts(self):
        ms = int((time.monotonic() - self._t0) * 1000)
        if ms <= self._last_ms:
            ms = self._last_ms + 1
        self._last_ms = ms
        return self.CM.CMTimeMake(ms, 1000)

    def encode(self, pixel_buffer, force_key: bool = False) -> Optional[Tuple[bytes, bool]]:
        """Encodes a pixel buffer; (Annex B bytes, is key frame) or None (frame dropped)."""
        VT, CM = self.VT, self.CM
        pts = self._pts()
        props = {_k(VT, "kVTEncodeFrameOptionKey_ForceKeyFrame", "ForceKeyFrame"): True} if force_key else None
        invalid = _k(CM, "kCMTimeInvalid", None)
        if invalid is None:
            invalid = CM.CMTimeMake(0, 0)
        if self.mode == "handler":
            out = []

            def handler(status, flags, sbuf):
                out.append((status, flags, sbuf))

            res = VT.VTCompressionSessionEncodeFrameWithOutputHandler(
                self.session, pixel_buffer, pts, invalid, props, None, handler)
        else:
            self._callback_out = out = []
            res = VT.VTCompressionSessionEncodeFrame(self.session, pixel_buffer, pts, invalid, props, None, None)
        st = _ok(res)
        if st != 0:
            raise RuntimeError(f"VTCompressionSessionEncodeFrame error {st}")
        st = _ok(VT.VTCompressionSessionCompleteFrames(self.session, pts))
        if st != 0:
            raise RuntimeError(f"VTCompressionSessionCompleteFrames error {st}")
        chunks, key = [], False
        for status, _flags, sbuf in out:
            if int(status) != 0:
                raise RuntimeError(f"the encoder reported an error: {int(status)}")
            if sbuf is None:
                continue                       # frame dropped
            data, k = self._sample_to_annexb(sbuf)
            if data:
                chunks.append(data)
                key = key or k
        if not chunks:
            return None
        return b"".join(chunks), key

    def set_bitrate(self, kbps: int) -> None:
        """Change the target bitrate while running (VideoToolbox applies it from the next frame)."""
        if self.session is not None and self._set("kVTCompressionPropertyKey_AverageBitRate", "AverageBitRate",
                                                  int(kbps) * 1000):
            self.kbps = int(kbps)

    def set_fps(self, fps: int) -> None:
        if self.session is not None:
            self._set("kVTCompressionPropertyKey_ExpectedFrameRate", "ExpectedFrameRate", int(fps))

    @property
    def supports_async(self) -> bool:
        return self.mode == "handler"

    def submit(self, pixel_buffer, force_key: bool, on_done) -> None:
        """Asynchronous encoding: the frame is handed to the encoder without waiting. When the output is ready,
        on_done(data|None, is_key, error|None) is called on VideoToolbox's thread.
        In low-latency mode the output arrives as soon as the frame is encoded (in order)."""
        VT, CM = self.VT, self.CM
        pts = self._pts()
        props = {_k(VT, "kVTEncodeFrameOptionKey_ForceKeyFrame", "ForceKeyFrame"): True} if force_key else None
        invalid = _k(CM, "kCMTimeInvalid", None)
        if invalid is None:
            invalid = CM.CMTimeMake(0, 0)

        def handler(status, flags, sbuf):
            try:
                if int(status) != 0:
                    on_done(None, False, f"the encoder reported an error: {int(status)}")
                elif sbuf is None:
                    on_done(None, False, None)          # frame dropped
                else:
                    data, key = self._sample_to_annexb(sbuf)
                    on_done(data or None, key, None)
            except Exception as exc:                    # never leak an exception into the VT thread
                on_done(None, False, f"could not process the output: {exc}")

        st = _ok(VT.VTCompressionSessionEncodeFrameWithOutputHandler(
            self.session, pixel_buffer, pts, invalid, props, None, handler))
        if st != 0:
            raise RuntimeError(f"VTCompressionSessionEncodeFrame error {st}")

    def flush(self) -> None:
        """Deliver all pending frames (so the last frame does not get stuck when things go idle)."""
        invalid = _k(self.CM, "kCMTimeInvalid", None)
        if invalid is None:
            invalid = self.CM.CMTimeMake(0, 0)
        if self.session is not None:
            self.VT.VTCompressionSessionCompleteFrames(self.session, invalid)

    def _sample_to_annexb(self, sbuf) -> Tuple[bytes, bool]:
        CM = self.CM
        bb = CM.CMSampleBufferGetDataBuffer(sbuf)
        if bb is None:
            return b"", False
        n = int(CM.CMBlockBufferGetDataLength(bb))
        res = CM.CMBlockBufferCopyDataBytes(bb, 0, n, None)
        st, raw = (res[0], res[1]) if isinstance(res, tuple) else (0, res)
        if int(st) != 0 or raw is None:
            raise RuntimeError(f"CMBlockBufferCopyDataBytes error {st}")
        raw = bytes(raw)
        key = self._is_sync(sbuf)
        sps_pps, nal_len = b"", 4
        fd = CM.CMSampleBufferGetFormatDescription(sbuf)
        if fd is not None:
            sps_pps, nal_len = self._param_sets(fd)
        data = avcc_to_annexb(raw, nal_len)
        if key and sps_pps:
            data = sps_pps + data
        return data, key or is_keyframe(data)

    def _is_sync(self, sbuf) -> bool:
        try:
            atts = self.CM.CMSampleBufferGetSampleAttachmentsArray(sbuf, False)
            if atts is None or len(atts) == 0:
                return True                    # no attachments = a sync frame
            a = atts[0]
            key = _k(self.CM, "kCMSampleAttachmentKey_NotSync", "NotSync")
            try:
                v = a.objectForKey_(key)
            except Exception:
                v = a.get(key) if hasattr(a, "get") else None
            return not bool(v)
        except Exception as exc:
            log.debug("could not read the key frame information: %s", exc)
            return False

    def _param_sets(self, fd) -> Tuple[bytes, int]:
        CM = self.CM
        atoms = CM.CMFormatDescriptionGetExtension(
            fd, _k(CM, "kCMFormatDescriptionExtension_SampleDescriptionExtensionAtoms",
                   "SampleDescriptionExtensionAtoms"))
        if atoms is None:
            return b"", 4
        try:
            avcc = atoms.objectForKey_("avcC")
        except Exception:
            avcc = atoms["avcC"]
        if avcc is None:
            return b"", 4
        avcc = bytes(avcc)
        if self._avcc_cache[0] != avcc:
            sps, pps, nal_len = parse_avcc_config(avcc)
            self._avcc_cache = (avcc, (build_annexb(sps + pps), nal_len))
        return self._avcc_cache[1]

    def close(self) -> None:
        s, self.session = self.session, None
        if s is not None:
            try:
                self.VT.VTCompressionSessionCompleteFrames(s, _k(self.CM, "kCMTimeInvalid", self.CM.CMTimeMake(0, 0)))
            except Exception:
                pass
            try:
                self.VT.VTCompressionSessionInvalidate(s)
            except Exception:
                pass


def pixel_buffer_size(pb) -> Tuple[int, int]:
    import Quartz
    return int(Quartz.CVPixelBufferGetWidth(pb)), int(Quartz.CVPixelBufferGetHeight(pb))
