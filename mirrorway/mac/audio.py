"""Capturing the sound that plays on the Mac (ScreenCaptureKit, macOS 13+) and muting the Mac's own speakers.

Audio is captured by an SCStream separate from the video (video 2x2 pixels, 1 frame per second: only needed for the
filter). Mirrorway's own sound is excluded. Output: 48 kHz stereo 16-bit interleaved PCM (mirrorway.audio.to_s16).
"""
from __future__ import annotations

import logging
import subprocess
import threading
from typing import Callable, Optional

from .. import audio as A
from ..i18n import T

log = logging.getLogger("mirrorway.audio")

_AUDIO_OUT_CLS = None
FLAG_FLOAT, FLAG_NON_INTERLEAVED = 0x1, 0x20          # kAudioFormatFlagIsFloat, ...IsNonInterleaved


def _audio_output_class():
    global _AUDIO_OUT_CLS
    if _AUDIO_OUT_CLS is not None:
        return _AUDIO_OUT_CLS
    import objc
    import ScreenCaptureKit as SCK
    from Foundation import NSObject
    audio_type = int(getattr(SCK, "SCStreamOutputTypeAudio", 1))

    def _handle(self, stream, sbuf, otype):
        owner = getattr(self, "owner", None)
        if owner is not None and int(otype) == audio_type:
            owner._on_audio(sbuf)

    try:
        proto = objc.protocolNamed("SCStreamOutput")

        class MirrorwayAudioOutput(NSObject, protocols=[proto]):
            def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, otype):
                _handle(self, stream, sbuf, otype)
    except Exception:
        class MirrorwayAudioOutputAlt(NSObject):
            @objc.typedSelector(b"v@:@^{opaqueCMSampleBuffer=}q")
            def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, otype):
                _handle(self, stream, sbuf, otype)
        MirrorwayAudioOutput = MirrorwayAudioOutputAlt
    _AUDIO_OUT_CLS = MirrorwayAudioOutput
    return _AUDIO_OUT_CLS


# ScreenCaptureKit's documented output: 32-bit float, channels one after the other (non-interleaved)
SCK_DEFAULT = (A.RATE, A.CHANNELS, FLAG_FLOAT | FLAG_NON_INTERLEAVED, 32)
_ASBD_ORDER = ("mSampleRate", "mFormatID", "mFormatFlags", "mBytesPerPacket", "mFramesPerPacket", "mBytesPerFrame",
               "mChannelsPerFrame", "mBitsPerChannel")


def asbd_info(asbd) -> Optional[tuple]:
    """AudioStreamBasicDescription -> (rate, channels, flags, bits). pyobjc may return it as a struct, a struct pointer
    (asbd[0] is the struct) or a flat tuple of its fields (asbd[0] is the sample rate) — real macOS gave the flat tuple."""
    if asbd is None:
        return None
    if not hasattr(asbd, "mSampleRate"):
        try:
            first = asbd[0]
        except Exception:
            return None
        if hasattr(first, "mSampleRate"):
            asbd = first
        else:
            try:
                vals = [asbd[i] for i in range(len(_ASBD_ORDER))]
            except Exception:
                return None
            asbd = dict(zip(_ASBD_ORDER, vals))
    get = asbd.get if isinstance(asbd, dict) else (lambda k: getattr(asbd, k))
    try:
        rate, ch = int(get("mSampleRate")), int(get("mChannelsPerFrame"))
        flags, bits = int(get("mFormatFlags")), int(get("mBitsPerChannel")) or 32
    except Exception:
        return None
    if rate <= 0 or ch <= 0 or bits not in (16, 32, 64):
        return None
    return rate, ch, flags, bits


def sample_buffer_pcm(sbuf, detail: Optional[dict] = None) -> Optional[tuple]:
    """CMSampleBuffer -> (16-bit interleaved PCM, sample rate, channels). None if it cannot be read.
    If detail is given, the format that was read is written into it (for the log)."""
    import CoreMedia as CM
    frames = int(CM.CMSampleBufferGetNumSamples(sbuf))
    if frames <= 0:
        return None
    info = None
    try:
        fd = CM.CMSampleBufferGetFormatDescription(sbuf)
        info = asbd_info(CM.CMAudioFormatDescriptionGetStreamBasicDescription(fd))
    except Exception:
        info = None
    rate, ch, flags, bits = info or SCK_DEFAULT      # unreadable: the format we asked for (48 kHz stereo float, planar)
    bb = CM.CMSampleBufferGetDataBuffer(sbuf)
    if bb is None:
        return None
    n = int(CM.CMBlockBufferGetDataLength(bb))
    try:
        res = CM.CMBlockBufferCopyDataBytes(bb, 0, n, None)
        err, data = res if isinstance(res, tuple) else (0, res)
    except TypeError:
        buf = bytearray(n)
        err = CM.CMBlockBufferCopyDataBytes(bb, 0, n, buf)
        data = bytes(buf)
    if err != 0 or not data:
        return None
    data = bytes(data)
    if detail is not None:
        detail.update(rate=rate, channels=ch, float=bool(flags & FLAG_FLOAT),
                      planar=bool(flags & FLAG_NON_INTERLEAVED), bits=bits, frames=frames, bytes=len(data),
                      assumed=info is None)
    pcm = A.to_s16(data, frames, ch, bool(flags & FLAG_FLOAT), bool(flags & FLAG_NON_INTERLEAVED), bits)
    return pcm, rate, A.CHANNELS                   # to_s16 always returns stereo


def chosen_apps(running, wanted) -> list:
    """The running apps (SCRunningApplication) whose bundle id was chosen, in the order ScreenCaptureKit lists them."""
    wanted = set(wanted or ())
    out = []
    for app in running or ():
        try:
            if str(app.bundleIdentifier() or "") in wanted:
                out.append(app)
        except Exception:
            continue
    return out


def _shareable_content(wait: float = 8.0):
    import ScreenCaptureKit as SCK
    box, done = {}, threading.Event()

    def on_content(content, error):
        box["content"], box["error"] = content, error
        done.set()
    SCK.SCShareableContent.getShareableContentWithCompletionHandler_(on_content)
    if not done.wait(wait):
        raise RuntimeError(T("ScreenCaptureKit did not respond"))
    content = box.get("content")
    if box.get("error") is not None or content is None or not content.displays():
        raise RuntimeError(T("no Screen Recording permission, or the content could not be read: {error}").format(error=box.get('error')))
    return content


class SCKAudioCapture:
    """start(on_pcm) -> on_pcm(pcm, rate, channels) is called on ScreenCaptureKit's own queue.
    apps: None = the sound of every app; a list of bundle ids = only those apps (3.2.4). The filter follows apps
    that start or quit (refresh()) and a changed choice (set_apps()) without restarting the stream."""

    name = "ScreenCaptureKit"

    def __init__(self, apps: Optional[list] = None):
        self.stream = None
        self.output = None
        self.on_pcm: Optional[Callable] = None
        self.errors = 0
        self.buffers = 0
        self.format = None
        self.apps = list(apps) if apps is not None else None
        self.capturing: Optional[frozenset] = None          # the chosen apps the filter holds now (None: every app)

    def _filter(self, content):
        import ScreenCaptureKit as SCK
        display = content.displays()[0]
        if self.apps is None:
            self.capturing = None
            return SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])
        apps = chosen_apps(content.applications(), self.apps)
        self.capturing = frozenset(str(a.bundleIdentifier()) for a in apps)
        return SCK.SCContentFilter.alloc().initWithDisplay_includingApplications_exceptingWindows_(display, apps, [])

    def set_apps(self, apps: Optional[list]) -> None:
        """A new choice of apps (None: all): applied to the running stream."""
        self.apps = list(apps) if apps is not None else None
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> bool:
        """With chosen apps: when one of them started or quit, update the stream's filter. True if updated."""
        s = self.stream
        if s is None or (self.apps is None and not force):
            return False
        content = _shareable_content()
        if not force and self.apps is not None:
            now = frozenset(str(a.bundleIdentifier()) for a in chosen_apps(content.applications(), self.apps))
            if now == self.capturing:
                return False
        flt = self._filter(content)
        done = threading.Event()
        s.updateContentFilter_completionHandler_(flt, lambda error: done.set())
        done.wait(5)
        log.info("Audio from %s", "every app" if self.capturing is None else
                 (", ".join(sorted(self.capturing)) or "none of the chosen apps (none is running)"))
        return True

    def start(self, on_pcm: Callable[[bytes, int, int], None]) -> None:
        import CoreMedia
        import ScreenCaptureKit as SCK
        self.on_pcm = on_pcm
        content = _shareable_content()
        flt = self._filter(content)
        if self.capturing is not None:
            log.info("Audio only from the chosen apps: %s",
                     ", ".join(sorted(self.capturing)) or "none is running yet")
        cfg = SCK.SCStreamConfiguration.alloc().init()
        cfg.setCapturesAudio_(True)
        cfg.setSampleRate_(A.RATE)
        cfg.setChannelCount_(A.CHANNELS)
        cfg.setExcludesCurrentProcessAudio_(True)
        cfg.setWidth_(2)
        cfg.setHeight_(2)
        cfg.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, 1))
        cfg.setShowsCursor_(False)
        self.output = _audio_output_class().alloc().init()
        self.output.owner = self
        self.stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(flt, cfg, None)
        queue = None
        try:
            import dispatch
            queue = dispatch.dispatch_queue_create(b"mirrorway.audio", None)
        except Exception:
            queue = None
        res = self.stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self.output, int(getattr(SCK, "SCStreamOutputTypeAudio", 1)), queue, None)
        ok = res[0] if isinstance(res, tuple) else res
        if not ok:
            raise RuntimeError(f"Could not add the audio output: {res}")
        started, err_box = threading.Event(), {}

        def on_start(error):
            err_box["error"] = error
            started.set()
        self.stream.startCaptureWithCompletionHandler_(on_start)
        if not started.wait(8):
            raise RuntimeError(T("audio capture could not start (timed out)"))
        if err_box.get("error") is not None:
            raise RuntimeError(T("audio capture could not start: {error}").format(error=err_box['error']))

    def _on_audio(self, sbuf) -> None:
        detail = {} if self.format is None else None
        try:
            got = sample_buffer_pcm(sbuf, detail)
        except Exception as exc:
            self.errors += 1
            if self.errors <= 3:
                log.warning("Could not read an audio sample: %s", exc)
            return
        if got is None:
            return
        pcm, rate, ch = got
        self.buffers += 1
        if self.format is None:
            self.format = (rate, ch)
            if detail:
                log.info("Audio format: %d Hz, %d channels, %s %d bit%s, %d frames = %d bytes%s", detail["rate"],
                         detail["channels"], "float" if detail["float"] else "integer", detail["bits"],
                         ", planar" if detail["planar"] else "", detail["frames"], detail["bytes"],
                         " (format unreadable, assumed)" if detail["assumed"] else "")
        cb = self.on_pcm
        if cb is not None and pcm:
            cb(pcm, rate, ch)

    def stop(self) -> None:
        s, self.stream = self.stream, None
        self.on_pcm = None
        if s is None:
            return
        done = threading.Event()
        try:
            s.stopCaptureWithCompletionHandler_(lambda error: done.set())
            done.wait(3)
        except Exception:
            pass


# ---- Muting the Mac's own sound (while it plays on Windows) --------------------------------------------------
OSASCRIPT = "/usr/bin/osascript"


def output_muted(run=subprocess.run) -> Optional[bool]:
    try:
        r = run([OSASCRIPT, "-e", "output muted of (get volume settings)"], capture_output=True, text=True,
                timeout=5)
        out = (r.stdout or "").strip().lower()
        return True if out == "true" else False if out == "false" else None
    except Exception:
        return None


def set_output_muted(muted: bool, run=subprocess.run) -> bool:
    try:
        r = run([OSASCRIPT, "-e", f"set volume output muted {'true' if muted else 'false'}"], capture_output=True,
                text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False
