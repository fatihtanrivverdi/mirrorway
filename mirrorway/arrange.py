"""Screen arrangement (3.5): where Windows' monitors sit next to the Mac's displays, and the pointer crossing that
follows from it (platform independent, tested on its own).

Without an arrangement (the default, as before 3.5) the whole outer edge of the Mac on the chosen side maps onto the
whole outer edge of Windows, in proportion. With one (dragged in the Mac's Settings › Arrangement), Windows'
monitors are a group placed against that edge: both sides are measured in the same units (Mac points; Windows
pixels divided by Windows' display scale, so both are "logical" sizes), the pointer crosses straight, and only the
part where the edges face each other leads across; the rest of each edge is a wall, as between displays on a Mac.

The edges are the ones geometry.edge_exit and entry_point use: the display that forms the outer edge on that side
(or all of them together when several do). An arrangement is the side plus the offset between the start of the
Mac's edge and the start of Windows' edge, in Mac points.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .geometry import OPPOSITE, Rect, edge_group, union

SIDES = ("left", "right", "top", "bottom")
MIN_OVERLAP = 48.0          # Mac points the edges must share at least (a narrower opening would be hard to hit)


@dataclass(frozen=True)
class Layout:
    side: str               # where Windows is, seen from the Mac
    offset: float           # start of Windows' edge minus start of the Mac's edge, in Mac points

    def to_cfg(self) -> dict:
        return {"side": self.side, "offset": round(float(self.offset), 1)}


def from_cfg(value, side: str) -> Optional[Layout]:
    """The saved arrangement, if it belongs to the current side (choosing another side elsewhere drops it)."""
    if not isinstance(value, dict) or value.get("side") != side or side not in SIDES:
        return None
    try:
        return Layout(side, float(value.get("offset")))
    except (TypeError, ValueError):
        return None


def rects_of(values) -> List[Rect]:
    out = []
    for v in values or ():
        try:
            x, y, w, h = (float(n) for n in v)
        except (TypeError, ValueError):
            continue
        if w > 0 and h > 0:
            out.append(Rect(x, y, w, h))
    return out


def edge_extent(rects: Sequence[Rect], side: str) -> Tuple[float, float]:
    """(start, length) along the edge on `side`: the display forming that outer edge, or all of them together."""
    u, cands = edge_group(rects, side)
    r = cands[0] if len(cands) == 1 else u
    if side in ("left", "right"):
        return r.y, r.h
    return r.x, r.w


def edge_scale(win_rects: Sequence[Rect], scales: Optional[Sequence[float]], side: str) -> float:
    """Windows' display scale (1.0 = 100 %) of the monitors on its edge facing the Mac (`side` = Windows' edge)."""
    if not scales or len(scales) != len(win_rects):
        return 1.0
    _u, cands = edge_group(win_rects, side)
    vals = [float(scales[win_rects.index(c)]) for c in cands if c in win_rects]
    vals = [v for v in vals if 0.5 <= v <= 5.0]
    return sum(vals) / len(vals) if vals else 1.0


class Geometry:
    """The Mac's displays (points) and Windows' monitors (pixels, with their scale) for one side."""

    def __init__(self, mac: Sequence[Rect], win: Sequence[Rect], scales: Optional[Sequence[float]], side: str):
        self.mac, self.win, self.side = list(mac), list(win), side
        self.win_side = OPPOSITE[side]
        self.scale = edge_scale(self.win, scales, self.win_side)
        self.m0, self.ml = edge_extent(self.mac, side)
        w0, wl = edge_extent(self.win, self.win_side)
        self.w0, self.wl = w0, wl                   # Windows pixels
        self.wl_pt = wl / self.scale                # the same length in Mac points

    # -- the pointer --------------------------------------------------------------------------------------------
    def mac_to_win(self, layout: Layout, ratio: float) -> Optional[float]:
        """A Mac exit at `ratio` along its edge -> the ratio along Windows' edge; None where no Windows faces it."""
        t = ratio * max(self.ml - 1, 1) - layout.offset
        span = max(self.wl_pt - 1 / self.scale, 1e-9)
        q = t / span
        if q < -1e-9 or q > 1 + 1e-9:
            return None
        return min(max(q, 0.0), 1.0)

    def win_to_mac(self, layout: Layout, ratio: float) -> float:
        """A Windows exit at `ratio` along its edge -> the ratio along the Mac's edge (clamped)."""
        m = layout.offset + ratio * max(self.wl_pt - 1 / self.scale, 1e-9)
        return min(max(m / max(self.ml - 1, 1), 0.0), 1.0)

    def win_span(self, layout: Layout) -> Optional[Tuple[float, float]]:
        """The part of Windows' edge (ratios) that faces the Mac; None if none does."""
        span = max(self.wl_pt - 1 / self.scale, 1e-9)
        lo = (0.0 - layout.offset) / span
        hi = (max(self.ml - 1, 1) - layout.offset) / span
        lo, hi = max(lo, 0.0), min(hi, 1.0)
        return (lo, hi) if lo <= hi else None

    # -- the picture ---------------------------------------------------------------------------------------------
    def win_group(self, layout: Optional[Layout]) -> List[Rect]:
        """Windows' monitors in Mac points, placed against the Mac's edge (centred when there is no arrangement)."""
        offset = layout.offset if layout is not None else self.centred_offset()
        mu, wu = union(self.mac), union(self.win)
        s = self.scale
        logical = [Rect((r.x - wu.x) / s, (r.y - wu.y) / s, r.w / s, r.h / s) for r in self.win]
        gw, gh = wu.w / s, wu.h / s
        lead = (self.w0 - (wu.y if self.side in ("left", "right") else wu.x)) / s   # group start -> its edge start
        start = self.m0 + offset - lead
        if self.side == "right":
            ox, oy = mu.right, start
        elif self.side == "left":
            ox, oy = mu.x - gw, start
        elif self.side == "bottom":
            ox, oy = start, mu.bottom
        else:
            ox, oy = start, mu.y - gh
        return [Rect(ox + r.x, oy + r.y, r.w, r.h) for r in logical]

    def centred_offset(self) -> float:
        return (self.ml - self.wl_pt) / 2.0

    def clamp(self, offset: float) -> float:
        """Keep at least MIN_OVERLAP of the edges facing each other."""
        need = min(MIN_OVERLAP, self.ml, self.wl_pt)
        return min(max(offset, need - self.wl_pt), self.ml - need)


def snap(mac: Sequence[Rect], win: Sequence[Rect], scales: Optional[Sequence[float]],
         group: Rect) -> Layout:
    """Where a dragged Windows group (its union, in Mac points) lands: the Mac's side nearest to its centre, and the
    offset along that side."""
    mu = union(mac)
    cx, cy = group.x + group.w / 2, group.y + group.h / 2
    dx = (cx - (mu.x + mu.w / 2)) / max(mu.w / 2 + group.w / 2, 1)
    dy = (cy - (mu.y + mu.h / 2)) / max(mu.h / 2 + group.h / 2, 1)
    if abs(dx) >= abs(dy):
        side = "right" if dx >= 0 else "left"
    else:
        side = "bottom" if dy >= 0 else "top"
    g = Geometry(mac, win, scales, side)
    wu = union(win)
    lead = (g.w0 - (wu.y if side in ("left", "right") else wu.x)) / g.scale
    start = (group.y if side in ("left", "right") else group.x) + lead
    return Layout(side, g.clamp(start - g.m0))


PAD = 18                    # the picture's margin in the Settings window (view points)


class Picture:
    """The geometry of the picture (pure Python): Mac rects and Windows' group in Mac points, the fit into the
    view, and dragging. The view below only draws it and forwards the mouse."""

    def __init__(self, model: dict, width: float, height: float):
        self.model = model
        self.width, self.height = width, height
        self.mac = rects_of(model.get("mac"))
        self.win_px = rects_of(model.get("win"))
        self.scales = list(model.get("scales") or [])
        self.side = model.get("side") or "right"
        off = model.get("offset")
        self.layout = Layout(self.side, float(off)) if off is not None else None
        self.geo = Geometry(self.mac, self.win_px, self.scales, self.side)
        self.win = self.geo.win_group(self.layout)
        self.drag: Optional[Tuple[float, float]] = None      # the group's move so far, in Mac points
        self.fit = self._fit(self.mac + self.win)

    def _fit(self, rects: List[Rect]) -> Tuple[float, float, float]:
        """(scale, x0, y0): Mac points -> view points, everything centred with room for dragging."""
        u = union(rects)
        g = union(self.win)
        u = Rect(u.x - g.w * 0.35, u.y - g.h * 0.35, u.w + g.w * 0.7, u.h + g.h * 0.7)
        k = min((self.width - 2 * PAD) / max(u.w, 1), (self.height - 2 * PAD) / max(u.h, 1))
        x0 = (self.width - u.w * k) / 2 - u.x * k
        y0 = (self.height - u.h * k) / 2 - u.y * k
        return k, x0, y0

    def to_view(self, r: Rect) -> Rect:
        k, x0, y0 = self.fit
        return Rect(x0 + r.x * k, y0 + r.y * k, r.w * k, r.h * k)

    def win_now(self) -> List[Rect]:
        if self.drag is None:
            return self.win
        dx, dy = self.drag
        return [Rect(r.x + dx, r.y + dy, r.w, r.h) for r in self.win]

    def hit(self, vx: float, vy: float) -> bool:
        return any(self.to_view(r).contains(vx, vy) for r in self.win)

    def begin(self) -> None:
        self.drag = (0.0, 0.0)

    def move(self, vdx: float, vdy: float) -> None:
        k = self.fit[0]
        self.drag = (vdx / k, vdy / k)

    def end(self) -> Optional[Layout]:
        """The arrangement where the group was dropped (snapped to the nearest side)."""
        if self.drag is None:
            return None
        group = union(self.win_now())
        self.drag = None
        lay = snap(self.mac, self.win_px, self.scales, group)
        self.side, self.layout = lay.side, lay
        self.geo = Geometry(self.mac, self.win_px, self.scales, lay.side)
        self.win = self.geo.win_group(lay)
        return lay

    def shared_edge(self) -> Optional[Tuple[float, float, float, float]]:
        """The part of the Mac's edge where the pointer crosses (view points: x1, y1, x2, y2)."""
        if self.drag is not None:
            return None
        if self.layout is None:
            lo, hi = 0.0, 1.0
        else:
            span = self.geo.win_span(self.layout)
            if span is None:
                return None
            lo, hi = (self.geo.win_to_mac(self.layout, span[0]), self.geo.win_to_mac(self.layout, span[1]))
        g, mu = self.geo, union(self.mac)
        a, b = g.m0 + lo * g.ml, g.m0 + hi * g.ml
        edge = {"right": mu.right, "left": mu.x, "bottom": mu.bottom, "top": mu.y}[self.side]
        k, x0, y0 = self.fit
        if self.side in ("left", "right"):
            return (x0 + edge * k, y0 + a * k, x0 + edge * k, y0 + b * k)
        return (x0 + a * k, y0 + edge * k, x0 + b * k, y0 + edge * k)
