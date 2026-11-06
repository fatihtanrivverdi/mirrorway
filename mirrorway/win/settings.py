"""The Windows Settings window (Tk): a navigation list on the left and, on the right, cards with switches,
choices, buttons, the Mac's address and the 6-digit pairing code. It follows the Windows light/dark app theme.

The pages come from prefs.win_pages(); a change is reported as app.apply_setting(key, value).
"""
from __future__ import annotations

import logging
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable, Dict, List

from .. import APP_NAME, prefs
from ..i18n import T
from .panel import windows_dark

log = logging.getLogger("mirrorway.settings")

PALETTES = {
    "light": {"bg": "#F3F3F3", "content": "#F9F9F9", "card": "#FFFFFF", "border": "#E5E5E5", "text": "#1A1A1A",
              "sub": "#5C5C5C", "accent": "#5B3FD6", "on_accent": "#FFFFFF", "nav_sel": "#E6E6E6",
              "nav_hover": "#EAEAEA", "button": "#FBFBFB", "button_border": "#D1D1D1", "ok": "#0F7B6C",
              "warn": "#B42318", "knob_off": "#5C5C5C", "field": "#FFFFFF", "disabled": "#A0A0A0"},
    "dark": {"bg": "#202020", "content": "#272727", "card": "#2D2D2D", "border": "#3A3A3A", "text": "#FFFFFF",
             "sub": "#C5C5C5", "accent": "#C4B8FF", "on_accent": "#17112E", "nav_sel": "#2D2D2D",
             "nav_hover": "#292929", "button": "#373737", "button_border": "#454545", "ok": "#2DD4BF",
             "warn": "#F97066", "knob_off": "#C5C5C5", "field": "#1F1F1F", "disabled": "#6B6B6B"},
}


class SettingsWindow:
    W, H, NAV_W = 900, 640, 250

    def __init__(self, app):
        self.app = app
        root = app.root
        self.c = PALETTES["dark" if windows_dark() else "light"]
        self.scale = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
        fams = set(tkfont.families(root))
        self.family = next((f for f in ("Segoe UI Variable Text", "Segoe UI") if f in fams), "TkDefaultFont")
        self.icons = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), None)
        self.pages: List[prefs.Page] = []
        self.page_id = "connection"
        self._sig = None
        self._updaters: Dict[str, Callable[[prefs.Item], None]] = {}
        self._code_boxes: List[tk.Entry] = []
        self._code_lock = False

        top = self.top = tk.Toplevel(root)
        top.title(T("Mirrorway Settings"))
        top.configure(bg=self.c["bg"])
        top.geometry(f"{self.px(self.W)}x{self.px(self.H)}")
        top.minsize(self.px(720), self.px(480))
        top.protocol("WM_DELETE_WINDOW", self.close)
        top.bind("<Escape>", lambda e: self.close())

        # the old window's variables, kept for the pairing flow (and the tests)
        self.host_var = tk.StringVar(master=top, value=app.cfg.get("host") or "")
        self.code_var = tk.StringVar(master=top, value="")
        self.code_var.trace_add("write", lambda *a: self._code_to_boxes())
        self.help_var = tk.StringVar(master=top, value="")
        self.pair_var = tk.StringVar(master=top, value="")
        self.status_var = tk.StringVar(master=top, value=app.status_text)
        self.found_var = tk.StringVar(master=top, value="")

        self.nav = tk.Frame(top, bg=self.c["bg"], width=self.px(self.NAV_W))
        self.nav.pack(side="left", fill="y")
        self.nav.pack_propagate(False)
        body = tk.Frame(top, bg=self.c["content"], highlightthickness=1, highlightbackground=self.c["border"])
        body.pack(side="left", fill="both", expand=True, pady=(self.px(4), 0))
        self.canvas = tk.Canvas(body, bg=self.c["content"], highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=self.c["content"])
        self._inner_id = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self._bar = bar
        self.inner.bind("<Configure>", lambda e: self._fit())
        self.canvas.bind("<Configure>", lambda e: (self.canvas.itemconfigure(self._inner_id, width=e.width),
                                                   self._fit()))
        top.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        if not app.creds.paired():
            self.page_id = "connection"
        self.refresh()
        self._tick()

    def _fit(self) -> None:
        """Scroll region for the page; the scroll bar only when the page is taller than the window."""
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        need = self.inner.winfo_reqheight() > self.canvas.winfo_height() > 1
        if need and not self._bar.winfo_ismapped():
            self._bar.pack(side="right", fill="y", before=self.canvas)
        elif not need and self._bar.winfo_ismapped():
            self._bar.pack_forget()
            self.canvas.yview_moveto(0)

    # ---- helpers ---------------------------------------------------------------------------------------------
    def px(self, v: float) -> int:
        return int(round(v * self.scale))

    def font(self, size: float, weight: str = "normal"):
        return (self.family, -self.px(size * 96 / 72), weight)

    def _label(self, parent, text="", size=10.5, color=None, weight="normal", wrap=0, bg=None):
        lab = tk.Label(parent, text=text, bg=bg or self.c["card"], fg=color or self.c["text"],
                       font=self.font(size, weight), anchor="w", justify="left")
        if wrap:
            lab.configure(wraplength=self.px(wrap))
        return lab

    def _button(self, parent, text, command, primary=False):
        c = self.c
        bg, fg = (c["accent"], c["on_accent"]) if primary else (c["button"], c["text"])
        b = tk.Label(parent, text=text, bg=bg, fg=fg, font=self.font(10, "bold" if primary else "normal"),
                     padx=self.px(14), pady=self.px(5), highlightthickness=1,
                     highlightbackground=c["accent"] if primary else c["button_border"], cursor="hand2")
        b._enabled = True
        b._command = command
        b._fg = fg

        def click(_e):
            if b._enabled:
                b._command()
        b.bind("<Button-1>", click)
        return b

    @staticmethod
    def _enable(widget, on: bool, c) -> None:
        widget._enabled = on
        try:
            widget.configure(fg=widget._fg if on else c["disabled"], cursor="hand2" if on else "")
        except (tk.TclError, AttributeError):
            pass

    def _switch(self, parent, item: prefs.Item):
        c, p = self.c, self.px
        cv = tk.Canvas(parent, width=p(40), height=p(20), bg=c["card"], highlightthickness=0, cursor="hand2")
        state = {"on": bool(item.value), "enabled": item.enabled}

        def draw():
            cv.delete("all")
            on, en = state["on"], state["enabled"]
            fill = (c["accent"] if on else c["card"]) if en else c["border"]
            outline = c["accent"] if on and en else c["knob_off"]
            r = p(10)
            cv.create_oval(1, 1, 2 * r - 1, 2 * r - 1, fill=fill, outline=outline)
            cv.create_oval(p(40) - 2 * r + 1, 1, p(40) - 1, 2 * r - 1, fill=fill, outline=outline)
            cv.create_rectangle(r, 1, p(40) - r, 2 * r - 1, fill=fill, outline="")
            cv.create_line(r, 1, p(40) - r, 1, fill=outline)
            cv.create_line(r, 2 * r - 1, p(40) - r, 2 * r - 1, fill=outline)
            k = p(6)
            cx = p(40) - r if on else r
            cv.create_oval(cx - k, r - k, cx + k, r + k, fill=c["on_accent"] if on else c["knob_off"], outline="")

        def click(_e):
            if state["enabled"]:
                state["on"] = not state["on"]
                draw()
                self._change(item.key, state["on"])
        cv.bind("<Button-1>", click)
        draw()

        def update(it: prefs.Item):
            if (bool(it.value), it.enabled) != (state["on"], state["enabled"]):
                state["on"], state["enabled"] = bool(it.value), it.enabled
                draw()
        return cv, update

    def _popup(self, parent, item: prefs.Item):
        c = self.c
        lab = tk.Label(parent, bg=c["button"], fg=c["text"], font=self.font(10), padx=self.px(10), pady=self.px(4),
                       highlightthickness=1, highlightbackground=c["button_border"], cursor="hand2", anchor="w")
        current = {"item": item}

        def text(it):
            i = prefs.selected(it)
            return (it.options[i][1] if i is not None else "—") + "   ⌄"

        def open_menu(_e):
            it = current["item"]
            menu = tk.Menu(lab, tearoff=0, bg=c["card"], fg=c["text"], activebackground=c["nav_sel"],
                           activeforeground=c["text"], font=self.font(10))
            sel = prefs.selected(it)
            for i, (value, label) in enumerate(it.options):
                menu.add_command(label=("✓  " if i == sel else "     ") + label,
                                 command=lambda v=value: self._change(it.key, v))
            menu.tk_popup(lab.winfo_rootx(), lab.winfo_rooty() + lab.winfo_height())
        lab.bind("<Button-1>", open_menu)

        def update(it: prefs.Item):
            current["item"] = it
            lab.configure(text=text(it))
        update(item)
        return lab, update

    # ---- building ---------------------------------------------------------------------------------------------
    def _build_nav(self) -> None:
        c = self.c
        for w in self.nav.winfo_children():
            w.destroy()
        tk.Frame(self.nav, bg=c["bg"], height=self.px(10)).pack(fill="x")
        for page in self.pages:
            sel = page.id == self.page_id
            row = tk.Frame(self.nav, bg=c["nav_sel"] if sel else c["bg"], cursor="hand2")
            row.pack(fill="x", padx=self.px(10), pady=self.px(2))
            bar = tk.Frame(row, bg=c["accent"] if sel else row["bg"], width=self.px(3), height=self.px(16))
            bar.pack(side="left", padx=(0, self.px(10)))
            if self.icons:
                tk.Label(row, text=page.symbol, font=(self.icons, -self.px(15)), bg=row["bg"],
                         fg=c["text"]).pack(side="left", padx=(0, self.px(12)))
            tk.Label(row, text=page.title, font=self.font(10.5, "bold" if sel else "normal"), bg=row["bg"],
                     fg=c["text"], anchor="w", pady=self.px(8)).pack(side="left", fill="x")
            for w in [row] + list(row.winfo_children()):
                w.bind("<Button-1>", lambda e, pid=page.id: self.show_page(pid))
        from .. import __version__
        tk.Label(self.nav, text=f"Mirrorway {__version__}", font=self.font(9), bg=c["bg"], fg=c["sub"],
                 anchor="w").pack(side="bottom", fill="x", padx=self.px(24), pady=self.px(12))

    def _page(self) -> prefs.Page:
        return next((p for p in self.pages if p.id == self.page_id), self.pages[0])

    def _build_page(self, page: prefs.Page) -> None:
        c, p = self.c, self.px
        for w in self.inner.winfo_children():
            w.destroy()
        self._updaters, self._code_boxes = {}, []
        tk.Label(self.inner, text=page.title, font=self.font(20, "bold"), bg=c["content"], fg=c["text"],
                 anchor="w").pack(fill="x", padx=(p(24), p(36)), pady=(p(14), p(10)))
        for g in page.groups:
            if g.title:
                tk.Label(self.inner, text=g.title, font=self.font(10.5, "bold"), bg=c["content"], fg=c["text"],
                         anchor="w").pack(fill="x", padx=(p(26), p(36)), pady=(p(8), p(4)))
            for it in g.items:
                self._build_item(it)
            if g.note:
                tk.Label(self.inner, text=g.note, font=self.font(9), bg=c["content"], fg=c["sub"], anchor="w",
                         justify="left", wraplength=p(560)).pack(fill="x", padx=(p(26), p(36)), pady=(p(4), p(8)))

    def _build_item(self, it: prefs.Item) -> None:
        c, p = self.c, self.px
        card = tk.Frame(self.inner, bg=c["card"], highlightthickness=1, highlightbackground=c["border"])
        card.pack(fill="x", padx=(p(24), p(36)), pady=p(3))
        row = tk.Frame(card, bg=c["card"])
        row.pack(fill="x", padx=p(16), pady=p(12))
        texts = tk.Frame(row, bg=c["card"])
        texts.pack(side="left", fill="x", expand=True)
        title = self._label(texts, it.title, 10.5)
        title.pack(fill="x")
        detail = self._label(texts, it.detail, 9, c["sub"], wrap=520 if it.kind == "code" else 430)
        if it.detail or it.kind == "code":
            detail.pack(fill="x", pady=(p(2), 0))
        ctl, ctl_update = None, None
        if it.kind == "switch":
            ctl, ctl_update = self._switch(row, it)
        elif it.kind == "popup":
            ctl, ctl_update = self._popup(row, it)
        elif it.kind == "button":
            ctl = self._button(row, it.button, lambda k=it.key: self._change(k, None))

            def ctl_update(i, b=ctl):
                b.configure(text=i.button)
                self._enable(b, i.enabled, c)
        elif it.kind == "entry":
            ctl = tk.Entry(row, textvariable=self.host_var, width=26, font=self.font(10), bg=c["field"],
                           fg=c["text"], insertbackground=c["text"], relief="flat", highlightthickness=1,
                           highlightbackground=c["button_border"], highlightcolor=c["accent"])
            ctl.bind("<Return>", lambda e, k=it.key: self._change(k, self.host_var.get()))
            ctl.bind("<FocusOut>", lambda e, k=it.key: self._change(k, self.host_var.get()))
        elif it.kind == "code":
            self._build_code(card, it)
        if ctl is not None:
            ctl.pack(side="right", padx=(p(12), 0))

        def update(i: prefs.Item, title=title, detail=detail):
            title.configure(text=i.title, fg=c["text"] if i.enabled or i.kind == "button" else c["disabled"])
            detail.configure(text=i.detail, fg=c[i.tone] if i.tone in ("ok", "warn") else c["sub"])
            if ctl_update is not None:
                ctl_update(i)
        self._updaters[it.key] = update
        update(it)

    def _build_code(self, card, it: prefs.Item) -> None:
        c, p = self.c, self.px
        line = tk.Frame(card, bg=c["card"])
        line.pack(fill="x", padx=p(16), pady=(0, p(14)))
        self._code_boxes = []
        for i in range(6):
            if i == 3:
                tk.Frame(line, bg=c["sub"], width=p(10), height=p(2)).pack(side="left", padx=p(6))
            var = tk.StringVar(master=line)
            e = tk.Entry(line, textvariable=var, width=2, justify="center", font=self.font(16, "bold"),
                         bg=c["field"], fg=c["text"], insertbackground=c["text"], relief="flat", highlightthickness=1,
                         highlightbackground=c["button_border"], highlightcolor=c["accent"])
            e.pack(side="left", padx=p(3), ipady=p(6))
            e._var = var
            var.trace_add("write", lambda *a, k=i: self._box_changed(k))
            e.bind("<BackSpace>", lambda ev, k=i: self._box_back(k))
            self._code_boxes.append(e)
        self._button(line, it.button, self.save, primary=True).pack(side="right")
        self._code_to_boxes()

    # ---- the pairing code -----------------------------------------------------------------------------------
    def _box_changed(self, k: int) -> None:
        if self._code_lock:
            return
        box = self._code_boxes[k]
        raw = "".join(ch for ch in box._var.get() if ch.isdigit())
        if len(raw) > 1:                                    # pasted: spread over the boxes
            self.code_var.set(raw[:6])
            return
        self._code_lock = True
        try:
            box._var.set(raw)
        finally:
            self._code_lock = False
        self._sync_code_var()
        if raw and k < 5:
            self._code_boxes[k + 1].focus_set()
        elif raw and k == 5 and len(self.code_var.get()) == 6:
            self.save()

    def _box_back(self, k: int) -> None:
        if not self._code_boxes[k]._var.get() and k > 0:
            self._code_boxes[k - 1].focus_set()

    def _sync_code_var(self) -> None:
        self._code_lock = True
        try:
            self.code_var.set("".join(b._var.get() for b in self._code_boxes))
        finally:
            self._code_lock = False

    def _code_to_boxes(self) -> None:
        if self._code_lock or not self._code_boxes:
            return
        digits = "".join(ch for ch in self.code_var.get() if ch.isdigit())
        if self.code_var.get().strip() and len(digits) != len(self.code_var.get().replace(" ", "").replace("-", "")):
            return                                          # an old long code: not shown in the boxes
        self._code_lock = True
        try:
            for i, b in enumerate(self._code_boxes):
                b._var.set(digits[i] if i < len(digits) else "")
        finally:
            self._code_lock = False

    # ---- content ---------------------------------------------------------------------------------------------
    def show_page(self, page_id: str) -> None:
        self.page_id = page_id
        self._sig = None
        self.refresh()
        self.canvas.yview_moveto(0)

    def refresh(self) -> None:
        if self.top is None:
            return
        app = self.app
        self.pages = app.settings_pages()
        sig = (self.page_id, tuple((p.id, p.title) for p in self.pages), self._page().signature())
        if sig != self._sig:
            self._sig = sig
            self._build_nav()
            self._build_page(self._page())
        else:
            for it in self._page().items():
                fn = self._updaters.get(it.key)
                if fn is not None:
                    fn(it)
        creds = app.creds
        code_item = next((it for p in self.pages for it in p.items() if it.key == "action:pair"), None)
        self.help_var.set(code_item.detail if code_item else "")
        if creds.paired():
            from ..devkeys import fingerprint
            self.pair_var.set(T("Paired Mac: {name} (fingerprint {id})").format(name=creds.mac_name(),
                                                                                 id=fingerprint(creds.mac_pub())))
        else:
            self.pair_var.set("")
        self.status_var.set(app.status_text)

    def refresh_pairing(self) -> None:
        self.refresh()

    def _tick(self) -> None:
        if self.top is None:
            return
        try:
            self.refresh()
        except Exception:
            log.exception("Could not refresh the Settings window")
        self.top.after(1000, self._tick)

    def _change(self, key: str, value) -> None:
        if key == "action:unpair":
            self.unpair()
        elif key == "action:pair":
            self.save()
        else:
            self.app.apply_setting(key, value)
        self.refresh()

    # ---- actions ---------------------------------------------------------------------------------------------
    def save(self) -> None:
        self.app.apply_settings(self.host_var.get(), self.code_var.get())
        self.code_var.set("")
        self.status_var.set(T("Connecting…"))

    def unpair(self) -> None:
        from tkinter import messagebox
        if messagebox.askyesno(APP_NAME, T("Remove this PC's pairing with the Mac? Connecting again will need "
                                           "a new code from the Mac."), parent=self.top):
            self.app.unpair()
            self.refresh()

    def close(self) -> None:
        top, self.top = self.top, None
        if top is not None:
            top.destroy()
        self.app.settings_win = None

