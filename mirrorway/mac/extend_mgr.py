"""Second-screen management (Mac): when the virtual display opens and closes, and data for transitions.

The virtual display (VD) exists only while it is needed:
  - while the setting is on (cfg "extend_monitor": "auto" or a Windows monitor [x, y, w, h]),
  - while Windows is connected and supports the second screen,
  - while a video connection (the screen window on Windows) is open. If the window closes, it is removed after
    GRACE_S; across short drops (Windows updating and reconnecting) the same display keeps being used.
When the virtual display goes away, macOS moves its windows to the main display.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional

from ..extend import (FEATURE, as_rects, choose_monitor, monitor_label, neighbor_sides, place_beside,
                      valid_size, win_entry)
from ..geometry import Rect
from ..i18n import T

log = logging.getLogger("mirrorway.extend")


def _default_vd_factory(w: int, h: int, origin):
    from .vdisplay import VirtualDisplay
    return VirtualDisplay(w, h, origin=origin)


class ExtendManager:
    GRACE_S = 20.0
    RECT_TTL = 1.0

    def __init__(self, server, vd_factory: Optional[Callable] = None, clock=time.monotonic):
        self.server = server
        self.cfg = server.cfg
        self.vd_factory = vd_factory or _default_vd_factory
        self.clock = clock
        self._lock = threading.RLock()
        self.vd = None
        self.monitors: List[list] = []
        self.m: Optional[int] = None
        self.mrect: Optional[Rect] = None
        self.last_error = ""
        self._gen = 0
        self._rect_cache = (-1e9, None)
        self._timer: Optional[threading.Timer] = None

    # ---- setting and state ---------------------------------------------------------------------------
    def desired(self):
        v = self.cfg.get("extend_monitor")
        if v == "auto" or (isinstance(v, (list, tuple)) and len(v) == 4):
            return v
        return None

    def supported(self) -> bool:
        return self.server.supports(FEATURE)

    def win_monitors(self) -> List[list]:
        c = self.server.control
        mons = (c.hello.get("monitors") if c is not None else None) or []
        out = []
        for m in mons:
            try:
                out.append([int(v) for v in list(m)[:4]])
            except Exception:
                pass
        return out

    def remember_monitors(self) -> None:
        mons = self.win_monitors()
        if mons and mons != self.cfg.get("win_monitors"):
            self.cfg.set("win_monitors", mons)

    def choices(self) -> List[tuple]:
        """[(label, value)] for the menu: value None = off."""
        mons = self.win_monitors() or list(self.cfg.get("win_monitors") or [])
        out = [(T("Off"), None)]
        for i, m in enumerate(mons):
            out.append((monitor_label(mons, i), list(m)))
        return out

    def active(self) -> bool:
        vd = self.vd
        return vd is not None and vd.alive()

    def check_alive(self) -> None:
        """If the helper process exited unexpectedly (the virtual display is gone): refresh the stream."""
        vd = self.vd
        if vd is None or vd.alive():
            return
        with self._lock:
            if self.vd is not vd:
                return
            self.vd, self.m, self.mrect = None, None, None
            self._rect_cache = (-1e9, None)
            self.last_error = T("The virtual display closed unexpectedly; reopening it")
        log.warning("Second-screen helper process exited; refreshing the video connection")
        inv = getattr(self.server.platform, "invalidate", None)
        if inv is not None:
            inv()
        threading.Thread(target=self.server.restart_video, daemon=True, name="mirrorway-extend-restart").start()

    def status(self) -> dict:
        self.check_alive()
        r = self.vd_rect() if self.active() else None
        return {"desired": self.desired() is not None, "active": self.active(), "error": self.last_error,
                "monitor": self.m, "vd": [r.x, r.y, r.w, r.h] if r else None,
                "size": [int(self.mrect.w), int(self.mrect.h)] if self.mrect is not None and self.active() else None}

    # ---- life cycle ---------------------------------------------------------------------------
    def prepare(self) -> Optional[dict]:
        """When a video connection starts: open the virtual display if requested (or reuse the open one)."""
        with self._lock:
            self._gen += 1
            self._cancel_timer()
            want = self.desired()
            if want is None:
                self._release_locked("second screen off")
                self.last_error = ""
                return None
            if not self.supported():
                self._release_locked("Windows side does not support the second screen")
                self.last_error = T("Mirrorway on Windows does not support the second screen (needs an update)")
                return None
            mons = self.win_monitors()
            m = choose_monitor(mons, want)
            if m is None:
                self._release_locked("no monitor")
                self.last_error = T("The selected Windows monitor was not found")
                return None
            r = as_rects(mons)[m]
            if not valid_size(r.w, r.h):
                self.last_error = T("Unsupported monitor size: {w}×{h}").format(w=int(r.w), h=int(r.h))
                return None
            vd = self.vd
            if vd is not None and vd.alive() and (vd.width, vd.height) == (int(r.w), int(r.h)):
                self.monitors, self.m, self.mrect = mons, m, r
                return self._info_locked()
            self._release_locked(None)
            try:
                main = self.server.platform.main_display()
                origin = place_beside(main, r.w, r.h, self.server.router.side)
            except Exception:
                origin = None
            vd = self.vd_factory(int(r.w), int(r.h), origin)
            try:
                info = vd.start()
            except Exception as exc:
                self.last_error = T("Could not open the virtual display: {error}").format(error=exc)
                log.warning("Could not open the second screen: %s", exc)
                return None
            self.vd, self.monitors, self.m, self.mrect = vd, mons, m, r
            self.last_error = ""
            self._rect_cache = (-1e9, None)
            inv = getattr(self.server.platform, "invalidate", None)
            if inv is not None:
                inv()
            b = info.get("bounds") or []
            log.info("Second screen opened: %d×%d, on the Mac at %s (display id %s)%s; on Windows %s",
                     int(r.w), int(r.h), "x={} y={}".format(*b[:2]) if len(b) >= 2 else "?", info.get("id"),
                     f" — {info['note']}" if info.get("note") else "", monitor_label(mons, m))
            return self._info_locked()

    def _info_locked(self) -> dict:
        r = self.mrect
        return {"id": self.vd.display_id, "w": int(r.w), "h": int(r.h), "mrect": [r.x, r.y, r.w, r.h],
                "monitor": self.m}

    def session_ended(self) -> None:
        """The video connection closed: remove the virtual display unless a new one arrives."""
        with self._lock:
            if self.vd is None:
                return
            gen = self._gen
            self._cancel_timer()

            def later():
                with self._lock:
                    if self._gen == gen:
                        v = self.server.video
                        if v is None or getattr(v, "closed", True):
                            self._release_locked("screen window closed")
            t = threading.Timer(self.GRACE_S, later)
            t.daemon = True
            self._timer = t
            t.start()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def release(self, reason: Optional[str] = None) -> None:
        with self._lock:
            self._gen += 1
            self._cancel_timer()
            self._release_locked(reason)

    def _release_locked(self, reason: Optional[str]) -> None:
        vd, self.vd = self.vd, None
        self.m, self.mrect = None, None
        self._rect_cache = (-1e9, None)
        if vd is None:
            return
        try:
            vd.stop()
        except Exception:
            log.exception("Could not close the virtual display")
        inv = getattr(self.server.platform, "invalidate", None)
        if inv is not None:
            inv()
        if reason:
            log.info("Second screen closed (%s)", reason)

    def changed(self) -> None:
        """The setting changed from the menu."""
        if self.desired() is None:
            self.release("turned off by the user")
        self.server.restart_video()
        if self.desired() is not None and self.supported():
            self.server.request_viewer()

    # ---- for transitions (Router) ---------------------------------------------------------------------
    def vd_rect(self) -> Optional[Rect]:
        vd = self.vd
        if vd is None:
            return None
        t, r = self._rect_cache
        now = self.clock()
        if r is not None and now - t < self.RECT_TTL:
            return r
        r = None
        fn = getattr(self.server.platform, "display_rect_id", None)
        if fn is not None:
            try:
                r = fn(vd.display_id)
            except Exception:
                r = None
        if r is None or r.w <= 0:
            b = (vd.info or {}).get("bounds")
            r = Rect(*(float(v) for v in b)) if b and len(b) == 4 else None
        self._rect_cache = (now, r)
        return r

    def m_rect(self) -> Optional[list]:
        r = self.mrect
        return [r.x, r.y, r.w, r.h] if r is not None and self.active() else None

    def route(self) -> Optional[dict]:
        """Through which edges of VD can the pointer move from the Mac to Windows."""
        if not self.active() or self.m is None:
            return None
        vr = self.vd_rect()
        if vr is None:
            return None
        return {"vd": vr, "sides": list(neighbor_sides(self.monitors, self.m))}

    def win_point(self, side: str, ratio: float):
        if self.m is None:
            return None
        return win_entry(self.monitors, self.m, side, ratio)
