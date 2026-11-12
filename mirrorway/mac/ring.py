"""The switch indicator on the Mac: when control comes back from Windows, a ring grows and fades where the pointer
appears (the same shape as on Windows, mirrorway/ring.py).

A small borderless window above everything (also full-screen apps and all Spaces) that ignores the mouse; two
circular layers (a white edge and the violet ring) animated by Core Animation, so the main thread only starts it.
Must be used on the main thread.
"""
from __future__ import annotations

from AppKit import NSBackingStoreBuffered, NSColor, NSScreen, NSScreenSaverWindowLevel, NSWindow
from Foundation import NSMakeRect, NSObject, NSValue
from Quartz import (CAAnimationGroup, CABasicAnimation, CALayer, CAMediaTimingFunction, CATransaction,
                    CGColorCreateGenericRGB)

from .. import ring as R

NSWindowStyleMaskBorderless = 0
# all Spaces, does not move with Spaces, not in the window cycle, above full-screen apps
COLLECTION = (1 << 0) | (1 << 4) | (1 << 6) | (1 << 8)


def cocoa_point(x: float, y: float, primary_height: float):
    """A point in global display coordinates (top-left origin, as the router uses) in Cocoa's (bottom-left)."""
    return x, primary_height - y


class RingWindow:
    def __init__(self):
        self.window = None
        self.layers = []
        self.shown = 0
        self.size = R.box(1.0)

    def _make(self) -> None:
        s = self.size
        w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, s, s), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
        w.setOpaque_(False)
        w.setBackgroundColor_(NSColor.clearColor())
        w.setHasShadow_(False)
        w.setIgnoresMouseEvents_(True)
        w.setLevel_(NSScreenSaverWindowLevel)
        w.setCollectionBehavior_(COLLECTION)
        w.setReleasedWhenClosed_(False)
        root = CALayer.layer()
        view = w.contentView()
        view.setLayer_(root)                          # a layer-hosting view: the layers are ours
        view.setWantsLayer_(True)
        scale = w.backingScaleFactor() or 2.0
        halo = CGColorCreateGenericRGB(*(c / 255.0 for c in R.HALO_COLOR), R.HALO_ALPHA)
        violet = CGColorCreateGenericRGB(*(c / 255.0 for c in R.COLOR), 1.0)
        self.layers = []
        for color, edge in ((halo, R.HALO), (violet, 0.0)):
            lay = CALayer.layer()
            lay.setContentsScale_(scale)
            lay.setBorderColor_(color)
            lay.setPosition_((s / 2.0, s / 2.0))
            lay.setOpacity_(0.0)
            root.addSublayer_(lay)
            self.layers.append((lay, edge))
        self.window = w

    def show(self, x: float, y: float) -> None:
        """The pointer appeared at (x, y), global display coordinates with the origin top left."""
        if self.window is None:
            self._make()
        s = self.size
        screens = NSScreen.screens()
        cx, cy = cocoa_point(x, y, screens[0].frame().size.height if screens else 0.0)
        self.window.setFrame_display_(NSMakeRect(cx - s / 2.0, cy - s / 2.0, s, s), False)
        r0, w0, _ = R.at(0.0)
        r1, w1, _ = R.at(R.DURATION)
        grow = CAMediaTimingFunction.functionWithControlPoints____(0.215, 0.61, 0.355, 1.0)     # ease-out cubic
        fade = CAMediaTimingFunction.functionWithControlPoints____(0.55, 0.0, 1.0, 0.45)        # ~1 - t²
        for lay, edge in self.layers:
            a_side, a_border = R.layer_shape(r0, w0, edge)
            b_side, b_border = R.layer_shape(r1, w1, edge)
            CATransaction.begin()
            CATransaction.setDisableActions_(True)         # the end state, without implicit animations
            lay.removeAllAnimations()
            lay.setBounds_(((0, 0), (b_side, b_side)))
            lay.setCornerRadius_(b_side / 2.0)
            lay.setBorderWidth_(b_border)
            lay.setOpacity_(0.0)
            CATransaction.commit()
            anims = []
            for key, a, b, timing in (
                    ("bounds", NSValue.valueWithRect_(NSMakeRect(0, 0, a_side, a_side)),
                     NSValue.valueWithRect_(NSMakeRect(0, 0, b_side, b_side)), grow),
                    ("cornerRadius", a_side / 2.0, b_side / 2.0, grow),
                    ("borderWidth", a_border, b_border, grow),
                    ("opacity", 1.0, 0.0, fade)):
                an = CABasicAnimation.animationWithKeyPath_(key)
                an.setFromValue_(a)
                an.setToValue_(b)
                an.setTimingFunction_(timing)
                anims.append(an)
            group = CAAnimationGroup.animation()
            group.setAnimations_(anims)
            group.setDuration_(R.DURATION)
            lay.addAnimation_forKey_(group, "ring")
        self.window.orderFrontRegardless()
        NSObject.cancelPreviousPerformRequestsWithTarget_selector_object_(self.window, "orderOut:", None)
        self.window.performSelector_withObject_afterDelay_("orderOut:", None, R.DURATION + 0.05)
        self.shown += 1

    def visible(self) -> bool:
        return self.window is not None and bool(self.window.isVisible())
