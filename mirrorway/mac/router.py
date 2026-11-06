"""Mac-side input routing logic (independent of Quartz, testable).

For every input event it returns one of these decisions:
  PASS          the event goes to the Mac as usual
  SUPPRESS      the event is hidden from the Mac (it may have been sent to Windows)
  PASS_AS_MOVE  the event is passed on as a "mouse move" (drags in remote mode)
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

from .. import gestures as G
from .. import quick
from ..extend import vd_point
from ..geometry import OPPOSITE, EdgeGuard, entry_point
from ..keymap import (FLAG_COMMAND, FLAG_CONTROL, FLAG_OPTION, FLAG_SHIFT, MAC_CAPS, MAC_FN, MAC_LOPT,
                      MAC_ROPT, KeyTranslator, modifier_is_down)
from ..protocol import (MSG_BUTTON, MSG_ENTER, MSG_GESTURE, MSG_EXIT, MSG_KEY, MSG_MOVE, MSG_RELEASE_ALL,
                        MSG_SCROLL, MSG_TEXT, pack_button, pack_key, pack_move, pack_scroll)

log = logging.getLogger("mirrorway.router")

PASS, SUPPRESS, PASS_AS_MOVE = 0, 1, 2

SWITCH_COOLDOWN = 0.25      # time after a switch during which bouncing back is prevented (s)
PARK_RADIUS = 120           # in remote mode, pull the pointer back once it is this far from the centre
MAX_STEP = 250              # largest movement accepted in one event (safety limit)
WARP_STALE_WINDOW = 0.3     # events with a "stale position" arriving this soon after a warp are dropped
PIXEL_SCROLL_FACTOR = 2.0   # trackpad pixels -> Windows wheel units
LINE_SCROLL_FACTOR = 120.0  # mouse wheel line -> Windows wheel units

ARROW_LEFT, ARROW_RIGHT, ARROW_DOWN, ARROW_UP = 123, 124, 125, 126
_HOTKEYS = {  # side -> (switch to Windows, back to the Mac)
    "right": (ARROW_RIGHT, ARROW_LEFT),
    "left": (ARROW_LEFT, ARROW_RIGHT),
    "top": (ARROW_UP, ARROW_DOWN),
    "bottom": (ARROW_DOWN, ARROW_UP),
}


@dataclass
class InputEvent:
    kind: str                  # move, drag, down, up, scroll, key_down, key_up, flags, media
    x: float = 0.0
    y: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    button: int = 0
    keycode: int = 0
    flags: int = 0
    kb_iso: Optional[bool] = None
    scroll_v: float = 0.0
    scroll_h: float = 0.0
    continuous: bool = False
    media_key: int = -1
    media_down: bool = False
    text: str = ""             # the character(s) the key produces on the Mac (key_down only)
    # trackpad gesture (kind="gesture"): event type, HID subtype, finger count and average position, magnification, phase
    gtype: int = 0
    hid: int = 0
    fingers: int = -1          # -1: no touch information in this event
    gx: float = 0.0
    gy: float = 0.0
    magnification: float = 0.0
    phase: int = 0
    # Dock gesture (HID 23): from the CGEvent fields and from the IOHIDEvent (whichever is filled)
    dock_type: int = 0
    dock_progress: float = 0.0
    dock_phase: int = 0
    hid_motion: int = 0
    hid_progress: float = 0.0
    hid_phase: int = 0
    hid_x: float = 0.0
    hid_y: float = 0.0


def printable(text: str) -> bool:
    """A visible character that can be typed on Windows as Unicode? (not Tab, Enter, function keys)"""
    if not text:
        return False
    for ch in text:
        o = ord(ch)
        if o < 0x20 or o == 0x7F or 0xF700 <= o <= 0xF8FF:
            return False
    return True


class Router:
    def __init__(self, platform, sink, settings, clock: Callable[[], float] = time.monotonic):
        """
        platform: displays() -> List[Rect], main_display() -> Rect, warp(x, y),
                  hide_cursor(), show_cursor(), cursor_pos() -> (x, y)
        sink:     connected() -> bool, send(mtype, payload), send_json(mtype, obj)
        settings: a settings object that provides .get(key)
        """
        self.platform = platform
        self.sink = sink
        self.settings = settings
        self.clock = clock
        self.lock = threading.RLock()
        self.remote = False
        self.remote_down: Dict[int, Tuple[int, int, int]] = {}
        self.mac_down: Set[int] = set()
        self.swallowed: Set[int] = set()
        self.buttons_down: Set[int] = set()
        self.last_switch = -1e9
        self.park: Tuple[float, float] = (0.0, 0.0)
        self._last_loc: Tuple[float, float] = (0.0, 0.0)
        self._warp_vec: Optional[Tuple[float, float]] = None
        self._warp_t = -1e9
        self._frozen = 0
        self._acc_v = 0.0
        self._acc_h = 0.0
        self._listeners: List[Callable[[bool], None]] = []
        self.auto_scale = 1.0                 # Windows/Mac screen width ratio
        self.edge_guard = EdgeGuard(clock)    # prevents accidental switching back at the edge
        self.opt_pending: Set[int] = set()    # Option keys that are down but not yet sent to Windows
        self.text_keys: Set[int] = set()      # keys typed as Unicode (their release is swallowed)
        self.translator = KeyTranslator()
        self.extend = None                    # second screen (ExtendManager): route(), win_point(), m_rect(), vd_rect()
        self.vd_guards: Dict[str, EdgeGuard] = {}
        self.gestures = G.GestureTracker()
        self._gesture_seen: Set[tuple] = set()   # diagnostics: (type, HID) pairs seen in this remote session
        self._dock_logs = 0                       # diagnostics: Dock gestures logged so far (per app run)
        self._dock_diag = None
        self._max_fingers = 0
        self._last_swipe_t = -1e9
        # quick shortcuts (Control + Option + letter): {key code: action}; on_action(action) is called from the
        # event tap thread and must return quickly
        self.action_keys: Dict[int, str] = {}
        self.on_action: Optional[Callable[[str], None]] = None
        self.reload_settings()

    # ---- settings ----------------------------------------------------------
    def reload_settings(self) -> None:
        with self.lock:
            layout = self.settings.get("keyboard_layout")
            self.translator = KeyTranslator(
                swap_cmd_ctrl=bool(self.settings.get("swap_cmd_ctrl")),
                left_option=self.settings.get("left_option") or "alt",
                iso=(layout != "ansi"),
            )
            self._layout_override = None if layout in (None, "auto") else (layout == "iso")
            self.action_keys = quick.mac_action_keys(self.settings)

    def add_listener(self, fn: Callable[[bool], None]) -> None:
        self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn(self.remote)
            except Exception:
                log.exception("state listener error")

    @property
    def side(self) -> str:
        s = self.settings.get("side")
        return s if s in _HOTKEYS else "right"

    # ---- main entry point ------------------------------------------------
    def handle(self, ev: InputEvent) -> int:
        with self.lock:
            k = ev.kind
            if k in ("move", "drag"):
                return self._on_move(ev)
            if k == "down":
                return self._on_button(ev, True)
            if k == "up":
                return self._on_button(ev, False)
            if k == "scroll":
                return self._on_scroll(ev)
            if k == "key_down":
                return self._on_key_down(ev)
            if k == "key_up":
                return self._on_key_up(ev)
            if k == "flags":
                return self._on_flags(ev)
            if k == "media":
                return self._on_media(ev)
            if k == "gesture":
                return self._on_gesture(ev)
            return PASS

    # ---- mouse ---------------------------------------------------------------
    def _on_move(self, ev: InputEvent) -> int:
        if not self.remote:
            if (ev.kind == "move" and self.settings.get("edge_switch")
                    and self.clock() - self.last_switch > SWITCH_COOLDOWN
                    and self.sink.connected()):
                displays = self.platform.displays()
                hit = self._vd_exit(displays, ev)
                if hit is not None:
                    if hit == "wall":
                        return PASS
                    side, ratio, wpos = hit
                    self._enter(OPPOSITE[side], ratio, pos=(ev.x, ev.y), win_pos=wpos)
                    return PASS
                ratio = self.edge_guard.check(displays, ev.x, ev.y, ev.dx, ev.dy, self.side)
                if ratio is not None:
                    self._enter(OPPOSITE[self.side], ratio, pos=(ev.x, ev.y))
            return PASS
        # Movement comes from the change in position rather than the events' delta field: after warping the
        # pointer, macOS may add the warp distance to the next event's delta, which made the pointer on Windows
        # shoot to the edge and bounce back.
        now = self.clock()
        lx, ly = self._last_loc
        mx, my = ev.x - lx, ev.y - ly
        if self._warp_vec is not None:
            if now - self._warp_t <= WARP_STALE_WINDOW:
                wx, wy = self._warp_vec
                wlen = math.hypot(wx, wy)
                if wlen > 20 and math.hypot(mx, my) > 0.5 * wlen and (mx * wx + my * wy) < 0:
                    # an event with a stale position, recorded before the warp was processed: ignore
                    self._warp_vec = None
                    return PASS_AS_MOVE
            self._warp_vec = None
        self._last_loc = (ev.x, ev.y)
        if not mx and not my and (ev.dx or ev.dy):
            # If the position does not change at all (the system froze the pointer), fall back to the delta field
            self._frozen += 1
            if self._frozen >= 5:
                mx, my = ev.dx, ev.dy
        else:
            self._frozen = 0
        step = math.hypot(mx, my)
        if step > MAX_STEP:
            mx, my = mx * MAX_STEP / step, my * MAX_STEP / step
        speed = self.effective_speed()
        if mx or my:
            self.sink.send(MSG_MOVE, pack_move(mx * speed, my * speed))
        px, py = self.park
        if abs(ev.x - px) > PARK_RADIUS or abs(ev.y - py) > PARK_RADIUS:
            self._park_cursor(ev.x, ev.y)
        return PASS_AS_MOVE

    def _vd_exit(self, displays, ev: InputEvent):
        """Second screen: if the pointer is pushed against the edge of the virtual display that faces a neighbouring
        monitor on Windows, (edge, fraction, entry point on Windows)."""
        ext = self.extend
        if ext is None:
            return None
        try:
            route = ext.route()
        except Exception:
            log.exception("could not compute the second-screen route")
            return None
        if not route:
            return None
        vd = route["vd"]
        if not vd.contains(ev.x, ev.y):
            return None
        if not route["sides"]:
            return None                  # Windows with one monitor: the usual edge applies
        for side in route["sides"]:
            g = self.vd_guards.get(side)
            if g is None:
                g = self.vd_guards[side] = EdgeGuard(self.clock)
            ratio = g.check(displays, ev.x, ev.y, ev.dx, ev.dy, side, corner_guard=0)
            if ratio is not None:
                wpos = ext.win_point(side, ratio)
                if wpos is not None:
                    return side, ratio, wpos
        # Edges of the virtual display without a neighbour on Windows are walls: e.g. heading for the menu bar
        # at the top edge must not move the pointer to Windows by accident (there is no monitor there)
        return "wall"

    def _extend_routes(self) -> bool:
        ext = self.extend
        if ext is None:
            return False
        try:
            route = ext.route()
        except Exception:
            return False
        return bool(route and route.get("sides"))

    def _park_cursor(self, from_x: float, from_y: float) -> None:
        px, py = self.park
        self.platform.warp(px, py)
        self._warp_vec = (px - from_x, py - from_y)
        self._warp_t = self.clock()
        self._last_loc = (px, py)

    def effective_speed(self) -> float:
        speed = float(self.settings.get("speed") or 1.0)
        if self.settings.get("auto_speed", True):
            speed *= self.auto_scale
        return speed

    def set_auto_scale(self, value: float) -> None:
        with self.lock:
            self.auto_scale = min(max(float(value), 0.5), 4.0)

    def _option_text_mode(self) -> bool:
        if not self.settings.get("option_unicode", True):
            return False
        supports = getattr(self.sink, "supports", None)
        return bool(supports and supports("text"))

    def _flush_options(self) -> None:
        """Send pending Option keys to Windows as Alt (for Alt+Tab, Alt+click etc.)."""
        for kc in sorted(self.opt_pending):
            t = self.translator.translate(kc)
            if t is not None and kc not in self.remote_down:
                flags, scan, vk = t
                self.remote_down[kc] = t
                self.sink.send(MSG_KEY, pack_key(True, flags, scan, vk))
        self.opt_pending.clear()

    def _on_button(self, ev: InputEvent, down: bool) -> int:
        if not self.remote:
            return PASS
        if down and self.opt_pending:
            self._flush_options()
        if down:
            self.buttons_down.add(ev.button)
            self.sink.send(MSG_BUTTON, pack_button(ev.button, True))
            return SUPPRESS
        if ev.button in self.buttons_down:
            self.buttons_down.discard(ev.button)
            self.sink.send(MSG_BUTTON, pack_button(ev.button, False))
            return SUPPRESS
        return PASS   # the press started on the Mac, so the release goes to the Mac too

    def _on_scroll(self, ev: InputEvent) -> int:
        if not self.remote:
            return PASS
        if self.opt_pending:
            self._flush_options()
        f = float(self.settings.get("scroll_speed") or 1.0)
        inv = -1.0 if self.settings.get("invert_scroll") else 1.0
        factor = PIXEL_SCROLL_FACTOR if ev.continuous else LINE_SCROLL_FACTOR
        # Mac: positive vertical = up (same as Windows); positive horizontal = left (the opposite on Windows)
        self._acc_v += ev.scroll_v * factor * f * inv
        self._acc_h += -ev.scroll_h * factor * f * inv
        iv, ih = int(self._acc_v), int(self._acc_h)
        self._acc_v -= iv
        self._acc_h -= ih
        if iv or ih:
            self.sink.send(MSG_SCROLL, pack_scroll(iv, ih))
        return SUPPRESS

    # ---- keyboard -------------------------------------------------------------
    def _hotkey(self, ev: InputEvent) -> Optional[str]:
        f = ev.flags
        if (f & FLAG_CONTROL) and (f & FLAG_OPTION) and not (f & FLAG_COMMAND):
            to_win, to_mac = _HOTKEYS[self.side]
            if ev.keycode == to_win:
                return "to_win"
            if ev.keycode == to_mac:
                return "to_mac"
        return None

    def _quick_action(self, ev: InputEvent) -> Optional[str]:
        f = ev.flags
        if self.on_action is None or not (f & FLAG_CONTROL) or not (f & FLAG_OPTION):
            return None
        if f & (FLAG_COMMAND | FLAG_SHIFT):
            return None
        return self.action_keys.get(ev.keycode)

    def _iso(self, ev: InputEvent) -> Optional[bool]:
        if self._layout_override is not None:
            return self._layout_override
        return ev.kb_iso

    def _on_key_down(self, ev: InputEvent) -> int:
        kc = ev.keycode
        if kc in self.swallowed:
            return SUPPRESS
        hot = self._hotkey(ev)
        if hot == "to_win":
            if self.remote:
                self.swallowed.add(kc)
                return SUPPRESS
            if self.sink.connected():
                self._enter("center", 0.5)
                self.swallowed.add(kc)
                return SUPPRESS
        elif hot == "to_mac" and self.remote:
            self._leave(None, notify=True)
            self.swallowed.add(kc)
            return SUPPRESS
        action = self._quick_action(ev)
        if action is not None:
            self.swallowed.add(kc)               # its release (and Windows) never see it
            try:
                self.on_action(action)
            except Exception:
                log.exception("quick shortcut %s failed", action)
            return SUPPRESS

        if not self.remote:
            self.mac_down.add(kc)
            return PASS
        if kc in self.mac_down:      # repeat of a key held down on the Mac
            return SUPPRESS
        if kc in self.text_keys:     # auto-repeat of a key typed as Unicode
            if printable(ev.text):
                self.sink.send(MSG_TEXT, ev.text.encode("utf-8"))
            return SUPPRESS
        if self.opt_pending:
            if not (ev.flags & (FLAG_COMMAND | FLAG_CONTROL)) and printable(ev.text):
                # Option + character: type the character the Mac produces (e.g. @, €) as is
                self.text_keys.add(kc)
                self.sink.send(MSG_TEXT, ev.text.encode("utf-8"))
                return SUPPRESS
            self._flush_options()    # Option + Tab/F4/arrow/shortcut: act as Alt
        t = self.translator.translate(kc, self._iso(ev))
        if t is None:
            self.swallowed.add(kc)
            return SUPPRESS
        flags, scan, vk = t
        self.remote_down[kc] = t
        self.sink.send(MSG_KEY, pack_key(True, flags, scan, vk))
        return SUPPRESS

    def _on_key_up(self, ev: InputEvent) -> int:
        kc = ev.keycode
        if kc in self.text_keys:
            self.text_keys.discard(kc)
            return SUPPRESS
        if kc in self.remote_down:
            flags, scan, vk = self.remote_down.pop(kc)
            self.sink.send(MSG_KEY, pack_key(False, flags, scan, vk))
            return SUPPRESS
        if kc in self.swallowed:
            self.swallowed.discard(kc)
            return SUPPRESS
        if kc in self.mac_down:
            self.mac_down.discard(kc)
            return PASS
        return SUPPRESS if self.remote else PASS

    def _on_flags(self, ev: InputEvent) -> int:
        # Modifier keys also pass to the Mac (so the Mac's modifier state stays consistent).
        kc = ev.keycode
        if not self.remote or kc == MAC_FN:
            return PASS
        if kc in (MAC_LOPT, MAC_ROPT) and self._option_text_mode():
            if modifier_is_down(kc, ev.flags):
                if kc not in self.remote_down:
                    self.opt_pending.add(kc)     # what goes to Windows depends on the next key
                return PASS
            if kc in self.opt_pending:
                self.opt_pending.discard(kc)     # Option alone was pressed and released: send nothing
                return PASS
        t = self.translator.translate(kc, self._iso(ev))
        if t is None:
            return PASS
        flags, scan, vk = t
        if kc == MAC_CAPS:   # the Mac produces one event per press -> press and release on Windows
            self.sink.send(MSG_KEY, pack_key(True, flags, scan, vk))
            self.sink.send(MSG_KEY, pack_key(False, flags, scan, vk))
            return PASS
        if modifier_is_down(kc, ev.flags):
            if kc not in self.remote_down:
                self.remote_down[kc] = t
                self.sink.send(MSG_KEY, pack_key(True, flags, scan, vk))
        elif kc in self.remote_down:
            self.remote_down.pop(kc)
            self.sink.send(MSG_KEY, pack_key(False, flags, scan, vk))
        return PASS

    def _on_media(self, ev: InputEvent) -> int:
        if not self.remote:
            return PASS
        t = self.translator.media(ev.media_key)
        if t is None:
            return PASS     # brightness etc. stay on the Mac
        flags, scan, vk = t
        self.sink.send(MSG_KEY, pack_key(ev.media_down, flags, scan, vk))
        return SUPPRESS

    # ---- trackpad gestures -----------------------------------------------
    def _on_gesture(self, ev: InputEvent) -> int:
        """While Windows is controlled, trackpad gestures do not reach the Mac; recognised ones become their Windows equivalents."""
        if not self.remote or not self.settings.get("gestures", True):
            return PASS
        key = (ev.gtype, ev.hid)
        if key not in self._gesture_seen and len(self._gesture_seen) < 12:
            self._gesture_seen.add(key)
            log.info("Trackpad event routed to Windows (suppressed on the Mac): type %d, HID %d", ev.gtype, ev.hid)
        action = None
        if ev.hid == G.HID_DOCK_SWIPE:
            # macOS's own Spaces / Mission Control gesture: the data is in the CGEvent fields or in the IOHIDEvent
            dtype = ev.dock_type or ev.hid_motion
            progress = ev.dock_progress or ev.hid_progress
            phase = ev.dock_phase or ev.hid_phase
            action = self.gestures.dock(dtype, progress, phase, self.clock())
            self._dock_diag = ev
            done, self.gestures.last_dock = self.gestures.last_dock, None
            if done is not None and self._dock_logs < 12:
                self._dock_logs += 1
                log.info("Trackpad Dock gesture: type %d, progress %.3f, phase %d, %d events (CG: type %d progress %.3f "
                         "phase %d; HID: motion %d progress %.3f phase %d position %.2f,%.2f) -> %s",
                         done["type"], done["last"], done["end"], done["n"], ev.dock_type, ev.dock_progress,
                         ev.dock_phase, ev.hid_motion, ev.hid_progress, ev.hid_phase, ev.hid_x, ev.hid_y,
                         G.describe(action) if action else "no action")
        elif ev.gtype == G.NS_MAGNIFY and ev.hid in (0, G.HID_ZOOM):
            action = self.gestures.magnify(ev.magnification, ev.phase)
            if ev.phase & (G.PHASE_ENDED | G.PHASE_CANCELLED) and self.gestures.zoom_total:
                log.info("Trackpad: %s", G.describe({"action": "zoom", "steps": self.gestures.zoom_total}))
                self.gestures.zoom_total = 0
        elif ev.gtype in (G.NS_BEGIN_GESTURE, G.NS_END_GESTURE):
            self.gestures.touches(0, 0.0, 0.0)       # gesture boundary: reset the tracker
        elif ev.fingers >= 0:
            action = self.gestures.touches(ev.fingers, ev.gx, ev.gy)
            if ev.fingers > self._max_fingers:
                self._max_fingers = ev.fingers
                log.info("Trackpad: %d fingers seen", ev.fingers)
        if action is not None and action.get("action") != "zoom":
            now = self.clock()
            if now - self._last_swipe_t < 0.6:
                action = None                       # the same swipe was recognised two ways: once
            else:
                self._last_swipe_t = now
                log.info("Trackpad: %s", G.describe(action))
        if action is not None:
            supports = getattr(self.sink, "supports", None)
            if supports is None or supports(G.FEATURE):
                self.sink.send_json(MSG_GESTURE, action)
        return SUPPRESS

    # ---- mode switches ------------------------------------------------------
    def _enter(self, from_side: str, ratio: float, pos: Optional[Tuple[float, float]] = None,
               win_pos: Optional[Tuple[float, float]] = None) -> None:
        main = self.platform.main_display()
        self.park = (main.x + main.w / 2.0, main.y + main.h / 2.0)
        self.remote = True
        self.last_switch = self.clock()
        self._acc_v = self._acc_h = 0.0
        self.gestures.reset()
        self._gesture_seen.clear()
        self._max_fingers = 0
        self._last_swipe_t = -1e9
        if pos is None:
            try:
                pos = self.platform.cursor_pos()
            except Exception:
                pos = self.park
        try:
            self.platform.hide_cursor()
            self._park_cursor(*pos)
        except Exception:
            log.exception("could not hide the pointer")
        msg = {"from": from_side, "ratio": float(ratio), "exit": OPPOSITE[self.side]}
        if win_pos is not None:
            msg["pos"] = [float(win_pos[0]), float(win_pos[1])]
        mrect = None
        if self.extend is not None:
            try:
                mrect = self.extend.m_rect()
            except Exception:
                mrect = None
        if mrect:
            msg["extend"] = mrect
        self.sink.send_json(MSG_ENTER, msg)
        log.info("Control on Windows (%s, %.2f%s)", from_side, ratio, ", from the second screen" if win_pos else "")
        self._notify()

    def _leave(self, ratio: Optional[float], notify: bool, vd: Optional[Tuple[float, float]] = None) -> None:
        if not self.remote:
            return
        self.remote = False
        self.last_switch = self.clock()
        self.edge_guard.reset(armed=False)
        for g in self.vd_guards.values():
            g.reset(armed=False)
        self.remote_down.clear()
        self.buttons_down.clear()
        self.opt_pending.clear()
        self.text_keys.clear()
        if notify:
            try:
                self.sink.send(MSG_RELEASE_ALL)
                self.sink.send(MSG_EXIT)
            except Exception:
                pass
        displays = self.platform.displays()
        vr = None
        if vd is not None and self.extend is not None:
            try:
                vr = self.extend.vd_rect()
            except Exception:
                vr = None
        if vr is not None:
            pos = vd_point(vr, vd[0], vd[1], inset=4)        # entered the second-screen monitor on Windows
        elif ratio is not None and self._extend_routes():
            # With the second screen open, returning through Windows' usual edge lands on the Mac's own display,
            # even if the virtual display covers that edge (physically the Mac is there)
            pos = entry_point([self.platform.main_display()], self.side, ratio, inset=4)
        elif ratio is None or not displays:
            main = self.platform.main_display()
            pos = (main.x + main.w / 2.0, main.y + main.h / 2.0)
        else:
            pos = entry_point(displays, self.side, ratio, inset=4)
        try:
            self.platform.warp(*pos)
            self.platform.show_cursor()
        except Exception:
            log.exception("could not show the pointer")
        log.info("Control returned to the Mac%s", " (to the second screen)" if vr is not None else "")
        self._notify()

    # ---- called from outside ----------------------------------------------
    def enter_center(self) -> bool:
        with self.lock:
            if self.remote or not self.sink.connected():
                return False
            self._enter("center", 0.5)
            return True

    def leave_to_mac(self) -> bool:
        """Back to the Mac (the panel's button); the pointer goes to the middle of the main display."""
        with self.lock:
            if not self.remote:
                return False
            self._leave(None, notify=True)
            return True

    def leave_from_client(self, ratio: float, vd: Optional[Tuple[float, float]] = None) -> None:
        with self.lock:
            self._leave(ratio, notify=False, vd=vd)

    def force_local(self) -> None:
        """When the connection drops or the app quits."""
        with self.lock:
            self._leave(None, notify=False)
