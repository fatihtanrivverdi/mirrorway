"""macOS input capture: CGEventTap + pointer handling (Quartz / pyobjc)."""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import threading
import time
from typing import Dict, List, Optional

import Quartz

from .. import gestures as G
from ..geometry import Rect
from .router import PASS_AS_MOVE, SUPPRESS, InputEvent, Router

log = logging.getLogger("mirrorway.tap")

NX_SYSDEFINED = 14
NX_SUBTYPE_AUX_CONTROL_BUTTONS = 8


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
def accessibility_trusted(prompt: bool = False) -> bool:
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: bool(prompt)}))
    except Exception:
        pass
    try:
        lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return True   # if unknown, leave it alone; creating the tap reports it anyway


def request_input_monitoring() -> None:
    for fn in ("CGRequestListenEventAccess", "CGRequestPostEventAccess"):
        try:
            getattr(Quartz, fn)()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Displays and the pointer
# ---------------------------------------------------------------------------
def active_display_ids() -> List[int]:
    try:
        err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
        if err == 0 and ids:
            return list(ids[:count])
    except Exception:
        log.exception("could not get the display list")
    return [Quartz.CGMainDisplayID()]


def display_rect(did: int) -> Rect:
    b = Quartz.CGDisplayBounds(did)
    return Rect(float(b.origin.x), float(b.origin.y), float(b.size.width), float(b.size.height))


class _CursorHack:
    """Lets a background app hide the pointer (the Synergy/Deskflow method)."""

    def __init__(self):
        self.ok = False
        try:
            cg = None
            for path in ("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices",
                         "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics",
                         "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight"):
                try:
                    lib = ctypes.cdll.LoadLibrary(path)
                    getattr(lib, "CGSSetConnectionProperty")
                    getattr(lib, "_CGSDefaultConnection")
                    cg = lib
                    break
                except (OSError, AttributeError):
                    continue
            if cg is None:
                return
            cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation")
                                         or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            cf.CFStringCreateWithCString.restype = ctypes.c_void_p
            cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
            key = cf.CFStringCreateWithCString(None, b"SetsCursorInBackground", 0x08000100)
            true_ref = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
            cg._CGSDefaultConnection.restype = ctypes.c_int
            cg.CGSSetConnectionProperty.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
            cg.CGSSetConnectionProperty.restype = ctypes.c_int
            conn = cg._CGSDefaultConnection()
            cg.CGSSetConnectionProperty(conn, conn, key, true_ref)
            self.ok = True
        except Exception as exc:
            log.debug("Could not set SetsCursorInBackground: %s", exc)


class MacPlatform:
    def __init__(self):
        self._cache: Optional[List[Rect]] = None
        self._cache_t = 0.0
        self._hidden = False
        self._lock = threading.Lock()
        self._cursor_hack = None
        self._event_source = None
        self._disable_warp_suppression()

    def _disable_warp_suppression(self) -> None:
        # After warping the pointer macOS suppresses local events for ~0.25 s; we turn that off.
        try:
            Quartz.CGSetLocalEventsSuppressionInterval(0.0)
        except Exception:
            pass
        try:
            src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)
            Quartz.CGEventSourceSetLocalEventsSuppressionInterval(src, 0.0)
            self._event_source = src
        except Exception:
            pass

    def displays(self) -> List[Rect]:
        now = time.monotonic()
        if self._cache is None or now - self._cache_t > 2.0:
            self._cache = [display_rect(d) for d in active_display_ids()]
            self._cache_t = now
        return self._cache

    def main_display(self) -> Rect:
        return display_rect(Quartz.CGMainDisplayID())

    def display_rect_id(self, did: int) -> Rect:
        return display_rect(int(did))

    def invalidate(self) -> None:
        """A display was added/removed (second screen): refresh the list on the next query."""
        self._cache = None

    def warp(self, x: float, y: float) -> None:
        Quartz.CGWarpMouseCursorPosition(Quartz.CGPointMake(x, y))
        try:
            Quartz.CGAssociateMouseAndMouseCursorPosition(True)
        except Exception:
            pass

    def hide_cursor(self) -> None:
        with self._lock:
            if self._hidden:
                return
            if self._cursor_hack is None:
                self._cursor_hack = _CursorHack()
            Quartz.CGDisplayHideCursor(Quartz.CGMainDisplayID())
            self._hidden = True

    def show_cursor(self) -> None:
        with self._lock:
            if not self._hidden:
                return
            Quartz.CGDisplayShowCursor(Quartz.CGMainDisplayID())
            self._hidden = False

    def cursor_pos(self):
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return float(loc.x), float(loc.y)


# ---------------------------------------------------------------------------
# Keyboard type (ISO / ANSI)
# ---------------------------------------------------------------------------
class _KeyboardTypes:
    ISO = 0x49534F20    # 'ISO '
    ANSI = 0x414E5349   # 'ANSI'
    JIS = 0x4A495320    # 'JIS '

    def __init__(self):
        self._cache: Dict[int, Optional[bool]] = {}
        self._fn = None
        try:
            carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
            carbon.KBGetLayoutType.argtypes = [ctypes.c_int16]
            carbon.KBGetLayoutType.restype = ctypes.c_uint32
            self._fn = carbon.KBGetLayoutType
        except Exception:
            self._fn = None

    def is_iso(self, kb_type: int) -> Optional[bool]:
        if kb_type in self._cache:
            return self._cache[kb_type]
        val: Optional[bool] = None
        if self._fn is not None:
            try:
                val = self._fn(kb_type) == self.ISO
            except Exception:
                val = None
        self._cache[kb_type] = val
        return val


# ---------------------------------------------------------------------------
# Event tap thread
# ---------------------------------------------------------------------------
_MOUSE_DOWN = {
    Quartz.kCGEventLeftMouseDown: 0,
    Quartz.kCGEventRightMouseDown: 1,
}
_MOUSE_UP = {
    Quartz.kCGEventLeftMouseUp: 0,
    Quartz.kCGEventRightMouseUp: 1,
}
_DRAGS = {Quartz.kCGEventLeftMouseDragged, Quartz.kCGEventRightMouseDragged,
          Quartz.kCGEventOtherMouseDragged}
# Mac "other" button numbers: 2 = middle, 3 = back, 4 = forward
_OTHER_BUTTON_MAP = {2: 2, 3: 3, 4: 4}

_EVENT_TYPES = [
    Quartz.kCGEventMouseMoved,
    Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp,
    Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp,
    Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp,
    Quartz.kCGEventLeftMouseDragged, Quartz.kCGEventRightMouseDragged,
    Quartz.kCGEventOtherMouseDragged,
    Quartz.kCGEventScrollWheel,
    Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp, Quartz.kCGEventFlagsChanged,
    NX_SYSDEFINED,
] + list(G.EVENT_TYPES)          # trackpad gestures (only handled while Windows is controlled)
_GESTURE_TYPES = frozenset(G.EVENT_TYPES)
_FIELD_GESTURE_HID = 110         # kCGEventGestureHIDType (undocumented field; e.g. 23 = Dock gesture)
_TOUCHING = 1 | 2 | 4            # NSTouchPhaseBegan | Moved | Stationary
# Dock gesture (3/4-finger Spaces / Mission Control) fields — undocumented; the ones Mac Mouse Fix uses
_FIELD_DOCK_TYPE, _FIELD_DOCK_PROGRESS, _FIELD_DOCK_PHASE = 123, 124, 132


class _HIDDock:
    """On macOS 27 the Dock gesture's data may live in the event's attached IOHIDEvent rather than in CGEvent fields.
    It is read with CGEventCopyIOHIDEvent + IOHIDEventGet… (private but exported functions); skipped quietly if missing."""

    BASE = 23 << 16                     # IOHIDEventFieldBase(kIOHIDEventTypeDockSwipe)
    MOTION, PROGRESS, POS_X, POS_Y = BASE + 1, BASE + 2, BASE + 3, BASE + 4

    def __init__(self):
        self.ok = False
        try:
            cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            io = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
            cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self.copy = cg.CGEventCopyIOHIDEvent
            self.copy.argtypes, self.copy.restype = [ctypes.c_void_p], ctypes.c_void_p
            self.getf = io.IOHIDEventGetFloatValue
            self.getf.argtypes, self.getf.restype = [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_double
            self.geti = io.IOHIDEventGetIntegerValue
            self.geti.argtypes, self.geti.restype = [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_long
            self.phase = io.IOHIDEventGetPhase
            self.phase.argtypes, self.phase.restype = [ctypes.c_void_p], ctypes.c_uint32
            self.release = cf.CFRelease
            self.release.argtypes, self.release.restype = [ctypes.c_void_p], None
            self.ok = True
        except Exception as exc:
            log.debug("No IOHIDEvent functions: %s", exc)

    def read(self, event):
        if not self.ok:
            return None
        import objc
        h = self.copy(ctypes.c_void_p(objc.pyobjc_id(event)))
        if not h:
            return None
        try:
            return (int(self.geti(h, self.MOTION)), float(self.getf(h, self.PROGRESS)), int(self.phase(h)),
                    float(self.getf(h, self.POS_X)), float(self.getf(h, self.POS_Y)))
        finally:
            self.release(h)


class EventTap(threading.Thread):
    def __init__(self, router: Router):
        super().__init__(daemon=True, name="mirrorway-eventtap")
        self.router = router
        self.tap = None
        self.error: Optional[str] = None
        self.running = threading.Event()
        self._stop_evt = threading.Event()
        self._kb = _KeyboardTypes()
        self._hid_dock = None

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        mask = 0
        for t in _EVENT_TYPES:
            mask |= (1 << int(t))
        self.tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            mask,
            self._callback,
            None,
        )
        if self.tap is None:
            self.error = "permission"
            log.error("Could not create the event tap: Accessibility / Input Monitoring permission required")
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopDefaultMode)
        Quartz.CGEventTapEnable(self.tap, True)
        self.running.set()
        log.info("Input capture started")
        try:
            while not self._stop_evt.is_set():
                Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 0.5, False)
        finally:
            try:
                Quartz.CGEventTapEnable(self.tap, False)
            except Exception:
                pass
            self.running.clear()

    # ---- event translation -------------------------------------------------------
    def _convert(self, etype: int, event) -> Optional[InputEvent]:
        if etype == Quartz.kCGEventMouseMoved or etype in _DRAGS:
            loc = Quartz.CGEventGetLocation(event)
            return InputEvent(
                kind="move" if etype == Quartz.kCGEventMouseMoved else "drag",
                x=float(loc.x), y=float(loc.y),
                dx=float(Quartz.CGEventGetDoubleValueField(event, Quartz.kCGMouseEventDeltaX)),
                dy=float(Quartz.CGEventGetDoubleValueField(event, Quartz.kCGMouseEventDeltaY)),
            )
        if etype in _MOUSE_DOWN:
            return InputEvent(kind="down", button=_MOUSE_DOWN[etype])
        if etype in _MOUSE_UP:
            return InputEvent(kind="up", button=_MOUSE_UP[etype])
        if etype in (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp):
            num = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventButtonNumber))
            btn = _OTHER_BUTTON_MAP.get(num)
            if btn is None:
                return None
            return InputEvent(kind="down" if etype == Quartz.kCGEventOtherMouseDown else "up", button=btn)
        if etype == Quartz.kCGEventScrollWheel:
            continuous = bool(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGScrollWheelEventIsContinuous))
            if continuous:
                v = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGScrollWheelEventPointDeltaAxis1)
                h = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGScrollWheelEventPointDeltaAxis2)
            else:
                v = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGScrollWheelEventDeltaAxis1)
                h = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGScrollWheelEventDeltaAxis2)
            return InputEvent(kind="scroll", scroll_v=float(v), scroll_h=float(h), continuous=continuous)
        if etype in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp, Quartz.kCGEventFlagsChanged):
            keycode = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
            flags = int(Quartz.CGEventGetFlags(event))
            kb_type = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeyboardType))
            kind = {Quartz.kCGEventKeyDown: "key_down", Quartz.kCGEventKeyUp: "key_up",
                    Quartz.kCGEventFlagsChanged: "flags"}[etype]
            text = ""
            if kind == "key_down":
                try:
                    _n, chars = Quartz.CGEventKeyboardGetUnicodeString(event, 8, None, None)
                    text = str(chars or "")[:max(int(_n or 0), 0)] if _n is not None else str(chars or "")
                except Exception:
                    text = ""
            return InputEvent(kind=kind, keycode=keycode, flags=flags, kb_iso=self._kb.is_iso(kb_type),
                              text=text)
        if etype in _GESTURE_TYPES:
            if not self.router.remote:
                return None                           # the Mac handles it (fast path)
            return self._gesture(etype, event)
        if etype == NX_SYSDEFINED:
            if not self.router.remote:
                return None
            try:
                from AppKit import NSEvent
                ns = NSEvent.eventWithCGEvent_(event)
                if ns is None or ns.subtype() != NX_SUBTYPE_AUX_CONTROL_BUTTONS:
                    return None
                data = int(ns.data1())
                key = (data & 0xFFFF0000) >> 16
                state = (data & 0x0000FF00) >> 8
                if state not in (0x0A, 0x0B):
                    return None
                return InputEvent(kind="media", media_key=key, media_down=(state == 0x0A))
            except Exception:
                return None
        return None

    def _gesture(self, etype: int, event) -> InputEvent:
        ev = InputEvent(kind="gesture", gtype=int(etype))
        try:
            ev.hid = int(Quartz.CGEventGetIntegerValueField(event, _FIELD_GESTURE_HID))
        except Exception:
            ev.hid = 0
        if ev.hid == G.HID_DOCK_SWIPE:
            self._dock_fields(ev, event)
        try:
            from AppKit import NSEvent
            ns = NSEvent.eventWithCGEvent_(event)
            if ns is None:
                return ev
            ev.gtype = int(ns.type())
            if ev.gtype == G.NS_MAGNIFY:
                ev.magnification = float(ns.magnification())
                ev.phase = int(ns.phase())
            elif ev.gtype == G.NS_GESTURE:
                pts = [t.normalizedPosition() for t in (ns.touchesMatchingPhase_inView_(_TOUCHING, None) or [])
                       if not t.isResting()]
                ev.fingers = len(pts)
                if pts:
                    ev.gx = sum(p.x for p in pts) / len(pts)
                    ev.gy = sum(p.y for p in pts) / len(pts)
        except Exception as exc:
            log.debug("could not read a trackpad event: %s", exc)
        return ev

    def _dock_fields(self, ev: InputEvent, event) -> None:
        """Dock gesture: type (1 horizontal, 2 vertical, 3 pinch), progress, phase — CGEvent fields first, then IOHIDEvent."""
        g_int, g_dbl = Quartz.CGEventGetIntegerValueField, Quartz.CGEventGetDoubleValueField
        try:
            ev.dock_type = int(g_int(event, _FIELD_DOCK_TYPE))
            ev.dock_progress = float(g_dbl(event, _FIELD_DOCK_PROGRESS))
            ev.dock_phase = int(g_int(event, _FIELD_DOCK_PHASE)) or int(g_dbl(event, _FIELD_DOCK_PHASE))
        except Exception as exc:
            log.debug("Could not read the Dock fields: %s", exc)
        try:
            if self._hid_dock is None:
                self._hid_dock = _HIDDock()
            got = self._hid_dock.read(event)
            if got is not None:
                ev.hid_motion, ev.hid_progress, ev.hid_phase, ev.hid_x, ev.hid_y = got
        except Exception as exc:
            log.debug("Could not read the IOHIDEvent: %s", exc)

    def _callback(self, proxy, etype, event, refcon):
        try:
            if etype in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
                log.warning("The system disabled the event tap, reopening it")
                Quartz.CGEventTapEnable(self.tap, True)
                return event
            ev = self._convert(int(etype), event)
            if ev is None:
                return event
            decision = self.router.handle(ev)
            if decision == SUPPRESS:
                return None
            if decision == PASS_AS_MOVE and etype != Quartz.kCGEventMouseMoved:
                Quartz.CGEventSetType(event, Quartz.kCGEventMouseMoved)
            return event
        except Exception:
            log.exception("could not handle an event")
            return event
