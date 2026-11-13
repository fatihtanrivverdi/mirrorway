"""Windows's own keyboard and mouse on the Mac (3.6), Windows's side: when the pointer is pushed against the edge
that faces the Mac (seen in raw mouse input, so moving along the edge does not count) or Ctrl+Alt + the arrow
towards the Mac is pressed, the keyboard and mouse are captured (low-level hooks), the pointer is hidden and parked,
and what they do goes to the Mac. Ctrl+Alt + the other arrow, the Mac sending the pointer back across its edge,
the Mac's own devices arriving, Windows locking or the connection dropping bring them home. Platform independent;
the backend (win.guest_hooks) does the Windows part.

backend: monitors() -> [Rect] (primary first), cursor_pos() -> (x, y), set_cursor(x, y), hide_cursor(),
         show_cursor(), capture(on) (installs or removes the hooks)
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Callable, List, Optional, Set, Tuple

from .. import guest as GU
from ..geometry import EdgeGuard, Rect, entry_point
from ..protocol import KEYFLAG_EXTENDED

log = logging.getLogger("mirrorway.guest")

VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28
ARROW_VK = {"left": VK_LEFT, "top": VK_UP, "right": VK_RIGHT, "bottom": VK_DOWN}
AWAY = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
CTRL_VKS = {0x11, 0xA2, 0xA3}
_MAC_KEYS = GU.WinToMacKeys()                  # which keys the Mac has a place for
ALT_VKS = {0x12, 0xA4, 0xA5}


class OutBox:
    """Messages to the Mac from the hook thread, sent on a thread of their own: a hook must never wait for the
    network (Windows would stall its input). Moves waiting behind each other are merged."""

    def __init__(self, send: Callable[[int, bytes], None]):
        self._send = send
        self._q: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name="mirrorway-guest-out").start()

    def __call__(self, mtype: int, payload: bytes = b"") -> None:
        self._q.put((mtype, payload))

    def _run(self) -> None:
        while True:
            items = [self._q.get()]
            while True:
                try:
                    items.append(self._q.get_nowait())
                except queue.Empty:
                    break
            out: List[Tuple[int, bytes]] = []
            for mtype, payload in items:
                if mtype == GU.MSG_G_MOVE and out and out[-1][0] == GU.MSG_G_MOVE:
                    ax, ay = GU.unpack_move(out[-1][1])
                    bx, by = GU.unpack_move(payload)
                    out[-1] = (GU.MSG_G_MOVE, GU.pack_move(ax + bx, ay + by))
                else:
                    out.append((mtype, payload))
            for mtype, payload in out:
                try:
                    self._send(mtype, payload)
                except Exception:
                    pass


class WinGuest:
    REPLY_S = 2.0                   # the Mac must answer this soon, or the keyboard and mouse come home

    def __init__(self, backend, send: Callable[[int, bytes], None], clock=time.monotonic,
                 enabled: Callable[[], bool] = lambda: True):
        self.backend = backend
        self.send = send
        self.clock = clock
        self.enabled = enabled
        self.lock = threading.RLock()
        self.capturing = False
        self.confirmed = False
        self.asked = 0.0
        self.edge: Optional[str] = None          # Windows's edge that faces the Mac (from the Mac)
        self.span: Optional[Tuple[float, float]] = None
        self.guard = EdgeGuard(clock)
        self.home: Optional[Tuple[float, float]] = None
        self.park: Tuple[float, float] = (0.0, 0.0)
        self.remote_keys: Set[Tuple[int, int, int]] = set()
        self.remote_buttons: Set[int] = set()
        self.ctrl: Set[int] = set()
        self.alt: Set[int] = set()
        self.last_error = ""
        self.listeners: List[Callable[[bool, Optional[Tuple[float, float]]], None]] = []

    # -- settings from the Mac --
    def configure(self, edge: Optional[str], span=None) -> None:
        with self.lock:
            self.edge = edge if edge in ARROW_VK else None
            try:
                self.span = (float(span[0]), float(span[1])) if span else None
            except (TypeError, ValueError, IndexError):
                self.span = None

    def available(self) -> bool:
        return self.edge is not None and bool(self.enabled())

    def toward_vk(self) -> Optional[int]:
        return ARROW_VK.get(self.edge) if self.edge else None

    # -- not captured: the edge and the shortcut --
    def on_raw(self, dx: float, dy: float) -> None:
        """Raw movement of a real mouse (from WM_INPUT)."""
        if self.capturing or (not dx and not dy) or not self.available():
            return
        try:
            x, y = self.backend.cursor_pos()
            mons = self.backend.monitors()
        except Exception:
            return
        ratio = self.guard.check(mons, x, y, dx, dy, self.edge, corner_guard=0)
        if ratio is None:
            return
        span = self.span
        if span is not None and not (span[0] - 1e-6 <= ratio <= span[1] + 1e-6):
            return                                          # no Mac display faces this part: a wall
        self.start(ratio)

    def hotkey(self) -> None:
        """Ctrl+Alt + the arrow towards the Mac."""
        if not self.capturing and self.available():
            self.start(None)

    def start(self, ratio: Optional[float]) -> None:
        with self.lock:
            if self.capturing:
                return
            mons = self.backend.monitors()
            self.home = self.backend.cursor_pos()
            p = mons[0] if mons else Rect(0, 0, 1920, 1080)
            self.park = (int(p.x + p.w / 2), int(p.y + p.h / 2))
            self.capturing, self.confirmed, self.asked = True, False, self.clock()
            self.remote_keys.clear()
            self.remote_buttons.clear()
            self.backend.capture(True)
            self.backend.hide_cursor()
            self.backend.set_cursor(*self.park)
            self.send(GU.MSG_G_ENTER, json.dumps({"ratio": ratio}).encode("utf-8"))
        log.info("This PC's keyboard and mouse went to the Mac (%s)",
                 "Ctrl+Alt+arrow" if ratio is None else f"edge, {ratio:.2f}")
        self._notify(True, None)

    # -- from the Mac --
    def on_state(self, info: dict) -> None:
        if info.get("active"):
            self.confirmed = True
            return
        self.last_error = str(info.get("error") or "the Mac said no")
        log.info("The Mac does not take this PC's keyboard and mouse: %s", self.last_error)
        self.end(None, notify=False)

    def on_exit(self, info: dict) -> None:
        r = info.get("ratio")
        self.end(None if r is None else float(r), notify=False)

    def on_mac_enter(self) -> None:
        """The Mac's own keyboard and mouse arrive: Windows's are home again (RemoteControl places the pointer)."""
        self.end(None, notify=False)

    def on_disconnect(self) -> None:
        self.end(None, notify=False)
        self.edge = None

    def tick(self) -> None:
        if self.capturing and not self.confirmed and self.clock() - self.asked > self.REPLY_S:
            log.info("The Mac did not answer; the keyboard and mouse stay on this PC")
            self.end(None, notify=True)

    # -- captured: from the hooks (return True to keep the event from Windows) --
    def on_move(self, x: int, y: int) -> bool:
        if not self.capturing:
            return False
        dx, dy = x - self.park[0], y - self.park[1]
        if dx or dy:
            self.send(GU.MSG_G_MOVE, GU.pack_move(dx, dy))
        return True

    def on_button(self, button: int, down: bool) -> bool:
        if not self.capturing:
            return False
        if down:
            self.remote_buttons.add(button)
        elif button in self.remote_buttons:
            self.remote_buttons.discard(button)
        else:
            return False                                    # pressed before it went to the Mac: Windows gets the release
        self.send(GU.MSG_G_BUTTON, GU.pack_button(button, down))
        return True

    def on_wheel(self, v: int, h: int) -> bool:
        if not self.capturing:
            return False
        if v or h:
            self.send(GU.MSG_G_SCROLL, GU.pack_scroll(v, h))
        return True

    def on_key(self, down: bool, flags: int, scan: int, vk: int) -> bool:
        if not self.capturing:
            return False
        for group, vks in ((self.ctrl, CTRL_VKS), (self.alt, ALT_VKS)):
            if vk in vks and scan != GU.FAKE_CTRL_SCAN:     # AltGr's extra Ctrl is not a Ctrl press
                (group.add if down else group.discard)(vk)
        if down and self.ctrl and self.alt and self.edge and vk == ARROW_VK.get(AWAY[self.edge]):
            self.end(None, notify=True)                     # Ctrl+Alt + the arrow away from the Mac
            return True
        key = (flags & KEYFLAG_EXTENDED, scan, vk)
        if down and scan != GU.FAKE_CTRL_SCAN and _MAC_KEYS.translate(flags, scan, vk) is None:
            return False                                    # no such key on the Mac (volume, media): stays here
        if down:
            self.remote_keys.add(key)
        elif key in self.remote_keys:
            self.remote_keys.discard(key)
        else:
            return False                                    # pressed before it went to the Mac (e.g. Ctrl, Alt)
        self.send(GU.MSG_G_KEY, GU.pack_key(down, flags, scan, vk))
        return True

    # -- home --
    def end(self, ratio: Optional[float], notify: bool) -> None:
        with self.lock:
            if not self.capturing:
                return
            self.capturing = False
            try:
                self.backend.capture(False)
            finally:
                self.backend.show_cursor()
            pos = self.home
            if ratio is not None and self.edge:
                try:
                    pos = entry_point(self.backend.monitors(), self.edge, ratio)
                except Exception:
                    pos = self.home
            if pos is not None:
                self.backend.set_cursor(int(pos[0]), int(pos[1]))
            self.guard.reset(armed=False)
            self.remote_keys.clear()
            self.remote_buttons.clear()
            self.ctrl.clear()
            self.alt.clear()
            if notify:
                self.send(GU.MSG_G_LEAVE, b"")
        log.info("This PC's keyboard and mouse are back on this PC")
        self._notify(False, pos if ratio is not None else None)

    def _notify(self, active: bool, pos) -> None:
        for fn in list(self.listeners):
            try:
                fn(active, pos)
            except Exception:
                log.exception("guest listener")
