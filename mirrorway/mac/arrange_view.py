"""The arrangement picture in the Mac's Settings (3.5): the Mac's displays stay put, Windows' monitors are dragged as
one group and snap to the nearest side of the Mac (mirrorway.arrange decides where). Drawn in drawRect, so it
also appears in rendered images (CI)."""
from __future__ import annotations

import logging
from typing import Callable, Optional

import objc
from AppKit import (NSBezierPath, NSColor, NSFont, NSFontAttributeName, NSFontWeightSemibold,
                    NSForegroundColorAttributeName, NSString, NSView)
from Foundation import NSMakeRect

from .. import arrange as AR
from ..arrange import Picture
from ..geometry import Rect

log = logging.getLogger("mirrorway.settings")

ACCENT = (123 / 255.0, 97 / 255.0, 255 / 255.0)         # the switch ring's colour


def _accent(alpha: float):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(ACCENT[0], ACCENT[1], ACCENT[2], alpha)


class MirrorwayArrangeView(NSView):

    def initWithFrame_(self, frame):
        self = objc.super(MirrorwayArrangeView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.mw_picture = None
        self.mw_on_change = None
        self.mw_start = None
        return self

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, _event):
        return True

    # -- from Python --
    @objc.python_method
    def mw_set_model(self, model: Optional[dict]) -> None:
        if self.mw_start is not None:                        # not while the user is dragging
            return
        b = self.bounds()
        self.mw_picture = Picture(model, b.size.width, b.size.height) if model else None
        self.setNeedsDisplay_(True)

    @objc.python_method
    def mw_drop(self, vdx: float, vdy: float) -> Optional[AR.Layout]:
        """Drag the group by (vdx, vdy) view points and drop it (the mouse handlers use this; so does CI)."""
        p = self.mw_picture
        if p is None:
            return None
        p.begin()
        p.move(vdx, vdy)
        lay = p.end()
        self.setNeedsDisplay_(True)
        cb = self.mw_on_change
        if lay is not None and cb is not None:
            try:
                cb("layout", lay.to_cfg())
            except Exception:
                log.exception("arrangement change")
        return lay

    # -- mouse --
    @objc.python_method
    def _point(self, event):
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        return float(p.x), float(p.y)

    def mouseDown_(self, event):
        p = self.mw_picture
        x, y = self._point(event)
        if p is not None and p.hit(x, y):
            self.mw_start = (x, y)
            p.begin()

    def mouseDragged_(self, event):
        p, s = self.mw_picture, self.mw_start
        if p is None or s is None:
            return
        x, y = self._point(event)
        p.move(x - s[0], y - s[1])
        self.setNeedsDisplay_(True)

    def mouseUp_(self, event):
        p, s = self.mw_picture, self.mw_start
        self.mw_start = None
        if p is None or s is None:
            return
        x, y = self._point(event)
        p.drag = None
        self.mw_drop(x - s[0], y - s[1])

    # -- drawing --
    def drawRect_(self, _rect):
        p = self.mw_picture
        if p is None:
            return
        try:
            self._draw(p)
        except Exception:
            log.exception("arrangement drawing")

    @objc.python_method
    def _box(self, r: Rect, fill, stroke, radius=5.0, width=1.0):
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(r.x + 0.5, r.y + 0.5, max(r.w - 1, 1), max(r.h - 1, 1)), radius, radius)
        fill.setFill()
        path.fill()
        stroke.setStroke()
        path.setLineWidth_(width)
        path.stroke()

    @objc.python_method
    def _text(self, r: Rect, text: str, color, size=12.0):
        attrs = {NSFontAttributeName: NSFont.systemFontOfSize_weight_(size, NSFontWeightSemibold),
                 NSForegroundColorAttributeName: color}
        s = NSString.stringWithString_(text)
        sz = s.sizeWithAttributes_(attrs)
        if sz.width > r.w - 6 or sz.height > r.h - 4:
            return
        s.drawAtPoint_withAttributes_((r.x + (r.w - sz.width) / 2, r.y + (r.h - sz.height) / 2), attrs)

    @objc.python_method
    def _draw(self, p: Picture) -> None:
        many = len(p.mac) > 1
        for i, r in enumerate(p.mac):
            v = p.to_view(r)
            self._box(v, NSColor.quaternaryLabelColor(), NSColor.secondaryLabelColor())
            self._text(v, "Mac" if not many else f"Mac {i + 1}", NSColor.labelColor())
        edge = p.shared_edge()
        dragging = p.drag is not None
        wins = p.win_now()
        for i, r in enumerate(wins):
            v = p.to_view(r)
            self._box(v, _accent(0.16 if dragging else 0.22), _accent(0.9), width=1.5 if dragging else 1.0)
            self._text(v, "Windows" if len(wins) == 1 else f"Windows {i + 1}", NSColor.labelColor())
        if edge is not None:
            line = NSBezierPath.bezierPath()
            line.moveToPoint_((edge[0], edge[1]))
            line.lineToPoint_((edge[2], edge[3]))
            line.setLineWidth_(4.0)
            line.setLineCapStyle_(1)                          # round
            _accent(1.0).setStroke()
            line.stroke()


def make(parent, frame, on_change: Callable[[str, object], None]):
    v = MirrorwayArrangeView.alloc().initWithFrame_(frame)
    v.mw_on_change = on_change
    parent.addSubview_(v)
    return v

