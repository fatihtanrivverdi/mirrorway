"""Screen geometry: outer edge detection and transition point calculations (platform independent)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
CORNER_GUARD = 8  # no transition this close to a corner (Mac hot corners)


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def left(self) -> float:
        return self.x

    @property
    def top(self) -> float:
        return self.y

    @property
    def right(self) -> float:   # exclusive bound
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def contains(self, px: float, py: float) -> bool:
        return self.left <= px < self.right and self.top <= py < self.bottom


def containing(rects: Sequence[Rect], px: float, py: float) -> Optional[Rect]:
    for r in rects:
        if r.contains(px, py):
            return r
    return None


def nearest(rects: Sequence[Rect], px: float, py: float) -> Optional[Rect]:
    best, best_d = None, None
    for r in rects:
        cx = min(max(px, r.left), r.right - 1)
        cy = min(max(py, r.top), r.bottom - 1)
        d = (cx - px) ** 2 + (cy - py) ** 2
        if best_d is None or d < best_d:
            best, best_d = r, d
    return best


def union(rects: Sequence[Rect]) -> Rect:
    l = min(r.left for r in rects)
    t = min(r.top for r in rects)
    rr = max(r.right for r in rects)
    b = max(r.bottom for r in rects)
    return Rect(l, t, rr - l, b - t)


def edge_ratio(rect: Rect, px: float, py: float, side: str) -> float:
    """The fraction along one display's edge (the inverse of entry_point on that display: pixel 0 -> 0, the last
    pixel -> 1)."""
    if side in ("left", "right"):
        v = (py - rect.top) / max(rect.h - 1, 1)
    else:
        v = (px - rect.left) / max(rect.w - 1, 1)
    return min(max(v, 0.0), 1.0)


def edge_group(rects: Sequence[Rect], side: str) -> Tuple[Rect, List[Rect]]:
    """The union of the displays and the displays that form its outer edge on `side`."""
    u = union(rects)
    if side == "left":
        cands = [r for r in rects if r.left == u.left]
    elif side == "right":
        cands = [r for r in rects if r.right == u.right]
    elif side == "top":
        cands = [r for r in rects if r.top == u.top]
    else:
        cands = [r for r in rects if r.bottom == u.bottom]
    return u, cands or list(rects)


def outer_ratio(rects: Sequence[Rect], r: Rect, px: float, py: float, side: str) -> float:
    """The fraction along the outer edge on `side` where the pointer (on display r) leaves: the exact inverse of
    entry_point, so leaving and coming back through the same edge lands on the same spot. One display on that edge:
    along that display; several (monitors side by side): along all of them together."""
    u, cands = edge_group(rects, side)
    if len(cands) == 1 and cands[0] == r:
        return edge_ratio(r, px, py, side)
    return edge_ratio(u, px, py, side)


def edge_exit(rects: Sequence[Rect], px: float, py: float, dx: float, dy: float, side: str,
              corner_guard: float = CORNER_GUARD) -> Optional[float]:
    """If the pointer is pushed against the outer edge towards `side`, the fraction (0..1) along that edge.

    "Outer edge" means no other display lies beyond that point (with several monitors, inner edges
    do not trigger a transition).
    """
    if not rects:
        return None
    r = containing(rects, px, py) or nearest(rects, px, py)
    if r is None:
        return None
    if side == "right":
        pushing = dx > 0 and px + dx >= r.right - 1 + 1e-6 and px >= r.right - 2
        probe = (r.right + 1, py)
    elif side == "left":
        pushing = dx < 0 and px + dx < r.left + 1 and px <= r.left + 1
        probe = (r.left - 2, py)
    elif side == "bottom":
        pushing = dy > 0 and py + dy >= r.bottom - 1 + 1e-6 and py >= r.bottom - 2
        probe = (px, r.bottom + 1)
    elif side == "top":
        pushing = dy < 0 and py + dy < r.top + 1 and py <= r.top + 1
        probe = (px, r.top - 2)
    else:
        return None
    if not pushing:
        return None
    if containing(rects, *probe) is not None:
        return None   # inner edge
    if side in ("left", "right"):
        t, span = py - r.top, r.h
    else:
        t, span = px - r.left, r.w
    if corner_guard and (t < corner_guard or t > span - corner_guard):
        return None
    return outer_ratio(rects, r, px, py, side)


def entry_point(rects: Sequence[Rect], side: str, ratio: float, inset: float = 3) -> Tuple[float, float]:
    """Where to place the pointer when entering the group of displays through the outer edge on `side`.

    side: the edge being entered (e.g. "left" = entering from the left).
    """
    ratio = min(max(ratio, 0.0), 1.0)
    if side == "center":
        r = rects[0]
        return (r.left + r.w / 2, r.top + r.h / 2)
    u, cands = edge_group(rects, side)
    if side in ("left", "right"):
        target_y = u.top + ratio * (u.h - 1)
        # among the displays touching this edge, pick the one closest to the target y
        r = min(cands, key=lambda c: 0 if c.top <= target_y < c.bottom
                else min(abs(target_y - c.top), abs(target_y - c.bottom)))
        y = r.top + ratio * (r.h - 1) if len(cands) == 1 else min(max(target_y, r.top), r.bottom - 1)
        x = r.left + inset if side == "left" else r.right - 1 - inset
        return (x, y)
    target_x = u.left + ratio * (u.w - 1)
    r = min(cands, key=lambda c: 0 if c.left <= target_x < c.right
            else min(abs(target_x - c.left), abs(target_x - c.right)))
    x = r.left + ratio * (r.w - 1) if len(cands) == 1 else min(max(target_x, r.left), r.right - 1)
    y = r.top + inset if side == "top" else r.bottom - 1 - inset
    return (x, y)


def clamp_to_rects(rects: List[Rect], px: float, py: float) -> Tuple[float, float]:
    r = containing(rects, px, py)
    if r is not None:
        return px, py
    r = nearest(rects, px, py)
    if r is None:
        return px, py
    return (min(max(px, r.left), r.right - 1), min(max(py, r.top), r.bottom - 1))


def distance_to_edge(rects: Sequence[Rect], px: float, py: float, side: str) -> float:
    """Distance from the pointer to the `side` edge of the display it is on (pixels/points)."""
    r = containing(rects, px, py) or nearest(rects, px, py)
    if r is None:
        return 0.0
    if side == "right":
        return r.right - 1 - px
    if side == "left":
        return px - r.left
    if side == "bottom":
        return r.bottom - 1 - py
    return py - r.top


class EdgeGuard:
    """Edge transition hysteresis (both directions).

    After a transition, going back is disabled until the pointer moves ARM_DISTANCE away from the edge or
    ARM_TIME seconds pass. Once armed, the pointer has to push PUSH_PX into the edge in total, so small
    finger slips do not switch back by accident.
    """

    ARM_DISTANCE = 24.0
    ARM_TIME = 0.8
    PUSH_PX = 3.0

    def __init__(self, clock):
        self.clock = clock
        self.armed = True
        self.t0 = clock()
        self.push = 0.0

    def reset(self, armed: bool = False) -> None:
        self.armed = armed
        self.t0 = self.clock()
        self.push = 0.0

    def check(self, rects: Sequence[Rect], px: float, py: float, dx: float, dy: float, side: str,
              corner_guard: float = CORNER_GUARD) -> Optional[float]:
        if not self.armed:
            if (self.clock() - self.t0 >= self.ARM_TIME
                    or distance_to_edge(rects, px, py, side) >= self.ARM_DISTANCE):
                self.armed = True
            else:
                return None
        ratio = edge_exit(rects, px, py, dx, dy, side, corner_guard)
        if ratio is None:
            self.push = 0.0
            return None
        self.push += abs(dx) if side in ("left", "right") else abs(dy)
        if self.push >= self.PUSH_PX:
            self.push = 0.0
            return ratio
        return None
