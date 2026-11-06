"""The Mac Settings window: a sidebar of pages and, on the right, groups of rows with switches, pop-up menus and
buttons (AppKit, laid out by hand). The pages come from prefs.mac_pages(); a change is reported as
on_change(key, value) and the app refreshes the window afterwards.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from AppKit import (NSAppearance, NSAppearanceNameAqua, NSAppearanceNameDarkAqua, NSApplication,
                    NSBitmapImageFileTypePNG, NSButton, NSColor, NSFontWeightBold, NSFontWeightRegular,
                    NSFontWeightSemibold, NSPopUpButton, NSScrollView, NSSegmentedControl, NSTextAlignmentCenter,
                    NSTextField, NSWindow)
from Foundation import NSMakeRect

from .. import prefs
from ..i18n import T
from .panel import (TONES, MirrorwayPanelBox, _box, _icon, _label, _rgb, _white, make_target)

log = logging.getLogger("mirrorway.settings")

WIN_W, WIN_H = 800, 600
SIDE_W = 220
CONTENT_W = WIN_W - SIDE_W
MARGIN = 28
CARD_W = CONTENT_W - 2 * MARGIN
ROW_H, ROW_H_DETAIL = 44, 54

# window style: titled, closable, miniaturizable
STYLE = 1 | 2 | 4


def _wrapping_label(parent, x, y, width, text, size=11.5):
    lab = NSTextField.wrappingLabelWithString_(text)
    lab.setFont_(_font(size))
    lab.setTextColor_(NSColor.secondaryLabelColor())
    h = lab.cell().cellSizeForBounds_(NSMakeRect(0, 0, width, 1000)).height
    lab.setFrame_(NSMakeRect(x, y, width, h))
    parent.addSubview_(lab)
    return h


def _font(size, weight=NSFontWeightRegular):
    from AppKit import NSFont
    return NSFont.systemFontOfSize_weight_(size, weight)


class SettingsWindow:
    def __init__(self, on_change: Callable[[str, object], None]):
        self.on_change = on_change
        self.pages: List[prefs.Page] = []
        self.page_id = "general"
        self._page_sig = None
        self._side_sig = None
        self._controls: List[tuple] = []         # (key, kind, options, control)
        self._rows: Dict[str, tuple] = {}         # key -> (title label, detail label or None, control or None)
        self._shown: Dict[int, object] = {}       # control index -> last value set
        self._target = make_target(click=self._side_clicked, change=self._changed, closed=self._closed)
        self.visible = False

        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIN_W, WIN_H), STYLE, 2, False)
        win.setTitle_(T("Mirrorway Settings"))
        win.setReleasedWhenClosed_(False)
        win.setDelegate_(self._target)
        self.window = win
        root = MirrorwayPanelBox.alloc().initWithFrame_(NSMakeRect(0, 0, WIN_W, WIN_H))
        root.mw_style = None
        win.setContentView_(root)
        self.root = root

        self.sidebar = MirrorwayPanelBox.alloc().initWithFrame_(NSMakeRect(0, 0, SIDE_W, WIN_H))
        self.sidebar.mw_style = ((_rgb("#EBEBEE"), _rgb("#232326"), None, None, 0))
        root.addSubview_(self.sidebar)
        _box(root, NSMakeRect(SIDE_W, 0, 1, WIN_H), "line")

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(SIDE_W + 1, 0, CONTENT_W - 1, WIN_H))
        scroll.setHasVerticalScroller_(True)
        scroll.setDrawsBackground_(False)
        scroll.setAutohidesScrollers_(True)
        root.addSubview_(scroll)
        self.scroll = scroll
        self.doc = None
        # Command-W closes the window (the app has no menu bar of its own)
        close = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 0))
        close.setTransparent_(True)
        close.setKeyEquivalent_("w")
        close.setKeyEquivalentModifierMask_(1 << 20)               # Command
        close.setTarget_(win)
        close.setAction_("performClose:")
        root.addSubview_(close)

    # ---- sidebar ---------------------------------------------------------------------------------------------
    def _build_sidebar(self) -> None:
        for v in list(self.sidebar.subviews()):
            v.removeFromSuperview()
        self._side_items = []
        y = 16
        for i, page in enumerate(self.pages):
            sel = page.id == self.page_id
            box = _box(self.sidebar, NSMakeRect(10, y, SIDE_W - 20, 32), "primary" if sel else None)
            square = _box(box, NSMakeRect(8, 6, 20, 20), (_rgb(page.color), _rgb(page.color), None, None, 5))
            if sel:
                square.mw_style = (_white(0.22), _white(0.22), None, None, 5)
            _icon(square, NSMakeRect(3, 3, 14, 14), page.symbol, 10, _white(1.0))
            lab = _label(box, NSMakeRect(36, 7, SIDE_W - 60, 18), 13,
                         NSFontWeightSemibold if sel else NSFontWeightRegular,
                         color=_white(1.0) if sel else NSColor.labelColor())
            lab.setStringValue_(page.title)
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, SIDE_W - 20, 32))
            btn.setBordered_(False)
            btn.setTransparent_(True)
            btn.setTitle_("")
            btn.setTag_(i)
            btn.setTarget_(self._target)
            btn.setAction_("clicked:")
            btn.setRefusesFirstResponder_(True)
            box.addSubview_(btn)
            y += 34
        from .. import __version__
        ver = _label(self.sidebar, NSMakeRect(18, WIN_H - 30, SIDE_W - 36, 15), 11,
                     color=NSColor.secondaryLabelColor())
        ver.setStringValue_(f"Mirrorway {__version__}")

    def _side_clicked(self, tag: int) -> None:
        if 0 <= tag < len(self.pages):
            self.show_page(self.pages[tag].id)

    def show_page(self, page_id: str) -> None:
        if page_id not in [p.id for p in self.pages]:
            return
        self.page_id = page_id
        self._side_sig = None
        self._page_sig = None
        self.update(self.pages)
        self.scroll.contentView().scrollToPoint_((0, 0))
        self.scroll.reflectScrolledClipView_(self.scroll.contentView())

    # ---- page ----------------------------------------------------------------------------------------------
    def _page(self) -> prefs.Page:
        for p in self.pages:
            if p.id == self.page_id:
                return p
        return self.pages[0]

    def _control(self, parent, item: prefs.Item, x_right: float, y_mid: float):
        """The control at the right of a row (None for text rows)."""
        idx = len(self._controls)
        ctl = None
        if item.kind == "switch":
            try:
                from AppKit import NSSwitch
                ctl = NSSwitch.alloc().initWithFrame_(NSMakeRect(0, 0, 38, 22))
            except ImportError:                                    # macOS 10.14
                ctl = NSButton.checkboxWithTitle_target_action_("", None, None)
            w, h = 38, 22
        elif item.kind == "segmented":
            ctl = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
                [lab for _v, lab in item.options], 0, None, None)
            ctl.sizeToFit()
            w, h = ctl.frame().size.width, 24
        elif item.kind == "popup":
            ctl = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 26), False)
            for _v, lab in item.options:
                ctl.addItemWithTitle_(lab)
            ctl.sizeToFit()
            w = min(max(ctl.frame().size.width, 90), 260)
            h = 26
        elif item.kind == "button":
            ctl = NSButton.buttonWithTitle_target_action_(item.button or "…", None, None)
            ctl.sizeToFit()
            w, h = max(ctl.frame().size.width, 84), 28
        elif item.kind == "keys":
            chip = _box(parent, NSMakeRect(x_right - 84, y_mid - 13, 84, 26), "button")
            lab = _label(chip, NSMakeRect(0, 5, 84, 16), 12.5, NSFontWeightSemibold, align=NSTextAlignmentCenter)
            lab.setStringValue_(str(item.value or ""))
            return None
        else:
            return None
        ctl.setFrame_(NSMakeRect(x_right - w, y_mid - h / 2.0, w, h))
        ctl.setTarget_(self._target)
        ctl.setAction_("changed:")
        ctl.setTag_(idx)
        parent.addSubview_(ctl)
        self._controls.append((item.key, item.kind, list(item.options), ctl))
        return ctl

    def _build_page(self, page: prefs.Page) -> None:
        self._controls, self._rows, self._shown = [], {}, {}
        doc = MirrorwayPanelBox.alloc().initWithFrame_(NSMakeRect(0, 0, CONTENT_W - 1, 100))
        doc.mw_style = None
        title = _label(doc, NSMakeRect(MARGIN, 22, CARD_W, 28), 20, NSFontWeightBold)
        title.setStringValue_(page.title)
        y = 64
        for g in page.groups:
            if g.title:
                lab = _label(doc, NSMakeRect(MARGIN + 2, y, CARD_W, 18), 13, NSFontWeightSemibold,
                             color=NSColor.secondaryLabelColor())
                lab.setStringValue_(g.title)
                y += 24
            heights = [ROW_H_DETAIL if it.detail else ROW_H for it in g.items]
            card = _box(doc, NSMakeRect(MARGIN, y, CARD_W, sum(heights)), "card")
            ry = 0
            for i, (it, h) in enumerate(zip(g.items, heights)):
                if i:
                    _box(card, NSMakeRect(14, ry, CARD_W - 14, 1), "line")
                ctl = self._control(card, it, CARD_W - 14, ry + h / 2.0)
                right = 14 + (ctl.frame().size.width + 16 if ctl is not None else (100 if it.kind == "keys" else 0))
                t = _label(card, NSMakeRect(14, ry + (9 if it.detail else 13), CARD_W - 14 - right, 18), 13)
                d = None
                if it.detail:
                    d = _label(card, NSMakeRect(14, ry + 29, CARD_W - 14 - right, 16), 11.5,
                               color=NSColor.secondaryLabelColor())
                self._rows[it.key] = (t, d, ctl)
                ry += h
            y += sum(heights)
            if g.note:
                y += 8 + _wrapping_label(doc, MARGIN + 2, y + 8, CARD_W - 4, g.note)
            y += 22
        doc.setFrame_(NSMakeRect(0, 0, CONTENT_W - 1, max(y, WIN_H)))
        self.scroll.setDocumentView_(doc)
        self.doc = doc

    # ---- values ----------------------------------------------------------------------------------------------
    def update(self, pages: List[prefs.Page]) -> None:
        """New content: rebuilds what changed in shape, updates values in place (open pop-ups stay open)."""
        self.pages = pages
        side_sig = (self.page_id, tuple((p.id, p.title) for p in pages))
        if side_sig != self._side_sig:
            self._side_sig = side_sig
            self._build_sidebar()
        page = self._page()
        sig = page.signature()
        if sig != self._page_sig:
            self._page_sig = sig
            self._build_page(page)
        for it in page.items():
            row = self._rows.get(it.key)
            if row is None:
                continue
            t, d, ctl = row
            t.setStringValue_(it.title)
            dim = not it.enabled and it.kind != "button"
            t.setTextColor_(NSColor.tertiaryLabelColor() if dim else NSColor.labelColor())
            if d is not None:
                d.setStringValue_(it.detail)
                tone = TONES.get(it.tone)
                d.setTextColor_(tone() if tone else NSColor.secondaryLabelColor())
            if ctl is None:
                continue
            idx = int(ctl.tag())
            ctl.setEnabled_(bool(it.enabled))
            value = it.value
            if it.kind == "button":
                value = it.button
            if self._shown.get(idx, object()) == value:
                continue
            self._shown[idx] = value
            if it.kind == "switch":
                ctl.setState_(1 if it.value else 0)
            elif it.kind == "segmented":
                i = prefs.selected(it)
                ctl.setSelectedSegment_(-1 if i is None else i)
            elif it.kind == "popup":
                i = prefs.selected(it)
                if i is not None:
                    ctl.selectItemAtIndex_(i)
            elif it.kind == "button":
                ctl.setTitle_(it.button)

    def _changed(self, sender) -> None:
        idx = int(sender.tag())
        if not 0 <= idx < len(self._controls):
            return
        key, kind, options, _ctl = self._controls[idx]
        if kind == "switch":
            value = bool(sender.state())
        elif kind == "segmented":
            i = int(sender.selectedSegment())
            value = options[i][0] if 0 <= i < len(options) else None
        elif kind == "popup":
            i = int(sender.indexOfSelectedItem())
            value = options[i][0] if 0 <= i < len(options) else None
        else:
            value = None
        self._shown.pop(idx, None)
        self.on_change(key, value)

    # ---- window -----------------------------------------------------------------------------------------------
    def show(self, page_id: Optional[str] = None) -> None:
        if page_id:
            self.show_page(page_id)
        if not self.visible:
            self.window.center()
        self.visible = True
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def close(self) -> None:
        self.window.performClose_(None)

    def _closed(self) -> None:
        self.visible = False

    def render_png(self, page_id: str, dark: bool = False) -> bytes:
        """One page as a PNG (CI); does not show the window."""
        self.show_page(page_id)
        view = self.root
        view.mw_style = "panel"
        view.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua if dark
                                                          else NSAppearanceNameAqua))
        try:
            self.update(self.pages)
            rect = view.bounds()
            rep = view.bitmapImageRepForCachingDisplayInRect_(rect)
            view.cacheDisplayInRect_toBitmapImageRep_(rect, rep)
            return bytes(rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}))
        finally:
            view.mw_style = None
            view.setAppearance_(None)

