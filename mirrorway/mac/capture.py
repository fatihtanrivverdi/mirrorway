"""Mac screen capture. Three methods, tried in order:

1. ScreenCaptureKit (macOS 12.3+, the most efficient; scales on the GPU, produces frames only on change)
2. mss (screenshots through CoreGraphics)
3. the `screencapture` command (slow but works on every version)

All return a PIL.Image. For diagnostics:  python -m mirrorway.mac.capture
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import time

from ..i18n import T
from typing import List, Optional, Tuple

from PIL import Image

try:
    import numpy as np
except Exception:  # without numpy, frames come back as PIL images
    np = None

log = logging.getLogger("mirrorway.capture")

# fps/q/subsampling: JPEG; h264_fps/kbps: H.264 starting values (the hardware encoder handles a much higher fps);
# min_kbps/max_kbps: the bitrate range of adaptive quality
QUALITY_PRESETS = {
    "low": {"max_w": 1280, "fps": 12, "q": 60, "subsampling": 2, "h264_fps": 30, "kbps": 4000,
              "min_kbps": 800, "max_kbps": 8000},
    "medium": {"max_w": 1600, "fps": 20, "q": 72, "subsampling": 2, "h264_fps": 30, "kbps": 8000,
             "min_kbps": 1000, "max_kbps": 16000},
    "high": {"max_w": 1920, "fps": 30, "q": 82, "subsampling": 0, "h264_fps": 60, "kbps": 14000,
               "min_kbps": 1500, "max_kbps": 30000},
}


def list_displays() -> List[dict]:
    """[{index, id, w, h, main}] — in logical (point) sizes."""
    import Quartz
    out = []
    try:
        err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
        ids = list(ids[:count]) if err == 0 and ids else [Quartz.CGMainDisplayID()]
    except Exception:
        ids = [Quartz.CGMainDisplayID()]
    main = Quartz.CGMainDisplayID()
    for i, did in enumerate(ids):
        b = Quartz.CGDisplayBounds(did)
        pw, ph = int(Quartz.CGDisplayPixelsWide(did)), int(Quartz.CGDisplayPixelsHigh(did))
        if pw <= 0 or ph <= 0:          # the mode of a virtual display opened by another process may be unreadable here
            pw, ph = int(b.size.width), int(b.size.height)
        out.append({"index": i, "id": int(did), "w": int(b.size.width), "h": int(b.size.height),
                    "main": did == main, "pw": pw, "ph": ph})
    return out


def _bgra_frame(data: bytes, w: int, h: int, bpr: int):
    """Raw BGRA bytes: an (h, w, 4) array with numpy (no conversion), a PIL image otherwise."""
    if np is not None:
        arr = np.frombuffer(data, dtype=np.uint8).reshape(h, bpr // 4, 4)
        if bpr != w * 4:
            arr = np.ascontiguousarray(arr[:, :w, :])
        return arr
    return Image.frombuffer("RGB", (w, h), data, "raw", "BGRX", bpr, 1)


def frame_size(frame) -> Tuple[int, int]:
    if np is not None and isinstance(frame, np.ndarray):
        return frame.shape[1], frame.shape[0]
    return frame.size


def to_array(frame, max_w: int):
    """Make the frame (array, colour layout): BGRA array -> 'BGRX'; PIL -> 'RGB'. Downscale if needed."""
    if isinstance(frame, np.ndarray):
        w, h = frame.shape[1], frame.shape[0]
        if not (max_w and w > max_w):
            return frame, "BGRX"
        frame = Image.frombuffer("RGB", (w, h), frame.tobytes(), "raw", "BGRX", 0, 1)
    img = fit(frame.convert("RGB") if frame.mode != "RGB" else frame, max_w)
    return np.asarray(img), "RGB"


def to_image(frame) -> Image.Image:
    if np is not None and isinstance(frame, np.ndarray):
        h, w = frame.shape[:2]
        return Image.frombuffer("RGB", (w, h), frame.tobytes(), "raw", "BGRX", 0, 1)
    return frame


def _target_size(w: int, h: int, max_w: int) -> Tuple[int, int]:
    """Target size; always even (H.264 4:2:0 and YUV textures need even sizes)."""
    if max_w and w > max_w:
        return max_w // 2 * 2, max(2, int(round(h * max_w / w)) // 2 * 2)
    return max(2, w // 2 * 2), max(2, h // 2 * 2)


PIXFMT_BGRA = 0x42475241    # kCVPixelFormatType_32BGRA
PIXFMT_420V = 0x34323076    # kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange (NV12, 16-235)


class CaptureBackend:
    name = "base"
    pixel_format = "bgra"
    self_paced = True     # grab() limits the frame rate itself (waits for a new frame / the interval)

    def start(self, display_index: int, max_w: int, fps: int, pixel_format: str = "bgra") -> None:
        raise NotImplementedError

    supports_raw = False   # can it hand out raw pixel buffers (ScreenCaptureKit only)

    def grab(self, timeout: float = 1.0) -> Optional[Image.Image]:
        """Returns a new frame if there is one, else None (the screen did not change)."""
        raise NotImplementedError

    def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 1) ScreenCaptureKit
# ---------------------------------------------------------------------------
_OUTPUT_CLS = None


def _output_class():
    """The SCStreamOutput class (Objective-C classes can only be defined once)."""
    global _OUTPUT_CLS
    if _OUTPUT_CLS is not None:
        return _OUTPUT_CLS
    import objc
    import ScreenCaptureKit as SCK
    from Foundation import NSObject

    screen_type = int(getattr(SCK, "SCStreamOutputTypeScreen", 0))

    def _handle(self, stream, sbuf, otype):
        owner = getattr(self, "owner", None)
        if owner is not None and int(otype) == screen_type:
            owner._on_sample(sbuf)

    try:
        proto = objc.protocolNamed("SCStreamOutput")

        class MirrorwayStreamOutput(NSObject, protocols=[proto]):
            def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, otype):
                _handle(self, stream, sbuf, otype)
    except Exception:
        class MirrorwayStreamOutputAlt(NSObject):  # if the protocol cannot be found, give the signature by hand
            @objc.typedSelector(b"v@:@^{opaqueCMSampleBuffer=}q")
            def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, otype):
                _handle(self, stream, sbuf, otype)

        MirrorwayStreamOutput = MirrorwayStreamOutputAlt

    _OUTPUT_CLS = MirrorwayStreamOutput
    return _OUTPUT_CLS


class SCKBackend(CaptureBackend):
    name = "ScreenCaptureKit"

    def __init__(self):
        self.stream = None
        self.output = None
        self._cond = threading.Condition()
        self._latest: Optional[Image.Image] = None
        self._latest_pb = None              # the last raw pixel buffer (for the H.264 encoder)
        self._seq = 0
        self._returned = 0
        self._ci_ctx = None
        self._as_buffer_ok = True

    def start(self, display_index: int, max_w: int, fps: int, pixel_format: str = "bgra",
              display_id: Optional[int] = None) -> None:
        import CoreMedia
        import ScreenCaptureKit as SCK

        displays = list_displays()
        if display_id:
            want = next((d for d in displays if d["id"] == int(display_id)), None) or {"id": int(display_id),
                                                                                      "pw": 0, "ph": 0}
        else:
            if display_index >= len(displays):
                display_index = 0
            want = displays[display_index]

        box = {}
        done = threading.Event()

        def on_content(content, error):
            box["content"], box["error"] = content, error
            done.set()

        sc_display = None
        for attempt in range(4 if display_id else 1):
            if attempt:
                time.sleep(0.4)          # a newly opened virtual display may show up in the list a little late
            box.clear()
            done.clear()
            SCK.SCShareableContent.getShareableContentWithCompletionHandler_(on_content)
            if not done.wait(8):
                raise RuntimeError(T("ScreenCaptureKit did not respond"))
            if box.get("error") is not None or box.get("content") is None:
                raise RuntimeError(T("no Screen Recording permission, or the content could not be read: {error}").format(error=box.get('error')))
            for d in box["content"].displays():
                if int(d.displayID()) == want["id"]:
                    sc_display = d
            if sc_display is not None:
                break
        if sc_display is None:
            if display_id:
                raise RuntimeError(f"Display {display_id} is not in ScreenCaptureKit's list")
            sc_display = box["content"].displays()[0]

        pw, ph = want["pw"], want["ph"]
        if pw <= 0 or ph <= 0:
            pw, ph = int(sc_display.width()), int(sc_display.height())
        tw, th = _target_size(pw, ph, max_w)

        flt = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(sc_display, [])
        cfg = SCK.SCStreamConfiguration.alloc().init()
        cfg.setWidth_(tw)
        cfg.setHeight_(th)
        cfg.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, max(1, int(fps))))
        if pixel_format == "nv12":
            # Straight to the hardware H.264 encoder: the GPU does the colour conversion during capture
            cfg.setPixelFormat_(PIXFMT_420V)
            try:
                import Quartz
                cfg.setColorMatrix_(getattr(Quartz, "kCGDisplayStreamYCbCrMatrix_ITU_R_709_2", "ITU_R_709_2"))
            except Exception as exc:
                log.info("Could not set the colour matrix: %s", exc)
            self.pixel_format = "nv12"
        else:
            cfg.setPixelFormat_(PIXFMT_BGRA)
            self.pixel_format = "bgra"
        cfg.setShowsCursor_(True)
        cfg.setQueueDepth_(6)   # on the H.264 path, frames in the encoder and the last frame also hold buffers

        self.output = _output_class().alloc().init()
        self.output.owner = self
        self.stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(flt, cfg, None)

        queue = None
        try:
            import dispatch  # pyobjc-framework-libdispatch
            queue = dispatch.dispatch_queue_create(b"mirrorway.capture", None)
        except Exception:
            queue = None
        res = self.stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self.output, SCK.SCStreamOutputTypeScreen, queue, None)
        ok = res[0] if isinstance(res, tuple) else res
        if not ok:
            raise RuntimeError(f"Could not add the stream output: {res}")

        started = threading.Event()
        err_box = {}

        def on_start(error):
            err_box["error"] = error
            started.set()

        self.stream.startCaptureWithCompletionHandler_(on_start)
        if not started.wait(8):
            raise RuntimeError(T("capture could not start (timed out)"))
        if err_box.get("error") is not None:
            raise RuntimeError(T("capture could not start: {error}").format(error=err_box['error']))
        # The first frame must arrive
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq > 0, timeout=4):
                self.stop()
                raise RuntimeError("ScreenCaptureKit produced no frame")

    def _pixels_from_buffer(self, pb) -> Optional[Image.Image]:
        import Quartz
        if self._as_buffer_ok and self.pixel_format == "bgra":
            Quartz.CVPixelBufferLockBaseAddress(pb, 1)  # kCVPixelBufferLock_ReadOnly
            try:
                base = Quartz.CVPixelBufferGetBaseAddress(pb)
                bpr = int(Quartz.CVPixelBufferGetBytesPerRow(pb))
                w = int(Quartz.CVPixelBufferGetWidth(pb))
                h = int(Quartz.CVPixelBufferGetHeight(pb))
                try:
                    data = bytes(base.as_buffer(bpr * h))
                except Exception:
                    self._as_buffer_ok = False
                    data = None
            finally:
                Quartz.CVPixelBufferUnlockBaseAddress(pb, 1)
            if data is not None:
                return _bgra_frame(data, w, h, bpr)
        # Fallback path: CIImage -> CGImage -> bytes
        if self._ci_ctx is None:
            self._ci_ctx = Quartz.CIContext.contextWithOptions_(None)
        ci = Quartz.CIImage.imageWithCVPixelBuffer_(pb)
        try:  # ask for BGRA; the default may be RGBA (colours would swap)
            cg = self._ci_ctx.createCGImage_fromRect_format_colorSpace_(
                ci, ci.extent(), Quartz.kCIFormatBGRA8, Quartz.CGColorSpaceCreateDeviceRGB())
        except Exception:
            cg = self._ci_ctx.createCGImage_fromRect_(ci, ci.extent())
        w, h = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
        bpr = Quartz.CGImageGetBytesPerRow(cg)
        data = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(cg)))
        return _bgra_frame(data, int(w), int(h), int(bpr))

    supports_raw = True

    def _on_sample(self, sbuf) -> None:
        try:
            import CoreMedia
            pb = CoreMedia.CMSampleBufferGetImageBuffer(sbuf)
            if pb is None:          # the screen did not change (idle frame)
                return
            with self._cond:        # conversion happens (if needed) on the thread that takes the frame
                self._latest_pb = pb
                self._seq += 1
                self._cond.notify_all()
        except Exception:
            log.exception("could not process a frame")

    def grab(self, timeout: float = 1.0, raw: bool = False):
        """New frame: a CVPixelBuffer if raw=True (straight to the H.264 encoder), else a BGRA array."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq > self._returned, timeout=timeout):
                return None
            self._returned = self._seq
            pb = self._latest_pb
        if pb is None:
            return None
        if raw:
            return pb
        img = self._pixels_from_buffer(pb)
        self._latest = img
        return img

    def latest(self) -> Optional[Image.Image]:
        with self._cond:
            return self._latest

    def stop(self) -> None:
        s, self.stream = self.stream, None
        if s is not None:
            done = threading.Event()
            try:
                s.stopCaptureWithCompletionHandler_(lambda err: done.set())
                done.wait(3)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 2) mss
# ---------------------------------------------------------------------------
class MSSBackend(CaptureBackend):
    name = "mss"

    def __init__(self):
        self.sct = None
        self.mon = None
        self.interval = 0.05
        self._prev = None
        self._last_t = 0.0

    def start(self, display_index: int, max_w: int, fps: int, pixel_format: str = "bgra") -> None:
        import mss
        self.sct = mss.mss()
        mons = self.sct.monitors
        idx = display_index + 1
        self.mon = mons[idx] if idx < len(mons) else mons[1]
        self.interval = 1.0 / max(1, fps)
        if self.grab(timeout=0) is None and self._prev is None:
            raise RuntimeError("mss got no frame")
        self._prev = None   # send the first frame again

    def grab(self, timeout: float = 1.0) -> Optional[Image.Image]:
        deadline = time.monotonic() + timeout
        while True:
            wait = self.interval - (time.monotonic() - self._last_t)
            if wait > 0:
                time.sleep(wait)
            self._last_t = time.monotonic()
            raw = self.sct.grab(self.mon)
            data = raw.bgra
            if data != self._prev:
                self._prev = data
                return Image.frombytes("RGB", raw.size, data, "raw", "BGRX")
            if time.monotonic() >= deadline:
                return None

    def stop(self) -> None:
        try:
            if self.sct is not None:
                self.sct.close()
        except Exception:
            pass
        self.sct = None


# ---------------------------------------------------------------------------
# 3) the screencapture command
# ---------------------------------------------------------------------------
class ScreencaptureBackend(CaptureBackend):
    name = "screencapture"

    def __init__(self):
        fd, self.path = tempfile.mkstemp(prefix="mirrorway-", suffix=".jpg")
        os.close(fd)
        self.display_no = 1
        self._prev = None

    def start(self, display_index: int, max_w: int, fps: int, pixel_format: str = "bgra") -> None:
        self.display_no = display_index + 1
        if self._shot() is None:
            raise RuntimeError("screencapture did not run")
        self._prev = None

    def _shot(self) -> Optional[Image.Image]:
        r = subprocess.run(["screencapture", "-x", "-C", "-D", str(self.display_no), "-t", "jpg", self.path],
                           capture_output=True, timeout=10)
        if r.returncode != 0 or not os.path.getsize(self.path):
            return None
        img = Image.open(self.path)
        img.load()
        return img.convert("RGB")

    def grab(self, timeout: float = 1.0) -> Optional[Image.Image]:
        img = self._shot()
        if img is None:
            return None
        sig = img.resize((64, 40)).tobytes()
        if sig == self._prev:
            time.sleep(min(timeout, 0.3))
            return None
        self._prev = sig
        return img

    def stop(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass


BACKENDS = [SCKBackend, MSSBackend, ScreencaptureBackend]


def open_best(display_index: int, max_w: int, fps: int, prefer: Optional[str] = None,
              pixel_format: str = "bgra", display_id: Optional[int] = None) -> CaptureBackend:
    errors = []
    order = list(BACKENDS)
    if prefer:
        order.sort(key=lambda b: 0 if b.name == prefer else 1)
    extra = {}
    if display_id:
        order = [SCKBackend]                 # a specific display (the second screen) works with ScreenCaptureKit only
        extra = {"display_id": int(display_id)}
    for cls in order:
        formats = [pixel_format] + (["bgra"] if pixel_format != "bgra" and getattr(cls, "supports_raw", False) else [])
        for fmt in formats:
            b = cls()
            try:
                b.start(display_index, max_w, fps, pixel_format=fmt, **extra)
                log.info("Screen capture method: %s (%s)", b.name, b.pixel_format)
                return b
            except Exception as exc:
                errors.append(f"{cls.name} ({fmt}): {exc}")
                log.warning("%s (%s) unavailable: %s", cls.name, fmt, exc)
                try:
                    b.stop()
                except Exception:
                    pass
    raise RuntimeError(T("No screen capture method worked. Check the Screen Recording permission.") + "\n"
                       + "\n".join(errors))


def fit(img, max_w: int) -> Image.Image:
    img = to_image(img)
    w, h = img.size
    if max_w and w > max_w:
        tw, th = _target_size(w, h, max_w)
        factor = w // tw
        if factor >= 2:
            img = img.reduce(factor)
        if img.size != (tw, th):
            img = img.resize((tw, th), Image.BILINEAR)
    return img


def _diagnose() -> None:
    import io
    logging.basicConfig(level=logging.INFO)
    print("Displays:", list_displays())
    for cls in BACKENDS:
        b = cls()
        try:
            t0 = time.monotonic()
            b.start(0, 1600, 20)
            n, size = 0, 0
            t1 = time.monotonic()
            while time.monotonic() - t1 < 3:
                img = b.grab(timeout=0.5)
                if img is not None:
                    buf = io.BytesIO()
                    fit(img, 1600).save(buf, "JPEG", quality=72)
                    n += 1
                    size = buf.tell()
            print(f"[OK] {cls.name}: start {t1 - t0:.2f} s, {n} new frames in 3 s, last JPEG {size // 1024} KB"
                  " (few frames if the screen is still; move the mouse)")
        except Exception as exc:
            print(f"[ERROR] {cls.name}: {exc}")
        finally:
            b.stop()


if __name__ == "__main__":
    _diagnose()
