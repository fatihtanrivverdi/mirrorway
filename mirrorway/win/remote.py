"""Logic that applies the input coming from the Mac on Windows (platform independent, testable).

backend interface:
  get_pos() -> (x, y)          monitors() -> List[Rect]      move_to(x, y)
  button(btn, down)            wheel(v, h)                   key(down, flags, scan, vk)
  type_text(text)
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional, Set, Tuple

from .. import extend as X
from .. import gestures as G
from ..geometry import EdgeGuard, Rect, clamp_to_rects, entry_point
from ..keymap import win_key_is_windows_logo
from ..protocol import KEYFLAG_EXTENDED, KEYFLAG_VK, MSG_LEAVE

log = logging.getLogger("mirrorway.remote")

ENTER_COOLDOWN = 0.25
MASK_VK = 0xE8   # an unassigned key: keeps the Start menu from opening when Win is released


class RemoteControl:
    def __init__(self, backend, send_json: Callable[[int, dict], None], clock=time.monotonic):
        self.backend = backend
        self.send_json = send_json
        self.clock = clock
        self.lock = threading.RLock()
        self.active = False
        self.exit_side = "left"
        self._ax = 0.0
        self._ay = 0.0
        self._last_enter = -1e9
        self._rects: Optional[List[Rect]] = None
        self._rects_t = 0.0
        self.keys_down: Set[Tuple[int, int, int]] = set()
        self.buttons_down: Set[int] = set()
        self.listeners: List[Callable[[bool], None]] = []
        self.enter_listeners: List[Callable[[float, float], None]] = []     # where the pointer arrived (the ring)
        self.edge_guard = EdgeGuard(clock)
        self.extend_rect: Optional[Rect] = None      # second screen: the monitor that shows the Mac's virtual display
        self._guards: dict = {}

    def _monitors(self, refresh: bool = False) -> List[Rect]:
        now = self.clock()
        if refresh or self._rects is None or now - self._rects_t > 2.0:
            self._rects = self.backend.monitors()
            self._rects_t = now
        return self._rects

    def _usable(self) -> List[Rect]:
        """Monitors the pointer may roam: the second-screen monitor belongs to the Mac (unless it is the only one)."""
        rects = self._monitors()
        m = self.extend_rect
        if m is None:
            return rects
        rest = [r for r in rects if r != m]
        return rest or rects

    def _pointer_on(self, rects: List[Rect]) -> Optional[Tuple[float, float]]:
        """Where Windows' pointer is now, if it is on one of these monitors."""
        try:
            x, y = self.backend.get_pos()
        except Exception:
            return None
        return (x, y) if any(r.contains(x, y) for r in rects) else None

    def _exit_sides(self, rects: List[Rect]) -> List[str]:
        sides = [self.exit_side]
        m = self.extend_rect
        if m is not None and m not in rects:
            for s in X.toward(rects, m):
                if s not in sides:
                    sides.append(s)
        return sides

    def _guard(self, side: str) -> EdgeGuard:
        if side == self.exit_side:
            return self.edge_guard
        g = self._guards.get(side)
        if g is None:
            g = self._guards[side] = EdgeGuard(self.clock)
        return g

    def _notify(self) -> None:
        for fn in list(self.listeners):
            try:
                fn(self.active)
            except Exception:
                pass

    # ---- from the server -------------------------------------------------
    def on_enter(self, info: dict) -> None:
        with self.lock:
            self._monitors(refresh=True)
            ext = info.get("extend")
            try:
                self.extend_rect = Rect(*(float(v) for v in ext)) if ext and len(ext) == 4 else None
            except (TypeError, ValueError):
                self.extend_rect = None
            rects = self._usable()
            from_side = info.get("from", "center")
            ratio = float(info.get("ratio", 0.5))
            exit_side = info.get("exit") or (from_side if from_side != "center" else "left")
            self.exit_side = exit_side
            pos = info.get("pos")
            here = self._pointer_on(rects) if from_side == "center" else None
            if isinstance(pos, list) and len(pos) == 2:
                x, y = clamp_to_rects(rects, float(pos[0]), float(pos[1]))
            elif here is not None:
                x, y = here                  # a shortcut or the panel: the pointer stays where it was on Windows
            else:
                x, y = entry_point(rects, from_side, ratio)
            self.backend.move_to(x, y)
            self.active = True
            self._ax = self._ay = 0.0
            self._last_enter = self.clock()
            self.edge_guard.reset(armed=False)
            for g in self._guards.values():
                g.reset(armed=False)
        self._notify()
        for fn in list(self.enter_listeners):
            try:
                fn(x, y)
            except Exception:
                pass

    def on_exit(self) -> None:
        with self.lock:
            self.release_all()
            self.active = False
        self._notify()

    def on_move(self, dx: float, dy: float) -> None:
        with self.lock:
            if not self.active:
                return
            self._ax += dx
            self._ay += dy
            ix, iy = int(self._ax), int(self._ay)
            self._ax -= ix
            self._ay -= iy
            if not ix and not iy:
                return
            x, y = self.backend.get_pos()
            if self.extend_rect is None:
                if self.clock() - self._last_enter > ENTER_COOLDOWN:
                    ratio = self.edge_guard.check(self._monitors(), x, y, ix, iy, self.exit_side, corner_guard=0)
                    if ratio is not None:
                        self._leave(ratio)
                        return
                self.backend.move_to(x + ix, y + iy)
                return
            # Second screen: a pointer trying to enter that monitor moves to the Mac's virtual display
            rects = self._usable()
            m = self.extend_rect
            if self.clock() - self._last_enter > ENTER_COOLDOWN:
                for side in self._exit_sides(rects):
                    ratio = self._guard(side).check(rects, x, y, ix, iy, side, corner_guard=0)
                    if ratio is None:
                        continue
                    if m not in rects:
                        hit = X.crosses_into(m, x, y, side)
                        if hit is not None:
                            self._leave(ratio, vd=X.m_fraction(m, *hit))
                            return
                    if side == self.exit_side:
                        self._leave(ratio)
                        return
            nx, ny = x + ix, y + iy
            if m not in rects:
                nx, ny = clamp_to_rects(rects, nx, ny)     # do not move onto the monitor that belongs to the Mac
            self.backend.move_to(nx, ny)

    def on_button(self, btn: int, down: bool) -> None:
        with self.lock:
            if down:
                if not self.active:
                    return
                self.buttons_down.add(btn)
                self.backend.button(btn, True)
            else:
                if btn in self.buttons_down or self.active:
                    self.buttons_down.discard(btn)
                    self.backend.button(btn, False)

    def on_scroll(self, v: int, h: int) -> None:
        with self.lock:
            if self.active and (v or h):
                self.backend.wheel(v, h)

    def on_key(self, down: bool, flags: int, scan: int, vk: int) -> None:
        with self.lock:
            k = (flags, scan, vk)
            if down:
                if not self.active:
                    return
                self.keys_down.add(k)
                self.backend.key(True, flags, scan, vk)
            else:
                if k in self.keys_down or self.active:
                    self.keys_down.discard(k)
                    self.backend.key(False, flags, scan, vk)

    def on_gesture(self, info: dict) -> None:
        """Mac trackpad gesture: desktop switch, Task View, show desktop or Ctrl+wheel."""
        with self.lock:
            if not self.active:
                return
            action = str(info.get("action") or "")
            if action == "zoom":
                steps = max(-20, min(20, int(info.get("steps") or 0)))
                if steps:
                    self.backend.key(True, KEYFLAG_VK, 0, G.VK_CONTROL)
                    try:
                        self.backend.wheel(120 * steps, 0)
                    finally:
                        self.backend.key(False, KEYFLAG_VK, 0, G.VK_CONTROL)
                return
            for down, vk, ext in G.key_sequence(action):
                self.backend.key(down, KEYFLAG_VK | (KEYFLAG_EXTENDED if ext else 0), 0, vk)

    def on_text(self, text: str) -> None:
        with self.lock:
            if self.active and text:
                self.backend.type_text(text)

    def release_all(self) -> None:
        with self.lock:
            keys = list(self.keys_down)
            if any(win_key_is_windows_logo(f, s) for f, s, _ in keys):
                self.backend.key(True, KEYFLAG_VK, 0, MASK_VK)
                self.backend.key(False, KEYFLAG_VK, 0, MASK_VK)
            for flags, scan, vk in keys:
                try:
                    self.backend.key(False, flags, scan, vk)
                except Exception:
                    pass
            for btn in list(self.buttons_down):
                try:
                    self.backend.button(btn, False)
                except Exception:
                    pass
            self.keys_down.clear()
            self.buttons_down.clear()

    def _leave(self, ratio: float, vd=None) -> None:
        self.release_all()
        self.active = False
        msg = {"ratio": ratio}
        if vd is not None:
            msg["vd"] = [float(vd[0]), float(vd[1])]
        try:
            self.send_json(MSG_LEAVE, msg)
        except Exception:
            pass
        self._notify()

    def on_disconnect(self) -> None:
        self.on_exit()
