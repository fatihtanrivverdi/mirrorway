"""The Windows tray panel: a borderless window above the tray with the connection status, the screen button and
six tiles (Tk canvas). It follows the Windows light/dark app theme and closes when it loses focus.

The content comes from quick.win_panel(); clicks are reported as keys ("primary", "tile:clipboard",
"settings", "quit", ...).
"""
from __future__ import annotations

import logging
import os
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable, Dict, Optional, Tuple

from .. import quick
from ..i18n import T

log = logging.getLogger("mirrorway.panel")

W, PAD = 368, 16
PRIMARY_H, TILE_H, GAP = 44, 72, 8
Y_CARD = 64

LOGO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "logo.png")

THEMES = {
    "dark": {"bg": "#2B2B2B", "border": "#3F3F3F", "card": "#363636", "card_border": "#3F3F3F", "text": "#FFFFFF",
             "sub": "#CFCFCF", "tile": "#383838", "tile_border": "#474747", "hover": "#424242",
             "accent": "#C4B8FF", "accent_text": "#17112E", "accent_hover": "#D3C9FF", "ok": "#2DD4BF",
             "warn": "#F59E0B", "grey": "#9CA3AF", "footer": "#BDBDBD", "button": "#333333",
             "button_border": "#4A4A4A", "line": "#3F3F3F", "chip": "#B3A6EE"},
    "light": {"bg": "#F9F9F9", "border": "#DADADA", "card": "#FFFFFF", "card_border": "#EBEBEB", "text": "#1A1A1A",
              "sub": "#5F5F5F", "tile": "#FFFFFF", "tile_border": "#E0E0E0", "hover": "#F3F3F3",
              "accent": "#5B3FD6", "accent_text": "#FFFFFF", "accent_hover": "#6A4FE6", "ok": "#0F9F8E",
              "warn": "#D97706", "grey": "#8A8A8A", "footer": "#6B6B6B", "button": "#FFFFFF",
              "button_border": "#D6D6D6", "line": "#E5E5E5", "chip": "#7C64E0"},
}
DOTS = {"connected": "ok", "remote": "ok", "waiting": "grey", "permission": "warn", "paused": "warn"}

# Segoe Fluent Icons (Windows 11) / Segoe MDL2 Assets (Windows 10) code points
GLYPHS = {"settings": "", "primary": "", "clipboard": "", "audio": "", "files": "",
          "reconnect": "", "pair": "", "diag": ""}


def windows_dark() -> bool:
    """Windows' app theme (Settings › Personalisation › Colours); light elsewhere."""
    env = os.environ.get("MIRRORWAY_THEME")
    if env in ("dark", "light"):
        return env == "dark"
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


def work_area(root) -> Tuple[int, int, int, int]:
    """The screen area without the taskbar (left, top, right, bottom)."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            r = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):     # SPI_GETWORKAREA
                return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def _round_corners(top) -> None:
    """Windows 11: rounded corners for the borderless window (Windows 10 keeps square ones)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(top.wm_frame(), 16)
        pref = ctypes.c_int(2)                                    # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


def _rounded(canvas, x1, y1, x2, y2, r, **kw):
    """A rounded rectangle as a smoothed polygon."""
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class TrayPanel:
    def __init__(self, root: tk.Tk, on_click: Callable[[str], None]):
        self.root = root
        self.on_click = on_click
        self.top: Optional[tk.Toplevel] = None
        self.canvas: Optional[tk.Canvas] = None
        self.model: Optional[quick.Panel] = None
        self.hidden_at = 0.0
        self.scale = 1.0
        self.theme = THEMES["light"]
        self._logo = None
        self._hover: Dict[str, Tuple[int, str, str]] = {}      # key -> (shape id, normal fill, hover fill)
        self.clicks = 0
        self.height = 0

    # ---- window ----------------------------------------------------------------------------------------------
    def _create(self) -> None:
        top = tk.Toplevel(self.root)
        top.withdraw()
        top.overrideredirect(True)
        try:
            top.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.scale = max(1.0, float(self.root.winfo_fpixels("1i")) / 96.0)
        self.canvas = tk.Canvas(top, width=self.px(W), height=self.px(400), highlightthickness=0, bd=0)
        self.canvas.pack()
        top.bind("<Escape>", lambda e: self.hide())
        top.bind("<FocusOut>", lambda e: self.root.after(150, self._check_focus))
        self.top = top
        fams = set(tkfont.families(self.root))
        self.icon_family = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), None)
        self.text_family = next((f for f in ("Segoe UI Variable Text", "Segoe UI") if f in fams), "TkDefaultFont")
        try:
            from PIL import Image, ImageTk
            img = Image.open(LOGO).convert("RGBA").resize((self.px(32), self.px(32)), Image.LANCZOS)
            self._logo = ImageTk.PhotoImage(img, master=top)
        except Exception:
            self._logo = None

    def px(self, v: float) -> int:
        return int(round(v * self.scale))

    def font(self, size: float, weight: str = "normal"):
        return (self.text_family, -self.px(size * 96 / 72), weight)

    def is_shown(self) -> bool:
        return self.top is not None and bool(self.top.winfo_viewable())

    def show(self) -> None:
        if self.top is None:
            self._create()
        self.theme = THEMES["dark" if windows_dark() else "light"]
        self.draw()
        left, top_, right, bottom = work_area(self.root)
        x = right - self.px(W) - self.px(12)
        y = bottom - self.height - self.px(12)
        self.top.geometry(f"+{max(left, x)}+{max(top_, y)}")
        self.top.deiconify()
        self.top.lift()
        _round_corners(self.top)
        try:
            self.top.focus_force()
        except tk.TclError:
            pass

    def hide(self) -> None:
        if self.is_shown():
            self.top.withdraw()
            self.hidden_at = time.monotonic()

    def toggle(self) -> None:
        if self.is_shown():
            self.hide()
        elif time.monotonic() - self.hidden_at > 0.35:      # the click on the tray icon took the focus first
            self.show()

    def _check_focus(self) -> None:
        if self.top is None:
            return
        try:
            focused = self.top.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if focused is None:
            self.hide()

    # ---- content ---------------------------------------------------------------------------------------------
    def update(self, model: quick.Panel) -> None:
        self.model = model
        if self.is_shown():
            self.draw()

    def _clickable(self, key: str, shape: int, fill: str, hover: str, tags: str, enabled: bool = True) -> None:
        c = self.canvas
        if not enabled:
            return
        self._hover[key] = (shape, fill, hover)
        c.tag_bind(tags, "<Button-1>", lambda e, k=key: self._clicked(k))
        c.tag_bind(tags, "<Enter>", lambda e, k=key: self._set_hover(k, True))
        c.tag_bind(tags, "<Leave>", lambda e, k=key: self._set_hover(k, False))

    def _set_hover(self, key: str, on: bool) -> None:
        shape, fill, hover = self._hover.get(key, (None, None, None))
        if shape is not None:
            try:
                self.canvas.itemconfigure(shape, fill=hover if on else fill)
                self.canvas.configure(cursor="hand2" if on else "")
            except tk.TclError:
                pass

    def _clicked(self, key: str) -> None:
        self.clicks += 1
        try:
            self.on_click(key)
        except Exception:
            log.exception("panel action %s failed", key)

    def draw(self) -> None:
        m, c, t, p = self.model, self.canvas, self.theme, self.px
        if c is None or m is None:
            return
        c.delete("all")
        self._hover = {}
        c.configure(bg=t["bg"])
        frame = c.create_rectangle(0, 0, p(W) - 1, p(400), outline=t["border"], fill=t["bg"])
        icon = self.icon_family

        # header: logo, name, connection line, settings
        if self._logo is not None:
            c.create_image(p(PAD), p(16), image=self._logo, anchor="nw")
        c.create_text(p(PAD + 44), p(15), text="Mirrorway", anchor="nw", fill=t["text"],
                      font=self.font(11.5, "bold"))
        dot = t[DOTS.get(m.header.state, "grey")]
        c.create_oval(p(PAD + 44), p(40), p(PAD + 52), p(48), fill=dot, outline=dot)
        c.create_text(p(PAD + 58), p(44), text=m.header.subtitle, anchor="w", fill=t["sub"], font=self.font(9),
                      width=p(W - PAD - 58 - 48))
        sx = W - PAD - 32
        shape = _rounded(c, p(sx), p(16), p(sx + 32), p(48), p(6), fill=t["bg"], outline="", tags="settings")
        if icon:
            c.create_text(p(sx + 16), p(32), text=GLYPHS["settings"], fill=t["text"], font=(icon, -p(16)),
                          tags="settings")
        self._clickable("settings", shape, t["bg"], t["hover"], "settings")

        # status card: as tall as its (wrapped) texts
        title = c.create_text(p(PAD + 12), p(Y_CARD + 10), text=m.header.title, anchor="nw", fill=t["text"],
                              font=self.font(10, "bold"), width=p(W - 2 * PAD - 24))
        below = c.bbox(title)[3] + p(4)
        detail = c.create_text(p(PAD + 12), below, text=m.header.detail, anchor="nw", fill=t["sub"],
                               font=self.font(9), width=p(W - 2 * PAD - 24))
        card_bottom = c.bbox(detail)[3] + p(10)
        card = _rounded(c, p(PAD), p(Y_CARD), p(W - PAD), card_bottom, p(6), fill=t["card"], outline=t["card_border"])
        c.tag_lower(card)
        c.tag_lower(frame)
        y_primary = card_bottom / self.scale + 12
        y_tiles = y_primary + PRIMARY_H + 12
        y_footer = y_tiles + 2 * TILE_H + GAP + 14

        # screen button
        pr = m.primary
        fill = t["accent"] if pr.enabled else t["tile"]
        fg = t["accent_text"] if pr.enabled else t["grey"]
        shape = _rounded(c, p(PAD), p(y_primary), p(W - PAD), p(y_primary + PRIMARY_H), p(6), fill=fill,
                         outline="" if pr.enabled else t["tile_border"], tags="primary")
        if icon:
            c.create_text(p(PAD + 24), p(y_primary + 22), text=GLYPHS["primary"], fill=fg, font=(icon, -p(18)),
                          tags="primary")
        c.create_text(p(PAD + 46), p(y_primary + 22), text=pr.title, anchor="w", fill=fg,
                      font=self.font(10.5, "bold"), tags="primary")
        if pr.shortcut:
            c.create_text(p(W - PAD - 14), p(y_primary + 22), text=pr.shortcut, anchor="e", fill=fg,
                          font=self.font(9), tags="primary")
        self._clickable("primary", shape, fill, t["accent_hover"], "primary", pr.enabled)

        # tiles
        tw = (W - 2 * PAD - 2 * GAP) / 3.0
        for i, tile in enumerate(m.tiles):
            x = PAD + (i % 3) * (tw + GAP)
            y = y_tiles + (i // 3) * (TILE_H + GAP)
            tag = "tile:" + tile.key
            on = tile.on and tile.key != "reconnect"
            fill = t["accent"] if on else t["tile"]
            fg = t["accent_text"] if on else (t["text"] if tile.enabled else t["grey"])
            shape = _rounded(c, p(x), p(y), p(x + tw), p(y + TILE_H), p(6), fill=fill,
                             outline=fill if on else t["tile_border"], tags=tag)
            if icon:
                c.create_text(p(x + tw / 2), p(y + 26), text=GLYPHS.get(tile.key, ""), fill=fg,
                              font=(icon, -p(19)), tags=tag)
            label = tile.title
            color = fg
            if tile.key == "reconnect" and tile.on:
                label, color = tile.detail, t["ok"]
            c.create_text(p(x + tw / 2), p(y + 54), text=label, fill=color, font=self.font(9, "bold"), tags=tag,
                          width=p(tw - 8), justify="center")
            self._clickable(tag, shape, fill, t["accent_hover"] if on else t["hover"], tag, tile.enabled)

        # footer
        c.create_line(p(PAD), p(y_footer - 6), p(W - PAD), p(y_footer - 6), fill=t["line"])
        c.create_text(p(PAD), p(y_footer + 16), text=m.footer, anchor="w", fill=t["footer"], font=self.font(8.5))
        qw = 72
        shape = _rounded(c, p(W - PAD - qw), p(y_footer + 2), p(W - PAD), p(y_footer + 30), p(4), fill=t["button"],
                         outline=t["button_border"], tags="quit")
        c.create_text(p(W - PAD - qw / 2), p(y_footer + 16), text=T("Quit"), fill=t["text"], font=self.font(9),
                      tags="quit")
        self._clickable("quit", shape, t["button"], t["hover"], "quit")
        self.height = p(y_footer + 30 + 14)
        c.coords(frame, 0, 0, p(W) - 1, self.height - 1)
        c.configure(height=self.height)

    def click(self, key: str) -> None:
        """For tests: the same as clicking the element."""
        self._clicked(key)
