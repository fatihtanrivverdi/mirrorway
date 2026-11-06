"""The window that draws the Mac's screen with the GPU (SDL2; Direct3D 11 on Windows).

H.264 frames are uploaded as YUV planes straight into a GPU texture; colour conversion (BT.709) and scaling to
the window happen on the GPU. Only copying the planes is left for the CPU.
RGB/RGBA images and changed pieces are also supported, for the JPEG stream.

The window runs on its own thread (independent of the Tk main thread). Its interface is the same as the Tk
viewer's (viewer.Viewer); in addition there is submit_video(av.VideoFrame).
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from typing import Callable, Optional

from ..i18n import T

log = logging.getLogger("mirrorway.viewer")
xlog = logging.getLogger("mirrorway.extend")      # second-screen lines also go to the Mac log

YUV_BT709 = 2          # SDL_YUV_CONVERSION_BT709 (limited range)


def _load_sdl():
    import warnings                                    # the DLL from the pysdl2-dll package is used
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import sdl2
    return sdl2


def available() -> bool:
    try:
        _load_sdl()
        return True
    except Exception:
        return False


class SDLViewer:
    MAX_PENDING_PATCHES = 240

    def __init__(self, title: str, on_close: Callable[[], None],
                 rtt_fn: Optional[Callable[[], Optional[float]]] = None, size_ratio: float = 0.7,
                 info_fn: Optional[Callable[[], dict]] = None, prefs=None):
        self.sdl = _load_sdl()
        self.on_close = on_close
        self.rtt_fn = rtt_fn
        self.info_fn = info_fn or (lambda: {})
        self.prefs = prefs                    # get/set: overlay_fullscreen, overlay_window
        self._ov_tex = None
        self._ov_size = (0, 0)
        self._ov_key = None                   # (text, font size) — the texture is renewed when it changes
        self.overlay_draws = 0
        self.size_ratio = size_ratio
        self._base_title = title
        self.title = title
        self._lock = threading.Lock()
        self._pending_video = None           # (av.VideoFrame, meta)
        self._pending_image = None           # (PIL image, meta)
        self._pending_patches = []
        self._overflow = False
        self._status: Optional[str] = T("Connecting…")
        self._tex_size = None                # size of the full frame (texture)
        self._acc = {"frames": 0, "bytes": 0, "enc": 0.0, "dec": 0.0, "disp": 0.0}
        self._stats = {"fps": 0.0, "kbps": 0.0, "enc_ms": 0.0, "decode_ms": 0.0, "display_ms": 0.0,
                       "latency_ms": 0.0, "renderer": "", "decoder": ""}
        self._decoder = ""
        self.renderer_name = ""
        self.frames_drawn = 0
        self.patch_batches = 0
        self.capture_next = False
        self.captured = None
        self.fullscreen = False
        self.closed = False
        self._close_called = False
        self._want_show = False
        self._want_fullscreen: Optional[bool] = None
        self._want_extend = None              # ("set", rect | None): a second-screen layout was requested
        self._want_hidden: Optional[bool] = None
        self.extend_rect = None               # full screen on this monitor while the second screen is on
        self._pre_extend = None               # the window state before the second screen (to go back to)
        self.hidden = False
        self._want_overlay_toggle = False
        self._wake_pending = False
        self._evtype = None
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="mirrorway-sdl")
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("the SDL window did not open (timed out)")
        if self._error:
            raise RuntimeError(self._error)

    # ---- thread-safe interface ------------------------------------------------
    def target_size(self):
        return (0, 0)              # the GPU does the scaling: frames should come at their original size

    def submit_video(self, frame, meta: Optional[dict] = None) -> None:
        with self._lock:
            self._pending_video = (frame, meta or {})
            self._pending_image = None
            self._pending_patches.clear()
            self._overflow = False
        self._wake()

    def submit(self, img, meta: Optional[dict] = None) -> None:
        with self._lock:
            self._pending_image = (img, meta or {})
            self._pending_video = None
            self._pending_patches.clear()
            self._overflow = False
        self._wake()

    def submit_patches(self, size, patches, meta: Optional[dict] = None) -> None:
        with self._lock:
            if len(self._pending_patches) >= self.MAX_PENDING_PATCHES:
                self._overflow = True
                self._pending_patches.clear()
                return
            self._pending_patches.append((tuple(size), patches, meta or {}))
        self._wake()

    def needs_full(self, size) -> bool:
        with self._lock:
            return self._overflow or self._tex_size != ("rgb", tuple(size))

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def set_status(self, text: str) -> None:
        with self._lock:
            self._status = text
        self._wake()

    def show(self) -> None:
        self._want_show = True
        self._wake()

    def toggle_fullscreen(self) -> None:
        self._want_fullscreen = not self.fullscreen
        self._wake()

    def set_extend(self, rect) -> None:
        """Thread-safe. rect: the Windows monitor that is the second screen [x, y, w, h]; None = normal window."""
        rect = list(rect) if rect else None
        pending = self._want_extend
        if (pending is None and rect == self.extend_rect) or (pending is not None and pending[1] == rect):
            return                                   # the same one is already applied or queued
        self._want_extend = ("set", rect)
        self._wake()

    def set_hidden(self, hidden: bool) -> None:
        """Thread-safe: with one monitor, the window minimises when control moves to Windows and returns when it comes back."""
        if bool(hidden) == self.hidden and self._want_hidden is None:
            return
        self._want_hidden = bool(hidden)
        self._wake()

    def close(self) -> None:
        """Closes the window; on_close runs once, on the thread making this call."""
        if self.closed:
            return
        self.closed = True
        self._wake()
        self._fire_close()

    def _fire_close(self) -> None:
        with self._lock:
            if self._close_called:
                return
            self._close_called = True
        try:
            self.on_close()
        except Exception:
            log.exception("viewer close handler")

    def _wake(self) -> None:
        sdl = self.sdl
        if self._evtype is None or self._wake_pending:
            return
        self._wake_pending = True
        ev = sdl.SDL_Event()
        ev.type = self._evtype
        sdl.SDL_PushEvent(ctypes.byref(ev))

    # ---- SDL thread -------------------------------------------------------------
    def _run(self) -> None:
        sdl = self.sdl
        try:
            sdl.SDL_SetHint(sdl.SDL_HINT_RENDER_SCALE_QUALITY, b"1")            # bilinear
            sdl.SDL_SetHint(b"SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS", b"0")
            if sdl.SDL_InitSubSystem(sdl.SDL_INIT_VIDEO) != 0:
                raise RuntimeError("SDL_Init: " + sdl.SDL_GetError().decode("utf-8", "replace"))
            sdl.SDL_SetYUVConversionMode(YUV_BT709)
            mode = sdl.SDL_DisplayMode()
            sw, sh = 1600, 900
            if sdl.SDL_GetDesktopDisplayMode(0, ctypes.byref(mode)) == 0 and mode.w > 0:
                sw, sh = mode.w, mode.h
            w, h = int(sw * self.size_ratio), int(sh * self.size_ratio)
            self.window = sdl.SDL_CreateWindow(self._base_title.encode("utf-8"), sdl.SDL_WINDOWPOS_CENTERED,
                                               sdl.SDL_WINDOWPOS_CENTERED, w, h,
                                               sdl.SDL_WINDOW_RESIZABLE | sdl.SDL_WINDOW_SHOWN)
            if not self.window:
                raise RuntimeError("SDL_CreateWindow: " + sdl.SDL_GetError().decode("utf-8", "replace"))
            self.renderer = sdl.SDL_CreateRenderer(self.window, -1, sdl.SDL_RENDERER_ACCELERATED)
            if not self.renderer:
                log.warning("No accelerated renderer (%s); using the software renderer",
                            sdl.SDL_GetError().decode("utf-8", "replace"))
                self.renderer = sdl.SDL_CreateRenderer(self.window, -1, sdl.SDL_RENDERER_SOFTWARE)
            if not self.renderer:
                raise RuntimeError("SDL_CreateRenderer: " + sdl.SDL_GetError().decode("utf-8", "replace"))
            info = sdl.SDL_RendererInfo()
            sdl.SDL_GetRendererInfo(self.renderer, ctypes.byref(info))
            self.renderer_name = (info.name or b"?").decode("ascii", "replace")
            self._evtype = sdl.SDL_RegisterEvents(1)
            self.texture = None
            self._tex_key = None
            log.info("Screen window: SDL %s, renderer %s", ".".join(map(str, self._sdl_version())),
                     self.renderer_name)
        except Exception as exc:
            self._error = str(exc)
            self._ready.set()
            self._cleanup()
            return
        self._ready.set()
        self._clear()
        try:
            self._loop()
        except Exception:
            log.exception("The screen window failed")
        finally:
            self.closed = True
            self._cleanup()
            self._fire_close()

    def _sdl_version(self):
        v = self.sdl.SDL_version()
        self.sdl.SDL_GetVersion(ctypes.byref(v))
        return v.major, v.minor, v.patch

    def _cleanup(self) -> None:
        sdl = self.sdl
        try:
            if getattr(self, "_ov_tex", None):
                sdl.SDL_DestroyTexture(self._ov_tex)
            if getattr(self, "texture", None):
                sdl.SDL_DestroyTexture(self.texture)
            if getattr(self, "renderer", None):
                sdl.SDL_DestroyRenderer(self.renderer)
            if getattr(self, "window", None):
                sdl.SDL_DestroyWindow(self.window)
            sdl.SDL_QuitSubSystem(sdl.SDL_INIT_VIDEO)
        except Exception:
            pass
        self.texture = self.renderer = self.window = self._ov_tex = None

    def _loop(self) -> None:
        sdl = self.sdl
        ev = sdl.SDL_Event()
        fps_t = time.monotonic()
        last_title = ""
        while not self.closed:
            got = sdl.SDL_WaitEventTimeout(ctypes.byref(ev), 100)
            redraw = False
            while got:
                if ev.type == self._evtype:
                    self._wake_pending = False
                elif ev.type == sdl.SDL_QUIT:
                    self.closed = True
                elif ev.type == sdl.SDL_WINDOWEVENT:
                    we = ev.window.event
                    if we == sdl.SDL_WINDOWEVENT_CLOSE:
                        self.closed = True
                    elif we in (sdl.SDL_WINDOWEVENT_SIZE_CHANGED, sdl.SDL_WINDOWEVENT_EXPOSED,
                                sdl.SDL_WINDOWEVENT_RESTORED):
                        redraw = True
                elif ev.type == sdl.SDL_KEYDOWN:
                    sym = ev.key.keysym.sym
                    if sym == sdl.SDLK_F11:
                        self._set_fullscreen(not self.fullscreen)
                    elif sym == sdl.SDLK_ESCAPE and self.fullscreen:
                        self._set_fullscreen(False)
                    elif sym == sdl.SDLK_F9:
                        self.toggle_overlay_now()
                        redraw = True
                elif ev.type == sdl.SDL_MOUSEBUTTONDOWN and ev.button.clicks >= 2:
                    self._set_fullscreen(not self.fullscreen)
                got = sdl.SDL_PollEvent(ctypes.byref(ev))
            if self.closed:
                break
            if self._want_show:
                self._want_show = False
                sdl.SDL_RestoreWindow(self.window)
                sdl.SDL_RaiseWindow(self.window)
            if self._want_fullscreen is not None:
                self._set_fullscreen(self._want_fullscreen)
                self._want_fullscreen = None
            if self._want_extend is not None:
                _, rect = self._want_extend
                self._want_extend = None
                try:
                    self._apply_extend(rect)
                except Exception:
                    log.exception("could not apply the second-screen layout")
                redraw = True
            if self._want_hidden is not None:
                hide, self._want_hidden = self._want_hidden, None
                self._apply_hidden(hide)
                redraw = True
            if self._want_overlay_toggle:
                self._want_overlay_toggle = False
                self.toggle_overlay_now()
                redraw = True
            redraw = self._consume() or redraw
            now = time.monotonic()
            if now - fps_t >= 1.0:
                self._update_stats(now - fps_t)
                fps_t = now
                if self.overlay_visible() and self._overlay_text() != (self._ov_key or ("",))[0]:
                    redraw = True                 # keep the measurements updating while the screen is still
            if redraw:
                self._present()
            title = self._title()
            if title != last_title:
                sdl.SDL_SetWindowTitle(self.window, title.encode("utf-8"))
                self.title = last_title = title

    def _set_fullscreen(self, value: bool) -> None:
        flag = self.sdl.SDL_WINDOW_FULLSCREEN_DESKTOP if value else 0
        if self.sdl.SDL_SetWindowFullscreen(self.window, flag) == 0:
            self.fullscreen = value

    def _display_bounds(self):
        sdl, out = self.sdl, []
        for i in range(max(0, int(sdl.SDL_GetNumVideoDisplays()))):
            r = sdl.SDL_Rect()
            if sdl.SDL_GetDisplayBounds(i, ctypes.byref(r)) == 0:
                out.append([r.x, r.y, r.w, r.h])
            else:
                out.append([0, 0, 0, 0])
        return out

    def _window_geometry(self):
        sdl = self.sdl
        x, y, w, h = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        sdl.SDL_GetWindowPosition(self.window, ctypes.byref(x), ctypes.byref(y))
        sdl.SDL_GetWindowSize(self.window, ctypes.byref(w), ctypes.byref(h))
        return x.value, y.value, w.value, h.value

    def _apply_extend(self, rect) -> None:
        sdl = self.sdl
        if rect:
            from ..extend import pick_display
            bounds = self._display_bounds()
            idx = pick_display(bounds, rect)
            if idx is None:
                xlog.warning("Second-screen monitor not found: %s (displays: %s)", rect, bounds)
                return
            if self._pre_extend is None:
                self._pre_extend = (self._window_geometry(), self.fullscreen)
            b = bounds[idx]
            if self.fullscreen:
                self._set_fullscreen(False)
            sdl.SDL_RestoreWindow(self.window)
            sdl.SDL_SetWindowPosition(self.window, b[0] + 40, b[1] + 40)
            sdl.SDL_SetWindowSize(self.window, max(320, b[2] - 80), max(200, b[3] - 80))
            self._set_fullscreen(True)
            sdl.SDL_RaiseWindow(self.window)
            self.extend_rect = list(rect)
            xlog.info("Second screen: window full screen on display %d (%d×%d)", idx + 1, b[2], b[3])
        else:
            if self.extend_rect is None:
                return
            self.extend_rect = None
            if self.hidden:
                self._apply_hidden(False)
            pre, self._pre_extend = self._pre_extend, None
            self._set_fullscreen(False)
            if pre is not None:
                (x, y, w, h), was_full = pre
                sdl.SDL_SetWindowPosition(self.window, x, y)
                sdl.SDL_SetWindowSize(self.window, w, h)
                if was_full:
                    self._set_fullscreen(True)
            xlog.info("Second screen closed: the window returned to its previous layout")

    def _apply_hidden(self, hide: bool) -> None:
        sdl = self.sdl
        if hide == self.hidden:
            return
        self.hidden = hide
        if hide:
            sdl.SDL_MinimizeWindow(self.window)
        else:
            sdl.SDL_RestoreWindow(self.window)
            if self.extend_rect is not None and not self.fullscreen:
                self._set_fullscreen(True)
            sdl.SDL_RaiseWindow(self.window)

    # ---- stats box (top right) ----------------------------------------------------------
    def _pref_key(self) -> str:
        if self.extend_rect is not None:
            return "overlay_extend"
        return "overlay_fullscreen" if self.fullscreen else "overlay_window"

    def overlay_visible(self) -> bool:
        # default: on in full screen, off in a window; off on the second screen (so it does not sit over the Mac desktop)
        default = self.fullscreen and self.extend_rect is None
        if self.prefs is None:
            return getattr(self, "_ov_local", {}).get(self._pref_key(), default)
        v = self.prefs.get(self._pref_key())
        return default if v is None else bool(v)

    def toggle_overlay(self) -> None:
        """Thread-safe: same as F9."""
        self._want_overlay_toggle = True
        self._wake()

    def toggle_overlay_now(self) -> None:
        val = not self.overlay_visible()
        if self.prefs is None:
            if not hasattr(self, "_ov_local"):
                self._ov_local = {}
            self._ov_local[self._pref_key()] = val
        else:
            try:
                self.prefs.set(self._pref_key(), val)
            except Exception:
                pass

    def _overlay_text(self) -> str:
        from .overlay import overlay_lines
        with self._lock:
            st = dict(self._stats)
        try:
            info = dict(self.info_fn() or {})
        except Exception:
            info = {}
        if not self.frames_drawn:
            return ""
        return "\n".join(overlay_lines(st, info))

    def _draw_overlay(self, ow: int, oh: int) -> None:
        sdl = self.sdl
        text = self._overlay_text()
        if not text:
            return
        from .overlay import font_px_for, render_overlay
        fpx = font_px_for(oh)
        key = (text, fpx)
        if key != self._ov_key or not self._ov_tex:
            img = render_overlay(text.split("\n"), fpx)
            if self._ov_tex:
                sdl.SDL_DestroyTexture(self._ov_tex)
            self._ov_tex = sdl.SDL_CreateTexture(self.renderer, sdl.SDL_PIXELFORMAT_RGBA32,
                                                 sdl.SDL_TEXTUREACCESS_STATIC, img.width, img.height)
            if not self._ov_tex:
                return
            sdl.SDL_SetTextureBlendMode(self._ov_tex, sdl.SDL_BLENDMODE_BLEND)
            sdl.SDL_UpdateTexture(self._ov_tex, None, img.tobytes(), img.width * 4)
            self._ov_key, self._ov_size = key, img.size
        w, h = self._ov_size
        margin = max(8, fpx // 2)
        dst = sdl.SDL_Rect(ow - w - margin, margin, w, h)
        sdl.SDL_RenderCopy(self.renderer, self._ov_tex, None, ctypes.byref(dst))
        self.overlay_draws += 1

    def _title(self) -> str:
        with self._lock:
            st, status = self._stats, self._status
        if self.frames_drawn and st["fps"] > 0:
            parts = [f"{st['fps']:.0f} fps", f"{st['kbps'] / 1024:.1f} MB/s"]
            if st["latency_ms"] > 0:
                parts.append(T("latency ≈ {ms} ms").format(ms=f"{st['latency_ms']:.0f}"))
            return f"{self._base_title} — {' · '.join(parts)}   " + T("(F11: full screen, F9: measurements)")
        if status:
            return f"{self._base_title} — {status}"
        return self._base_title

    # ---- textures ----------------------------------------------------------------------
    def _ensure_texture(self, kind: str, fmt: int, w: int, h: int) -> bool:
        sdl = self.sdl
        key = (kind, fmt, w, h)
        if self._tex_key == key and self.texture:
            return True
        if self.texture:
            sdl.SDL_DestroyTexture(self.texture)
        self.texture = sdl.SDL_CreateTexture(self.renderer, fmt, sdl.SDL_TEXTUREACCESS_STREAMING, w, h)
        if not self.texture:
            log.warning("Could not create a texture (%s %dx%d): %s", kind, w, h,
                        sdl.SDL_GetError().decode("utf-8", "replace"))
            self._tex_key = None
            return False
        self._tex_key = key
        with self._lock:
            self._tex_size = (kind, (w, h))
        return True

    @staticmethod
    def _plane_ptr(plane):
        return ctypes.cast(ctypes.c_void_p(plane.buffer_ptr), ctypes.POINTER(ctypes.c_ubyte))

    def _upload_video(self, frame) -> bool:
        sdl = self.sdl
        fmt = frame.format.name
        w, h = frame.width, frame.height
        full_range = getattr(frame, "color_range", 0) == 2
        if fmt in ("yuv420p", "nv12") and not (w % 2 or h % 2) and not full_range:
            if fmt == "yuv420p":
                if not self._ensure_texture("yuv", sdl.SDL_PIXELFORMAT_IYUV, w, h):
                    return False
                p = frame.planes
                rc = sdl.SDL_UpdateYUVTexture(self.texture, None, self._plane_ptr(p[0]), p[0].line_size,
                                              self._plane_ptr(p[1]), p[1].line_size,
                                              self._plane_ptr(p[2]), p[2].line_size)
            else:
                if not self._ensure_texture("yuv", sdl.SDL_PIXELFORMAT_NV12, w, h):
                    return False
                p = frame.planes
                rc = sdl.SDL_UpdateNVTexture(self.texture, None, self._plane_ptr(p[0]), p[0].line_size,
                                             self._plane_ptr(p[1]), p[1].line_size)
            if rc == 0:
                return True
            log.warning("Could not update the YUV texture: %s", sdl.SDL_GetError().decode("utf-8", "replace"))
        # Fallback: convert to RGBA on the CPU
        rgba = frame.reformat(format="rgba")
        if not self._ensure_texture("rgba", sdl.SDL_PIXELFORMAT_RGBA32, w, h):
            return False
        p = rgba.planes[0]
        return sdl.SDL_UpdateTexture(self.texture, None, ctypes.c_void_p(p.buffer_ptr), p.line_size) == 0

    def _upload_image(self, img, rect=None) -> bool:
        sdl = self.sdl
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        fmt, bpp = (sdl.SDL_PIXELFORMAT_RGBA32, 4) if img.mode == "RGBA" else (sdl.SDL_PIXELFORMAT_RGB24, 3)
        data = img.tobytes()
        if rect is None:
            if not self._ensure_texture("rgb", fmt, img.width, img.height):
                return False
            return sdl.SDL_UpdateTexture(self.texture, None, data, img.width * bpp) == 0
        x, y = rect
        if self._tex_key is None or self._tex_key[1] != fmt:
            return False
        r = sdl.SDL_Rect(int(x), int(y), img.width, img.height)
        return sdl.SDL_UpdateTexture(self.texture, ctypes.byref(r), data, img.width * bpp) == 0

    def _consume(self) -> bool:
        with self._lock:
            video, self._pending_video = self._pending_video, None
            image, self._pending_image = self._pending_image, None
            batches, self._pending_patches = self._pending_patches, []
        drew = False
        t0 = time.monotonic()
        if video is not None:
            frame, meta = video
            if self._upload_video(frame):
                drew = True
                self._decoder = str(meta.get("decoder") or self._decoder)
                self._count(meta, t0)
        if image is not None:
            img, meta = image
            if self._upload_image(img):
                drew = True
                self._count(meta, t0)
        for size, patches, meta in batches:
            if self._tex_key is None or self._tex_key[0] != "rgb" or (self._tex_key[2], self._tex_key[3]) != size:
                with self._lock:
                    self._overflow = True       # size mismatch: ask for a full frame
                break
            t1 = time.monotonic()
            for x, y, img in patches:
                self._upload_image(img, (x, y))
            self.patch_batches += 1
            drew = True
            self._count(meta, t1)
        return drew

    def _clear(self) -> None:
        sdl = self.sdl
        sdl.SDL_SetRenderDrawColor(self.renderer, 0, 0, 0, 255)
        sdl.SDL_RenderClear(self.renderer)
        sdl.SDL_RenderPresent(self.renderer)

    def _present(self) -> None:
        sdl = self.sdl
        t0 = time.monotonic()
        sdl.SDL_SetRenderDrawColor(self.renderer, 0, 0, 0, 255)
        sdl.SDL_RenderClear(self.renderer)
        if self.texture and self._tex_key:
            _, _, tw, th = self._tex_key
            ow, oh = ctypes.c_int(), ctypes.c_int()
            sdl.SDL_GetRendererOutputSize(self.renderer, ctypes.byref(ow), ctypes.byref(oh))
            s = min(ow.value / tw, oh.value / th) if tw and th else 1.0
            dw, dh = max(1, int(tw * s)), max(1, int(th * s))
            dst = sdl.SDL_Rect((ow.value - dw) // 2, (oh.value - dh) // 2, dw, dh)
            sdl.SDL_RenderCopy(self.renderer, self.texture, None, ctypes.byref(dst))
            if self.overlay_visible():
                self._draw_overlay(ow.value, oh.value)
        if self.capture_next:                      # diagnostics/tests: read back the drawn image
            self.capture_next = False
            ow, oh = ctypes.c_int(), ctypes.c_int()
            sdl.SDL_GetRendererOutputSize(self.renderer, ctypes.byref(ow), ctypes.byref(oh))
            buf = (ctypes.c_ubyte * (ow.value * oh.value * 4))()
            if sdl.SDL_RenderReadPixels(self.renderer, None, sdl.SDL_PIXELFORMAT_RGBA32, buf, ow.value * 4) == 0:
                self.captured = (ow.value, oh.value, bytes(buf))
        sdl.SDL_RenderPresent(self.renderer)
        self.frames_drawn += 1
        self._acc["disp"] += (time.monotonic() - t0) * 1000.0

    # ---- measurements ---------------------------------------------------------------------
    def _count(self, meta: dict, t0: float) -> None:
        a = self._acc
        a["frames"] += 1
        a["bytes"] += int(meta.get("bytes", 0))
        a["enc"] += float(meta.get("enc_ms", 0.0))
        a["dec"] += float(meta.get("decode_ms", 0.0))
        a["disp"] += (time.monotonic() - t0) * 1000.0      # upload; drawing is added in _present

    def _update_stats(self, span: float) -> None:
        a = self._acc
        n = max(a["frames"], 1)
        rtt = None
        if self.rtt_fn is not None:
            try:
                rtt = self.rtt_fn()
            except Exception:
                rtt = None
        enc, dec, disp = a["enc"] / n, a["dec"] / n, a["disp"] / n
        latency = enc + dec + disp + (rtt / 2.0 if rtt else 0.0) if a["frames"] else 0.0
        with self._lock:
            self._stats = {"fps": a["frames"] / span, "kbps": a["bytes"] / 1024.0 / span, "enc_ms": enc,
                           "decode_ms": dec, "display_ms": disp, "latency_ms": latency,
                           "rtt_ms": rtt or 0.0, "renderer": self.renderer_name, "decoder": self._decoder}
        self._acc = {"frames": 0, "bytes": 0, "enc": 0.0, "dec": 0.0, "disp": 0.0}
