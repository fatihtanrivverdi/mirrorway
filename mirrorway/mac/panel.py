"""The menu bar panel: a popover under the Mirrorway icon with the connection status, the main button, six quick
tiles and three rows (AppKit, laid out by hand so it opens instantly).

The content comes from quick.mac_panel(); this file only draws it. Clicks are reported to the app as keys
("primary", "tile:screen", "row:network", "footer:quit", ...). The panel follows the system's light/dark
appearance; the accent is Mirrorway's violet.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Dict, List, Optional, Tuple

from AppKit import (NSAppearance, NSAppearanceNameAqua, NSAppearanceNameDarkAqua, NSApplication, NSBezierPath,
                    NSBitmapImageFileTypePNG, NSButton, NSColor, NSEventModifierFlagCommand, NSFont,
                    NSFontWeightMedium, NSFontWeightRegular, NSFontWeightSemibold, NSImage,
                    NSImageScaleProportionallyUpOrDown, NSImageView, NSLineBreakByTruncatingTail, NSMenu,
                    NSMenuItem, NSPopover, NSPopoverBehaviorTransient, NSTextAlignmentCenter, NSTextAlignmentRight,
                    NSTextField, NSView, NSViewController)
from Foundation import NSInsetRect, NSMakePoint, NSMakeRect, NSMakeSize, NSObject

from .. import quick
from ..i18n import T

log = logging.getLogger("mirrorway.panel")

NSRectEdgeMinY = 1

W, PAD = 364, 12
INNER = W - 2 * PAD
TILE_W, TILE_H, GAP = (INNER - 16) / 3.0, 86, 8
ROW_H = 38
Y_HEADER, Y_CARD, Y_PRIMARY, Y_TILES = 12, 52, 122, 176
Y_ROWS = Y_TILES + 2 * TILE_H + GAP + 10
Y_FOOTER = Y_ROWS + 3 * ROW_H + 10
H = Y_FOOTER + 30 + 12

LOGO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "logo.png")

SYMBOLS = {"screen": ("display",), "second": ("display.2", "rectangle.on.rectangle"),
           "audio": ("speaker.wave.2",), "clipboard": ("doc.on.clipboard",), "files": ("folder",),
           "wake": ("power",), "primary": ("arrow.left.arrow.right",), "settings": ("slider.horizontal.3",),
           "pair": ("plus",), "network": ("house",), "disconnect": ("link",), "chevron": ("chevron.right",)}


def _rgb(hex_: str, alpha: float = 1.0):
    v = int(hex_.lstrip("#"), 16)
    return NSColor.colorWithSRGBRed_green_blue_alpha_(((v >> 16) & 255) / 255.0, ((v >> 8) & 255) / 255.0,
                                                       (v & 255) / 255.0, alpha)


def _white(alpha: float):
    return NSColor.colorWithWhite_alpha_(1.0, alpha)


def _black(alpha: float):
    return NSColor.colorWithWhite_alpha_(0.0, alpha)


# box styles: (fill light, fill dark, stroke light, stroke dark, corner radius); None = not drawn
STYLES = {
    "panel": (_rgb("#F5F5F7"), _rgb("#2A2A2D"), None, None, 0),
    "card": (_white(1.0), _white(0.06), _black(0.06), _white(0.08), 10),
    "tile_off": (_white(1.0), _white(0.06), _black(0.08), _white(0.10), 10),
    "tile_on": (_rgb("#5B3FD6"), _rgb("#6A4FE6"), _rgb("#5B3FD6"), _rgb("#6A4FE6"), 10),
    "primary": (_rgb("#5B3FD6"), _rgb("#6A4FE6"), None, None, 10),
    "chip": (_white(0.2), _white(0.2), None, None, 5),
    "button": (_white(1.0), _white(0.08), _black(0.10), _white(0.12), 7),
    "line": (_black(0.06), _white(0.08), None, None, 0),
    "dot": (NSColor.systemTealColor(), NSColor.systemTealColor(), None, None, 4),
}
DOT_COLORS = {"connected": NSColor.systemTealColor, "remote": NSColor.systemTealColor,
              "waiting": NSColor.systemGrayColor, "permission": NSColor.systemOrangeColor,
              "paused": NSColor.systemOrangeColor}
TONES = {"ok": NSColor.systemTealColor, "warn": NSColor.systemOrangeColor}


def is_dark(view) -> bool:
    try:
        name = view.effectiveAppearance().bestMatchFromAppearancesWithNames_(
            [NSAppearanceNameAqua, NSAppearanceNameDarkAqua])
        return name == NSAppearanceNameDarkAqua
    except Exception:
        return False


class MirrorwayPanelBox(NSView):
    """A rounded box drawn in drawRect (so it also appears in rendered images); style from STYLES."""

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        style = self.mw_style
        if style is None:
            return
        if isinstance(style, str):
            style = STYLES[style]
        dark = is_dark(self)
        fill = style[1] if dark else style[0]
        stroke = style[3] if dark else style[2]
        radius = style[4]
        r = NSInsetRect(self.bounds(), 0.5, 0.5) if stroke is not None else self.bounds()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, radius, radius)
        if fill is not None:
            fill.setFill()
            path.fill()
        if stroke is not None:
            stroke.setStroke()
            path.setLineWidth_(1.0)
            path.stroke()


class MirrorwayPanelTarget(NSObject):
    """Receives button clicks, menu choices and the popover's close notice (mw_* are Python callables).
    Errors are logged here: an exception must not travel back into AppKit."""

    def clicked_(self, sender):
        _call(self.mw_click, int(sender.tag()))

    def changed_(self, sender):
        _call(self.mw_change, sender)

    def windowWillClose_(self, _notification):
        _call(self.mw_closed)

    def popoverDidClose_(self, _notification):
        _call(self.mw_closed)

    def menuDidClose_(self, _menu):
        _call(self.mw_menu_closed)


def _call(fn, *args) -> None:
    if fn is None:
        return
    try:
        fn(*args)
    except Exception:
        log.exception("panel callback failed")


def make_target(click=None, closed=None, menu_closed=None, change=None):
    t = MirrorwayPanelTarget.alloc().init()
    t.mw_click, t.mw_closed, t.mw_menu_closed, t.mw_change = click, closed, menu_closed, change
    return t


def _box(parent, frame, style):
    b = MirrorwayPanelBox.alloc().initWithFrame_(frame)
    b.mw_style = style
    parent.addSubview_(b)
    return b


def _label(parent, frame, size, weight=NSFontWeightRegular, color=None, align=None):
    lab = NSTextField.labelWithString_("")
    lab.setFrame_(frame)
    lab.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
    lab.setTextColor_(color if color is not None else NSColor.labelColor())
    lab.setLineBreakMode_(NSLineBreakByTruncatingTail)
    lab.setMaximumNumberOfLines_(1)
    if align is not None:
        lab.setAlignment_(align)
    parent.addSubview_(lab)
    return lab


def _symbol(key: str, size: float, weight=NSFontWeightMedium):
    for name in SYMBOLS.get(key, (key,)):
        try:
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        except Exception:                                    # macOS 10.15: no SF Symbols in AppKit
            return None
        if img is None:
            continue
        try:
            from AppKit import NSImageSymbolConfiguration
            img = img.imageWithSymbolConfiguration_(
                NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, weight)) or img
        except Exception:
            pass
        return img
    return None


def _icon(parent, frame, key, size, tint=None):
    view = NSImageView.alloc().initWithFrame_(frame)
    view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
    img = _symbol(key, size)
    if img is not None:
        view.setImage_(img)
    if tint is not None:
        try:
            view.setContentTintColor_(tint)
        except Exception:
            pass
    parent.addSubview_(view)
    return view


class QuickPanel:
    """The popover and its views. on_click(key) is called on the main thread."""

    def __init__(self, on_click: Callable[[str], None]):
        self.on_click = on_click
        self._keys: List[str] = []
        self._buttons: Dict[str, object] = {}
        self._target = make_target(click=self._clicked, closed=self._did_close)
        self._menu_actions: List[Callable[[], None]] = []
        self._menu_target = make_target(click=self._menu_chosen)
        self.closed_at = 0.0
        self.popover = None
        self._status_button = None
        self.root = MirrorwayPanelBox.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        self.root.mw_style = None
        self._build()

    # ---- building -------------------------------------------------------------------------------------------
    def _button(self, parent, frame, key, key_equivalent: Optional[Tuple[str, int]] = None):
        """A transparent button over a drawn area (added last, so it receives the clicks)."""
        btn = NSButton.alloc().initWithFrame_(frame)
        btn.setBordered_(False)
        btn.setTransparent_(True)
        btn.setTitle_("")
        btn.setTarget_(self._target)
        btn.setAction_("clicked:")
        btn.setTag_(len(self._keys))
        btn.setFocusRingType_(1)                              # NSFocusRingTypeNone
        btn.setRefusesFirstResponder_(True)
        if key_equivalent:
            btn.setKeyEquivalent_(key_equivalent[0])
            btn.setKeyEquivalentModifierMask_(key_equivalent[1])
        self._keys.append(key)
        self._buttons[key] = btn
        parent.addSubview_(btn)
        return btn

    def _build(self) -> None:
        root = self.root
        accent = _rgb("#5B3FD6")
        # header: logo, name, settings
        logo = NSImageView.alloc().initWithFrame_(NSMakeRect(PAD, Y_HEADER + 1, 28, 28))
        logo.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        if os.path.exists(LOGO):
            img = NSImage.alloc().initWithContentsOfFile_(LOGO)
            if img is not None:
                logo.setImage_(img)
        root.addSubview_(logo)
        name = _label(root, NSMakeRect(PAD + 38, Y_HEADER + 5, 200, 20), 15, NSFontWeightSemibold)
        name.setStringValue_("Mirrorway")
        _icon(root, NSMakeRect(W - PAD - 26, Y_HEADER + 6, 18, 18), "settings", 15,
              NSColor.secondaryLabelColor())
        self._button(root, NSMakeRect(W - PAD - 30, Y_HEADER, 30, 30), "settings")

        # status card
        _box(root, NSMakeRect(PAD, Y_CARD, INNER, 60), "card")
        self.dot = _box(root, NSMakeRect(PAD + 12, Y_CARD + 17, 8, 8), "dot")
        self.h_title = _label(root, NSMakeRect(PAD + 28, Y_CARD + 11, INNER - 40, 18), 13, NSFontWeightSemibold)
        self.h_detail = _label(root, NSMakeRect(PAD + 28, Y_CARD + 31, INNER - 40, 16), 12,
                               color=NSColor.secondaryLabelColor())

        # main button
        self.primary = _box(root, NSMakeRect(PAD, Y_PRIMARY, INNER, 44), "primary")
        _icon(self.primary, NSMakeRect(14, 12, 20, 20), "primary", 15, _white(1.0))
        self.p_title = _label(self.primary, NSMakeRect(44, 13, INNER - 120, 19), 14, NSFontWeightSemibold,
                              color=_white(1.0))
        chip = _box(self.primary, NSMakeRect(INNER - 14 - 50, 12, 50, 20), "chip")
        self.p_chip = _label(chip, NSMakeRect(0, 2, 50, 16), 12, NSFontWeightMedium, color=_white(1.0),
                             align=NSTextAlignmentCenter)
        self._button(self.primary, NSMakeRect(0, 0, INNER, 44), "primary")

        # tiles
        self.tiles = {}
        for i, key in enumerate(("screen", "second", "audio", "clipboard", "files", "wake")):
            x = PAD + (i % 3) * (TILE_W + GAP)
            y = Y_TILES + (i // 3) * (TILE_H + GAP)
            box = _box(root, NSMakeRect(x, y, TILE_W, TILE_H), "tile_off")
            icon = _icon(box, NSMakeRect(10, 10, 20, 20), key, 15, accent)
            keys = _label(box, NSMakeRect(TILE_W - 52, 12, 42, 14), 10.5, NSFontWeightMedium,
                          color=NSColor.tertiaryLabelColor(), align=NSTextAlignmentRight)
            title = _label(box, NSMakeRect(10, 44, TILE_W - 12, 17), 12.5, NSFontWeightSemibold)
            sub = _label(box, NSMakeRect(10, 62, TILE_W - 12, 15), 11, color=NSColor.secondaryLabelColor())
            self._button(box, NSMakeRect(0, 0, TILE_W, TILE_H), "tile:" + key)
            self.tiles[key] = (box, icon, title, sub, keys)

        # rows
        _box(root, NSMakeRect(PAD, Y_ROWS, INNER, 3 * ROW_H), "card")
        self.rows = {}
        for i, key in enumerate(("pair", "network", "disconnect")):
            y = Y_ROWS + i * ROW_H
            if i:
                _box(root, NSMakeRect(PAD + 1, y, INNER - 2, 1), "line")
            _icon(root, NSMakeRect(PAD + 12, y + 11, 16, 16), key, 13, NSColor.secondaryLabelColor())
            title = _label(root, NSMakeRect(PAD + 38, y + 10, INNER - 180, 18), 13)
            right = PAD + INNER - (32 if key == "network" else 12)
            detail = _label(root, NSMakeRect(right - 130, y + 11, 130, 16), 12, NSFontWeightSemibold,
                            color=NSColor.secondaryLabelColor(), align=NSTextAlignmentRight)
            if key == "network":
                _icon(root, NSMakeRect(PAD + INNER - 26, y + 12, 14, 14), "chevron", 11,
                      NSColor.tertiaryLabelColor())
            self._button(root, NSMakeRect(PAD, y, INNER, ROW_H), "row:" + key)
            self.rows[key] = (title, detail)

        # footer: Settings…, version (or the update), Quit
        sw = 116
        box = _box(root, NSMakeRect(PAD, Y_FOOTER, sw, 30), "button")
        lab = _label(box, NSMakeRect(4, 7, sw - 8, 16), 12.5, align=NSTextAlignmentCenter)
        lab.setStringValue_(T("Settings…") + "  ⌘,")
        self._button(box, NSMakeRect(0, 0, sw, 30), "footer:settings", (",", NSEventModifierFlagCommand))
        qw = 74
        box = _box(root, NSMakeRect(W - PAD - qw, Y_FOOTER, qw, 30), "button")
        lab = _label(box, NSMakeRect(4, 7, qw - 8, 16), 12.5, align=NSTextAlignmentCenter)
        lab.setStringValue_(T("Quit"))
        self._button(box, NSMakeRect(0, 0, qw, 30), "footer:quit", ("q", NSEventModifierFlagCommand))
        mid_x, mid_w = PAD + sw + 6, INNER - sw - qw - 12
        self.footer = _label(root, NSMakeRect(mid_x, Y_FOOTER + 8, mid_w, 15), 11,
                             color=NSColor.secondaryLabelColor(), align=NSTextAlignmentCenter)
        self._button(root, NSMakeRect(mid_x, Y_FOOTER, mid_w, 30), "footer:update")

    # ---- content --------------------------------------------------------------------------------------------
    def update(self, m: quick.Panel) -> None:
        h = m.header
        self.dot.mw_style = (DOT_COLORS[h.state](), DOT_COLORS[h.state](), None, None, 4)
        self.dot.setNeedsDisplay_(True)
        self.h_title.setStringValue_(h.title)
        self.h_detail.setStringValue_(h.detail)

        p = m.primary
        self.p_title.setStringValue_(p.title)
        self.p_chip.setStringValue_(p.shortcut)
        self.primary.setAlphaValue_(1.0 if p.enabled else 0.45)
        self._buttons["primary"].setEnabled_(p.enabled)

        # the violet of the off tiles' icons: lighter in dark appearance (contrast)
        accent = _rgb("#A898FF") if is_dark(self.root) else _rgb("#5B3FD6")
        for t in m.tiles:
            box, icon, title, sub, keys = self.tiles[t.key]
            style = "tile_on" if t.on else "tile_off"
            if box.mw_style != style:
                box.mw_style = style
                box.setNeedsDisplay_(True)
            title.setStringValue_(t.title)
            sub.setStringValue_(t.detail)
            keys.setStringValue_(t.shortcut)
            title.setTextColor_(_white(1.0) if t.on else NSColor.labelColor())
            sub.setTextColor_(_white(0.88) if t.on else NSColor.secondaryLabelColor())
            keys.setTextColor_(_white(0.7) if t.on else NSColor.tertiaryLabelColor())
            try:
                icon.setContentTintColor_(_white(1.0) if t.on else accent)
            except Exception:
                pass
            box.setAlphaValue_(1.0 if t.enabled else 0.45)
            self._buttons["tile:" + t.key].setEnabled_(t.enabled)

        for r in m.rows:
            title, detail = self.rows[r.key]
            title.setStringValue_(r.title)
            detail.setStringValue_(r.detail)
            tone = TONES.get(r.tone)
            detail.setTextColor_(tone() if tone else NSColor.secondaryLabelColor())
            title.setTextColor_(NSColor.labelColor() if r.enabled else NSColor.tertiaryLabelColor())
            self._buttons["row:" + r.key].setEnabled_(r.enabled)

        self.footer.setStringValue_(m.footer)
        self.footer.setTextColor_(accent if m.update else NSColor.secondaryLabelColor())
        self._buttons["footer:update"].setEnabled_(bool(m.update))

    # ---- popover --------------------------------------------------------------------------------------------
    def is_shown(self) -> bool:
        return self.popover is not None and bool(self.popover.isShown())

    def show(self, status_button) -> None:
        if self.popover is None:
            pop = NSPopover.alloc().init()
            pop.setBehavior_(NSPopoverBehaviorTransient)
            vc = NSViewController.alloc().init()
            vc.setView_(self.root)
            pop.setContentViewController_(vc)
            pop.setContentSize_(NSMakeSize(W, H))
            pop.setDelegate_(self._target)
            self.popover, self._vc = pop, vc
        self._status_button = status_button
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.popover.showRelativeToRect_ofView_preferredEdge_(status_button.bounds(), status_button, NSRectEdgeMinY)
        try:
            status_button.highlight_(True)
        except Exception:
            pass

    def close(self) -> None:
        if self.is_shown():
            self.popover.performClose_(None)

    def toggle(self, status_button) -> None:
        """Clicking the icon while the panel is open closes it (the popover has already closed itself on the
        mouse press when it was transient, so a click right after closing does not reopen it)."""
        if self.is_shown():
            self.close()
        elif time.monotonic() - self.closed_at > 0.3:
            self.show(status_button)

    def _did_close(self) -> None:
        self.closed_at = time.monotonic()
        b = self._status_button
        if b is not None:
            try:
                b.highlight_(False)
            except Exception:
                pass

    def _clicked(self, tag: int) -> None:
        if 0 <= tag < len(self._keys):
            try:
                self.on_click(self._keys[tag])
            except Exception:
                log.exception("panel action %s failed", self._keys[tag])

    # ---- small menus (Network row) ------------------------------------------------------------------------
    def popup(self, key: str, items: List[tuple]) -> None:
        """A menu under a row. items: (title, callback or None, checked) or None for a separator."""
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self._menu_actions = []
        for it in items:
            if it is None:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            title, fn, checked = it
            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "clicked:" if fn else None, "")
            if fn is not None:
                mi.setTarget_(self._menu_target)
                mi.setTag_(len(self._menu_actions))
                self._menu_actions.append(fn)
            mi.setEnabled_(fn is not None)
            mi.setState_(1 if checked else 0)
            menu.addItem_(mi)
        btn = self._buttons.get(key)
        if btn is None:
            return
        f = btn.frame()
        where = NSMakePoint(f.origin.x + 30, f.origin.y + f.size.height)
        menu.popUpMenuPositioningItem_atLocation_inView_(None, where, btn.superview())

    def _menu_chosen(self, tag: int) -> None:
        if 0 <= tag < len(self._menu_actions):
            try:
                self._menu_actions[tag]()
            except Exception:
                log.exception("panel menu action failed")

    # ---- image (CI and diagnostics) --------------------------------------------------------------------------
    def render_png(self, dark: bool = False, model: Optional[quick.Panel] = None) -> bytes:
        """The panel (filled from model, if given) as a PNG on its own background; needs no window or screen."""
        view = self.root
        view.mw_style = "panel"
        view.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua if dark
                                                          else NSAppearanceNameAqua))
        if model is not None:
            self.update(model)
        try:
            rect = view.bounds()
            rep = view.bitmapImageRepForCachingDisplayInRect_(rect)
            view.cacheDisplayInRect_toBitmapImageRep_(rect, rep)
            data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
            return bytes(data)
        finally:
            view.mw_style = None
            view.setAppearance_(None)

