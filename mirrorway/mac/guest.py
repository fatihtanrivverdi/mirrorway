"""Windows's own keyboard and mouse on the Mac (3.6), the Mac's side: what Windows sends is posted as the Mac's own
input (mac.inject), and a pointer pushed against the Mac's edge that faces Windows goes back. Platform independent
logic; the poster does the posting.

poster: cursor_pos() -> (x, y), move(x, y, dx, dy, button or None), button(button, down, x, y, clicks),
        scroll(lines_v, lines_h), key(keycode, down, flags), flags_changed(keycode, flags)
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional, Set, Tuple

from .. import guest as GU
from .. import keymap as K
from ..geometry import EdgeGuard, clamp_to_rects
from ..i18n import T

log = logging.getLogger("mirrorway.guest")

CLICK_S = 0.5                 # a second click this soon, this close, is a double click (and so on)
CLICK_PT = 5.0
WHEEL_PER_LINE = 40.0         # one notch (120) = 3 lines


class MacGuest:
    def __init__(self, router, poster, send_json: Callable[[int, dict], None], clock=time.monotonic,
                 trusted: Callable[[], bool] = lambda: True):
        self.router = router
        self.poster = poster
        self.send_json = send_json
        self.clock = clock
        self.trusted = trusted
        self.lock = threading.RLock()
        self.active = False
        self.buttons: Set[int] = set()
        self.keys: Set[int] = set()
        self.mods = GU.ModifierState()
        self.translator = GU.WinToMacKeys()
        self.guard = EdgeGuard(clock)
        self._click: Optional[Tuple[int, float, float, float, int]] = None   # button, time, x, y, count
        self._acc_v = self._acc_h = 0.0
        self.entered = 0
        self.on_change: Optional[Callable[[bool], None]] = None

    # -- from Windows --
    def enter(self, ratio: Optional[float]) -> dict:
        """Windows's devices arrive. The answer goes back as MSG_G_STATE."""
        if not self.trusted():
            log.warning("Windows's keyboard and mouse cannot be used on the Mac: no Accessibility permission")
            return {"active": False, "error": T("Mirrorway on the Mac needs the Accessibility permission")}
        settings = self.router.settings
        layout = settings.get("keyboard_layout")
        pos = self.router.guest_arrives(ratio)        # (not under self.lock: the router calls end_local under its own)
        with self.lock:
            self.translator = GU.WinToMacKeys(swap_cmd_ctrl=bool(settings.get("swap_cmd_ctrl", True)),
                                              iso=layout != "ansi")
            self.active = True
            self.entered += 1
            self.guard.reset(armed=False)
            self._acc_v = self._acc_h = 0.0
        log.info("Windows's keyboard and mouse are on the Mac (%s)",
                 "shortcut" if ratio is None else f"at {pos[0]:.0f}, {pos[1]:.0f}")
        self._changed()
        return {"active": True}

    def move(self, dx: float, dy: float) -> None:
        with self.lock:
            if not self.active:
                return
            scale = self.router.auto_scale or 1.0                # Windows pixels per Mac point
            mx, my = dx / scale, dy / scale
            displays = self.router.platform.displays()
            try:
                x, y = self.poster.cursor_pos()                  # the Mac's own trackpad may have moved it too
            except Exception:
                return
            ratio = self.guard.check(displays, x, y, mx, my, self.router.side)
            if ratio is not None:
                q = self.router.windows_ratio(ratio, displays)
                if q is not None:
                    self._exit(q)
                    return
            nx, ny = clamp_to_rects(displays, x + mx, y + my)
            self.poster.move(nx, ny, mx, my, min(self.buttons) if self.buttons else None)

    def button(self, button: int, down: bool) -> None:
        with self.lock:
            if not self.active and down:
                return
            if not down and button not in self.buttons:
                return
            x, y = self.poster.cursor_pos()
            clicks = 1
            if down:
                now = self.clock()
                c = self._click
                if (c is not None and c[0] == button and now - c[1] <= CLICK_S
                        and abs(x - c[2]) <= CLICK_PT and abs(y - c[3]) <= CLICK_PT):
                    clicks = min(c[4] + 1, 3)
                self._click = (button, now, x, y, clicks)
                self.buttons.add(button)
            else:
                self.buttons.discard(button)
                if self._click is not None and self._click[0] == button:
                    clicks = self._click[4]
            self.poster.button(button, down, x, y, clicks)

    def scroll(self, v: int, h: int) -> None:
        with self.lock:
            if not self.active:
                return
            self._acc_v += v / WHEEL_PER_LINE
            self._acc_h -= h / WHEEL_PER_LINE                # Windows: positive = right; the Mac: positive = left
            lv, lh = int(self._acc_v), int(self._acc_h)
            if lv or lh:
                self._acc_v -= lv
                self._acc_h -= lh
                self.poster.scroll(lv, lh)

    def key(self, down: bool, flags: int, scan: int, vk: int) -> None:
        with self.lock:
            if not self.active and down:
                return
            kc = self.translator.translate(flags, scan, vk)
            if kc is None:
                return
            if kc in K.MODIFIER_KEYCODES:
                if not down and kc not in self.mods.down and kc != K.MAC_CAPS:
                    return
                self.mods.update(kc, down)
                self.poster.flags_changed(kc, self.mods.flags())
                return
            if down:
                self.keys.add(kc)
            elif kc in self.keys:
                self.keys.discard(kc)
            else:
                return
            self.poster.key(kc, down, self.mods.flags())

    def leave(self) -> None:
        """Windows took its devices back."""
        with self.lock:
            if not self.active:
                return
            self._release()
            self.active = False
        log.info("Windows's keyboard and mouse went back to Windows")
        self._changed()

    # -- on the Mac --
    def end_local(self) -> None:
        """The Mac's own devices went to Windows, or the connection dropped: Windows's are not on the Mac anymore
        (Windows ends its side when it hears of it, or on its own when the connection drops)."""
        self.leave()

    def _exit(self, ratio: Optional[float]) -> None:
        self._release()
        self.active = False
        try:
            self.send_json(GU.MSG_G_EXIT, {"ratio": None if ratio is None else round(float(ratio), 6)})
        except Exception:
            pass
        log.info("Windows's keyboard and mouse went back across the edge")
        self._changed()

    def _release(self) -> None:
        try:
            x, y = self.poster.cursor_pos()
        except Exception:
            x = y = 0.0
        for b in sorted(self.buttons):
            try:
                self.poster.button(b, False, x, y, 1)
            except Exception:
                pass
        for kc in sorted(self.keys):
            try:
                self.poster.key(kc, False, self.mods.flags())
            except Exception:
                pass
        for kc in sorted(self.mods.down):
            self.mods.update(kc, False)
            try:
                self.poster.flags_changed(kc, self.mods.flags())
            except Exception:
                pass
        self.buttons.clear()
        self.keys.clear()
        self.mods.clear()

    def _changed(self) -> None:
        cb = self.on_change
        if cb is not None:
            try:
                cb(self.active)
            except Exception:
                log.exception("guest listener")
