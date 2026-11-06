"""Second screen: one of the Windows monitors ("M") shows the Mac's virtual display ("VD").

Platform-independent calculations (testable):
  - which Windows monitor becomes the second screen and its name in the menu,
  - which side of the Mac's main display the virtual display goes on,
  - physical transitions: if an edge of VD touches a monitor next to M on Windows, the pointer moves to Windows
    there; when the pointer on Windows tries to enter M, it returns to the Mac at the same point of VD.

Coordinates: global display coordinates on the Mac (points, top left origin, y down), virtual desktop pixels on Windows.
VD has the same pixel size as M, so a fraction (0..1) along an edge is the same point on both sides.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .geometry import OPPOSITE, Rect
from .i18n import T

SIDES = ("left", "right", "top", "bottom")
MIN_SIZE, MAX_SIZE = 640, 7680
FEATURE = "extend"


def as_rects(monitors: Sequence[Sequence[float]]) -> List[Rect]:
    return [Rect(float(m[0]), float(m[1]), float(m[2]), float(m[3])) for m in monitors or []]


def is_primary(r: Rect) -> bool:
    return r.contains(0, 0)


def choose_monitor(monitors: Sequence[Sequence[float]], want=None) -> Optional[int]:
    """Index of the Windows monitor that becomes the second screen.

    want: the monitor the user chose [x, y, w, h] (if gone, the first monitor of the same size); with "auto"/None,
    the largest non-primary monitor when there are several, otherwise the only one.
    """
    rects = as_rects(monitors)
    if not rects:
        return None
    if isinstance(want, (list, tuple)) and len(want) == 4:
        w = Rect(*(float(v) for v in want))
        for i, r in enumerate(rects):
            if r == w:
                return i
        for i, r in enumerate(rects):
            if (r.w, r.h) == (w.w, w.h):
                return i
        return None
    others = [i for i, r in enumerate(rects) if not is_primary(r)]
    if others:
        return max(others, key=lambda i: (rects[i].w * rects[i].h, -i))
    return 0


def _relation(r: Rect, primary: Optional[Rect]) -> str:
    if primary is None or r == primary:
        return T("main")
    if r.right <= primary.left:
        return T("left")
    if r.left >= primary.right:
        return T("right")
    if r.bottom <= primary.top:
        return T("above")
    if r.top >= primary.bottom:
        return T("below")
    return ""


def monitor_label(monitors: Sequence[Sequence[float]], i: int) -> str:
    rects = as_rects(monitors)
    r = rects[i]
    primary = next((m for m in rects if is_primary(m)), None)
    rel = _relation(r, primary)
    return T("Monitor {n} — {w}×{h}").format(n=i + 1, w=int(r.w), h=int(r.h)) + (f" ({rel})" if rel else "")


def valid_size(w: float, h: float) -> bool:
    return MIN_SIZE <= w <= MAX_SIZE and MIN_SIZE // 2 <= h <= MAX_SIZE


def place_beside(main: Rect, w: float, h: float, side: str) -> Tuple[int, int]:
    """Top left corner of VD: on the `side` of the main display, centred along that edge."""
    if side == "left":
        return int(main.left - w), int(round(main.top + (main.h - h) / 2))
    if side == "right":
        return int(main.right), int(round(main.top + (main.h - h) / 2))
    if side == "top":
        return int(round(main.left + (main.w - w) / 2)), int(main.top - h)
    return int(round(main.left + (main.w - w) / 2)), int(main.bottom)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return min(a1, b1) - max(a0, b0)


def touching(a: Rect, b: Rect, side: str) -> bool:
    """Does b touch the `side` edge of a (do they share a stretch along it)?"""
    if side == "right":
        return abs(b.left - a.right) < 1 and _overlap(a.top, a.bottom, b.top, b.bottom) > 0
    if side == "left":
        return abs(b.right - a.left) < 1 and _overlap(a.top, a.bottom, b.top, b.bottom) > 0
    if side == "bottom":
        return abs(b.top - a.bottom) < 1 and _overlap(a.left, a.right, b.left, b.right) > 0
    if side == "top":
        return abs(b.bottom - a.top) < 1 and _overlap(a.left, a.right, b.left, b.right) > 0
    return False


def neighbor_sides(monitors: Sequence[Sequence[float]], m: int) -> Dict[str, List[int]]:
    """Which Windows monitors sit on which edge of M."""
    rects = as_rects(monitors)
    out: Dict[str, List[int]] = {}
    for s in SIDES:
        ns = [i for i, r in enumerate(rects) if i != m and touching(rects[m], r, s)]
        if ns:
            out[s] = ns
    return out


def _along(r: Rect, side: str, ratio: float) -> Tuple[float, float]:
    ratio = min(max(ratio, 0.0), 1.0)
    if side in ("left", "right"):
        return (r.left if side == "left" else r.right), r.top + ratio * (r.h - 1)
    return r.left + ratio * (r.w - 1), (r.top if side == "top" else r.bottom)


def win_entry(monitors: Sequence[Sequence[float]], m: int, side: str, ratio: float,
              inset: float = 2) -> Optional[Tuple[float, float]]:
    """Where the pointer goes on Windows when it leaves VD through `side` (on M's neighbour on that side)."""
    rects = as_rects(monitors)
    ns = [rects[i] for i in neighbor_sides(monitors, m).get(side, [])]
    if not ns:
        return None
    x, y = _along(rects[m], side, ratio)
    if side in ("left", "right"):
        n = min(ns, key=lambda r: 0 if r.top <= y < r.bottom else min(abs(y - r.top), abs(y - r.bottom)))
        y = min(max(y, n.top), n.bottom - 1)
        x = n.right - 1 - inset if side == "left" else n.left + inset
    else:
        n = min(ns, key=lambda r: 0 if r.left <= x < r.right else min(abs(x - r.left), abs(x - r.right)))
        x = min(max(x, n.left), n.right - 1)
        y = n.bottom - 1 - inset if side == "top" else n.top + inset
    return x, y


def m_fraction(m_rect: Rect, x: float, y: float) -> Tuple[float, float]:
    """Position of a point inside (or on the edge of) M relative to M (0..1, 0..1)."""
    fx = (x - m_rect.left) / max(m_rect.w - 1, 1)
    fy = (y - m_rect.top) / max(m_rect.h - 1, 1)
    return min(max(fx, 0.0), 1.0), min(max(fy, 0.0), 1.0)


def vd_point(vd: Rect, fx: float, fy: float, inset: float = 3) -> Tuple[float, float]:
    """The point of VD at fraction (fx, fy); `inset` pixels inside the edge."""
    x = vd.left + min(max(fx, 0.0), 1.0) * (vd.w - 1)
    y = vd.top + min(max(fy, 0.0), 1.0) * (vd.h - 1)
    return (min(max(x, vd.left + inset), vd.right - 1 - inset),
            min(max(y, vd.top + inset), vd.bottom - 1 - inset))


def toward(rects: Sequence[Rect], target: Rect) -> List[str]:
    """Which edges of the monitors in `rects` touch `target` (M): the directions that lead into M on Windows."""
    out = []
    for s in SIDES:
        if any(touching(r, target, s) for r in rects):
            out.append(s)
    return out


def crosses_into(target: Rect, x: float, y: float, side: str, step: float = 2) -> Optional[Tuple[float, float]]:
    """Does moving a few pixels from (x, y) towards `side` land inside `target`? If so, the entry point on
    that edge of target."""
    dx = {"left": -step, "right": step}.get(side, 0)
    dy = {"top": -step, "bottom": step}.get(side, 0)
    px, py = x + dx, y + dy
    if not target.contains(px, py):
        return None
    # the point on the entered edge of target (so the fraction comes out as exactly 0 or 1)
    if side == "left":
        px = target.right - 1
    elif side == "right":
        px = target.left
    elif side == "top":
        py = target.bottom - 1
    elif side == "bottom":
        py = target.top
    return px, py


def opposite(side: str) -> str:
    return OPPOSITE.get(side, side)


def pick_display(bounds: Sequence[Sequence[float]], rect: Sequence[float]) -> Optional[int]:
    """For the viewer window on Windows: index of the display that matches the second-screen monitor (rect).

    Exact bounds first, then the same size, then the display that contains the centre of rect.
    """
    bs = as_rects(bounds)
    t = Rect(*(float(v) for v in rect))
    for i, b in enumerate(bs):
        if b == t:
            return i
    same = [i for i, b in enumerate(bs) if (b.w, b.h) == (t.w, t.h)]
    if len(same) == 1:
        return same[0]
    cx, cy = t.x + t.w / 2, t.y + t.h / 2
    for i, b in enumerate(bs):
        if b.contains(cx, cy):
            return i
    return same[0] if same else None
