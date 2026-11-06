"""Trackpad gestures: while Windows is controlled, multi-finger gestures on the MacBook trackpad become their
Windows equivalents (the platform-independent, testable part).

  3 or 4 fingers left / right   ->  next / previous Windows virtual desktop  (Win+Ctrl+→ / ←)
                                     (as on macOS: fingers left = the desktop on the right)
  3 or 4 fingers up             ->  Task View (Win+Tab) — the Mission Control equivalent
  3 or 4 fingers down           ->  Show desktop (Win+D) — as on a Windows trackpad
  two-finger pinch / spread     ->  zoom out / in (Ctrl + wheel)

The Mac does not act on these gestures itself while Windows is controlled (Spaces stay, Mission Control stays shut).
Positions are relative to the trackpad, 0..1 (x grows to the right, y upwards; NSTouch.normalizedPosition).
"""
from __future__ import annotations

from typing import List, Optional

FEATURE = "gestures"

# Windows virtual key codes (vk, extended key?)
VK_LWIN, VK_CONTROL, VK_TAB, VK_LEFT, VK_RIGHT, VK_D = 0x5B, 0x11, 0x09, 0x25, 0x27, 0x44
COMBOS = {
    "desktop_right": [(VK_LWIN, True), (VK_CONTROL, False), (VK_RIGHT, True)],
    "desktop_left": [(VK_LWIN, True), (VK_CONTROL, False), (VK_LEFT, True)],
    "task_view": [(VK_LWIN, True), (VK_TAB, False)],
    "show_desktop": [(VK_LWIN, True), (VK_D, False)],
}
LABELS = {
    "desktop_right": "desktop on the right", "desktop_left": "desktop on the left",
    "task_view": "Task View", "show_desktop": "show desktop", "zoom": "zoom",
}

# NSEvent types (they also arrive as CGEvent types)
NS_ROTATE, NS_BEGIN_GESTURE, NS_END_GESTURE = 18, 19, 20
NS_GESTURE, NS_MAGNIFY, NS_SWIPE, NS_SMART_MAGNIFY = 29, 30, 31, 32
EVENT_TYPES = (NS_ROTATE, NS_BEGIN_GESTURE, NS_END_GESTURE, NS_GESTURE, NS_MAGNIFY, NS_SWIPE, NS_SMART_MAGNIFY)
PHASE_ENDED, PHASE_CANCELLED = 8, 16               # NSEventPhase
HID_ZOOM, HID_DOCK_SWIPE = 8, 23                   # IOHIDEventType: pinch; Spaces / Mission Control gesture
DOCK_HORIZONTAL, DOCK_VERTICAL, DOCK_PINCH = 1, 2, 3
HID_BEGAN, HID_CHANGED, HID_ENDED, HID_CANCELLED = 1, 2, 4, 8   # IOHIDEventPhaseBits


class GestureTracker:
    SWIPE_X = 0.14          # share of the trackpad width
    SWIPE_Y = 0.11
    DOMINANCE = 1.4         # the dominant direction must be at least this many times the other
    ZOOM_STEP = 0.07        # this much magnification = one wheel notch
    DOCK_MIN = 0.04         # a finished Dock gesture must have progressed at least this far
    DOCK_FALLBACK = 0.3     # without phase information, act once it has progressed this far
    DOCK_IDLE = 0.5         # no events for this long = a new gesture

    def __init__(self):
        self._start = None
        self._fingers = 0
        self._fired = False
        self._mag = 0.0
        self.zoom_total = 0
        self._dock: Optional[dict] = None
        self.last_dock: Optional[dict] = None      # the last finished Dock gesture (for diagnostics)

    def reset(self) -> None:
        self._start, self._fingers, self._fired = None, 0, False
        self._mag = 0.0
        self._dock = None

    def touches(self, n: int, cx: float, cy: float) -> Optional[dict]:
        """Finger count and average position; returns an action when a swipe completes (once per gesture)."""
        if n < 3:
            self._start, self._fingers, self._fired = None, n, False
            return None
        if self._start is None or (n != self._fingers and not self._fired):
            self._start, self._fingers = (cx, cy), n       # the average jumps when the finger count changes: restart
            return None
        if self._fired:
            return None
        dx, dy = cx - self._start[0], cy - self._start[1]
        action = None
        if abs(dx) >= self.SWIPE_X and abs(dx) >= self.DOMINANCE * abs(dy):
            action = "desktop_right" if dx < 0 else "desktop_left"
        elif abs(dy) >= self.SWIPE_Y and abs(dy) >= self.DOMINANCE * abs(dx):
            action = "task_view" if dy > 0 else "show_desktop"
        if action is None:
            return None
        self._fired = True
        return {"action": action, "fingers": n}

    def dock(self, dtype: int, progress: float, phase: int, now: float) -> Optional[dict]:
        """macOS's own Spaces / Mission Control gesture (3-4 fingers): when it ends, an action by direction.

        Progress is cumulative; its sign gives the direction (negative: fingers left / up).
        """
        d = self._dock
        if d is None or phase & HID_BEGAN or now - d["t"] > self.DOCK_IDLE:
            d = self._dock = {"type": dtype, "last": progress, "n": 0, "t": now, "fired": False,
                              "phased": bool(phase)}
        else:
            d["last"], d["t"] = progress, now
            d["n"] += 1
            d["type"] = dtype or d["type"]
            d["phased"] = d["phased"] or bool(phase)
        action = None
        if phase & (HID_ENDED | HID_CANCELLED):
            self._dock = None
            self.last_dock = dict(d, end=phase)
            if (not phase & HID_CANCELLED and not d["fired"] and d["n"] >= 2
                    and abs(d["last"]) >= self.DOCK_MIN):
                action = self._dock_action(d["type"], d["last"])
        elif not d["phased"] and not d["fired"] and abs(d["last"]) >= self.DOCK_FALLBACK:
            d["fired"] = True
            action = self._dock_action(d["type"], d["last"])
            self.last_dock = dict(d, end=0)
        return action

    @staticmethod
    def _dock_action(dtype: int, progress: float) -> Optional[dict]:
        if dtype == DOCK_HORIZONTAL:
            return {"action": "desktop_right" if progress < 0 else "desktop_left"}
        if dtype == DOCK_VERTICAL:
            return {"action": "task_view" if progress < 0 else "show_desktop"}
        return None                                  # pinch (Launchpad / desktop): no equivalent

    def magnify(self, m: float, phase: int = 0) -> Optional[dict]:
        """Pinch: magnification deltas add up; every ZOOM_STEP is one notch of zoom in (negative: zoom out)."""
        self._mag += float(m or 0.0)
        steps = int(self._mag / self.ZOOM_STEP)
        if phase & (PHASE_ENDED | PHASE_CANCELLED):
            self._mag = 0.0
        else:
            self._mag -= steps * self.ZOOM_STEP
        if not steps:
            return None
        self.zoom_total += steps
        return {"action": "zoom", "steps": steps}


def describe(action: dict) -> str:
    a = action.get("action")
    if a == "zoom":
        n = int(action.get("steps") or 0)
        return f"{'zoom in' if n > 0 else 'zoom out'} ({abs(n)})"
    fingers = action.get("fingers")
    return LABELS.get(a, str(a)) + (f" ({fingers} fingers)" if fingers else "")


def key_sequence(action: str) -> List[tuple]:
    """[(pressed?, vk, extended?)]: keys are pressed in order and released in reverse."""
    combo = COMBOS.get(action) or []
    return [(True, vk, ext) for vk, ext in combo] + [(False, vk, ext) for vk, ext in reversed(combo)]
