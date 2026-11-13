"""Posting Windows's keyboard and mouse as the Mac's own input (3.6): CGEventPost at the HID level, every event
marked (kCGEventSourceUserData = guest.GUEST_MARK) so Mirrorway's own event tap lets it through untouched. Posting
needs the Accessibility permission, which Mirrorway already has for its tap."""
from __future__ import annotations

import Quartz

from ..guest import GUEST_MARK

_DOWN = {0: Quartz.kCGEventLeftMouseDown, 1: Quartz.kCGEventRightMouseDown}
_UP = {0: Quartz.kCGEventLeftMouseUp, 1: Quartz.kCGEventRightMouseUp}
_DRAG = {0: Quartz.kCGEventLeftMouseDragged, 1: Quartz.kCGEventRightMouseDragged}
_CG_BUTTON = {0: Quartz.kCGMouseButtonLeft, 1: Quartz.kCGMouseButtonRight}


class QuartzPoster:
    def __init__(self):
        self.source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)

    def _post(self, ev) -> None:
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGEventSourceUserData, GUEST_MARK)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def cursor_pos(self):
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return float(loc.x), float(loc.y)

    def move(self, x: float, y: float, dx: float, dy: float, button) -> None:
        if button is None:
            etype, cg = Quartz.kCGEventMouseMoved, Quartz.kCGMouseButtonLeft
        else:
            etype = _DRAG.get(button, Quartz.kCGEventOtherMouseDragged)
            cg = _CG_BUTTON.get(button, button)
        ev = Quartz.CGEventCreateMouseEvent(self.source, etype, (x, y), cg)
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventDeltaX, int(round(dx)))
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventDeltaY, int(round(dy)))
        self._post(ev)

    def button(self, button: int, down: bool, x: float, y: float, clicks: int) -> None:
        if button in _DOWN:
            etype = _DOWN[button] if down else _UP[button]
        else:
            etype = Quartz.kCGEventOtherMouseDown if down else Quartz.kCGEventOtherMouseUp
        ev = Quartz.CGEventCreateMouseEvent(self.source, etype, (x, y), _CG_BUTTON.get(button, button))
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, max(1, int(clicks)))
        if button not in _DOWN:
            Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventButtonNumber, int(button))
        self._post(ev)

    def scroll(self, lines_v: int, lines_h: int) -> None:
        ev = Quartz.CGEventCreateScrollWheelEvent(self.source, Quartz.kCGScrollEventUnitLine, 2, int(lines_v),
                                                  int(lines_h))
        self._post(ev)

    def key(self, keycode: int, down: bool, flags: int) -> None:
        ev = Quartz.CGEventCreateKeyboardEvent(self.source, int(keycode), bool(down))
        Quartz.CGEventSetFlags(ev, int(flags))
        self._post(ev)

    def flags_changed(self, keycode: int, flags: int) -> None:
        ev = Quartz.CGEventCreateKeyboardEvent(self.source, int(keycode), True)
        Quartz.CGEventSetType(ev, Quartz.kCGEventFlagsChanged)
        Quartz.CGEventSetFlags(ev, int(flags))
        self._post(ev)


def is_guest_event(event) -> bool:
    """For the tap: an event Mirrorway posted for Windows's devices."""
    try:
        return Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData) == GUEST_MARK
    except Exception:
        return False
