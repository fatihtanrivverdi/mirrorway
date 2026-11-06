"""The Tk window that shows the Mac's screen."""
from __future__ import annotations

import threading
import time
import tkinter as tk
from typing import Callable, Optional

from PIL import ImageTk

from ..i18n import T


class Viewer:
    def __init__(self, root: tk.Tk, title: str, on_close: Callable[[], None],
                 rtt_fn: Optional[Callable[[], Optional[float]]] = None,
                 info_fn: Optional[Callable[[], dict]] = None, prefs=None):
        self.root = root
        self.on_close = on_close
        self.rtt_fn = rtt_fn
        self.info_fn = info_fn or (lambda: {})
        self.prefs = prefs
        self._acc = {"frames": 0, "bytes": 0, "enc": 0.0, "dec": 0.0, "disp": 0.0}
        self._stats = {"fps": 0.0, "kbps": 0.0, "enc_ms": 0.0, "decode_ms": 0.0, "display_ms": 0.0,
                       "latency_ms": 0.0}
        self.top = tk.Toplevel(root)
        self.top.title(title)
        self.top.configure(bg="black")
        sw, sh = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
        w, h = int(sw * 0.7), int(sh * 0.7)
        self.top.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")
        self.label = tk.Label(self.top, bg="black", fg="#bbbbbb", font=("Segoe UI", 14),
                              text=T("Connecting…"), compound="center", bd=0, highlightthickness=0,
                              padx=0, pady=0)
        self.label.pack(fill="both", expand=True)
        # Measurements in the top right (the title bar is hidden in full screen); F9 shows/hides them
        self.overlay = tk.Label(self.top, bg="#16161a", fg="#e6e8ee", font=("Consolas", 11),
                                justify="left", anchor="nw", padx=8, pady=6, bd=0)
        self._ov_local = {}
        self._size = (w, h)
        self._lock = threading.Lock()
        self._pending = None                 # pending full frame (image, measurement)
        self._pending_patches = []           # pending partial updates
        self._expected_size = None           # size of the last full frame sent
        self._overflow = False               # partial updates piled up -> a full frame is needed
        self._status: Optional[str] = None
        self._photo = None
        self._photo_size = None
        self.patch_batches = 0               # partial frames updated in place (for diagnostics)
        self._has_frame = False
        self._fullscreen = False
        self._frames = 0
        self._fps_t = time.monotonic()
        self._base_title = title
        self.closed = False

        self.label.bind("<Configure>", self._on_resize)
        self.top.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.top.bind("<Double-Button-1>", lambda e: self.toggle_fullscreen())
        self.top.bind("<Escape>", lambda e: self.set_fullscreen(False))
        self.top.bind("<F9>", lambda e: self.toggle_overlay())
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.after(15, self._pump)
        self.top.lift()
        self.top.focus_force()

    # ---- thread-safe -------------------------------------------------
    def target_size(self):
        return self._size

    MAX_PENDING_PATCHES = 120

    def submit(self, img, meta: Optional[dict] = None) -> None:
        """A full frame (scaled to the window size)."""
        with self._lock:
            self._pending = (img, meta or {})
            self._pending_patches.clear()
            self._expected_size = img.size
            self._overflow = False

    def submit_patches(self, size, patches, meta: Optional[dict] = None) -> None:
        """Only the changed pieces: [(x, y, image)], in image coordinates."""
        with self._lock:
            if len(self._pending_patches) >= self.MAX_PENDING_PATCHES:
                self._overflow = True        # Tk cannot keep up: let the next frame be a full one
                self._pending_patches.clear()
                return
            self._pending_patches.append((tuple(size), patches, meta or {}))

    def needs_full(self, size) -> bool:
        """Can the video client send partial updates at this size?"""
        with self._lock:
            return self._overflow or self._expected_size != tuple(size)

    def stats(self) -> dict:
        """Statistics of the last measurement window (also sent to the Mac)."""
        with self._lock:
            return dict(self._stats)

    def show(self) -> None:
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()

    def set_status(self, text: str) -> None:
        with self._lock:
            self._status = text

    # ---- Tk thread --------------------------------------------------------
    def _on_resize(self, event) -> None:
        self._size = (max(event.width, 1), max(event.height, 1))

    def toggle_fullscreen(self) -> None:
        self.set_fullscreen(not self._fullscreen)

    def set_fullscreen(self, value: bool) -> None:
        self._fullscreen = value
        self.top.attributes("-fullscreen", value)
        self._refresh_overlay()

    # ---- stats box ------------------------------------------------------------
    def _pref_key(self) -> str:
        return "overlay_fullscreen" if self._fullscreen else "overlay_window"

    def overlay_visible(self) -> bool:
        default = self._fullscreen
        v = self.prefs.get(self._pref_key()) if self.prefs is not None else self._ov_local.get(self._pref_key())
        return default if v is None else bool(v)

    def toggle_overlay(self) -> None:
        val = not self.overlay_visible()
        if self.prefs is not None:
            try:
                self.prefs.set(self._pref_key(), val)
            except Exception:
                pass
        else:
            self._ov_local[self._pref_key()] = val
        self._refresh_overlay()

    def _refresh_overlay(self) -> None:
        from .overlay import overlay_lines
        if not (self.overlay_visible() and self._has_frame):
            self.overlay.place_forget()
            return
        try:
            info = dict(self.info_fn() or {})
        except Exception:
            info = {}
        with self._lock:
            st = dict(self._stats)
        self.overlay.configure(text="\n".join(overlay_lines(st, info)))
        self.overlay.place(relx=1.0, x=-10, y=10, anchor="ne")
        self.overlay.lift()

    def _pump(self) -> None:
        if self.closed:
            return
        with self._lock:
            item, self._pending = self._pending, None
            batches, self._pending_patches = self._pending_patches, []
            status, self._status = self._status, None
        if item is not None:
            img, meta = item
            t0 = time.monotonic()
            self._photo = ImageTk.PhotoImage(img)
            self._photo_size = img.size
            self.label.configure(image=self._photo, text="")
            self._has_frame = True
            self._count(meta, (time.monotonic() - t0) * 1000.0)
        for size, patches, meta in batches:
            if self._photo is None or self._photo_size != size:
                with self._lock:
                    self._overflow = True    # size mismatch: ask for a full frame
                break
            t0 = time.monotonic()
            name = str(self._photo)
            for x, y, img in patches:
                part = ImageTk.PhotoImage(img)
                self.label.tk.call(name, "copy", str(part), "-to", int(x), int(y))
            self.patch_batches += 1
            self._count(meta, (time.monotonic() - t0) * 1000.0)
        if item is None and not batches and status is not None and not self._has_frame:
            self.label.configure(text=status)
        elif item is None and not batches and status is not None:
            self.top.title(f"{self._base_title} — {status}")
        now = time.monotonic()
        if now - self._fps_t >= 2.0:
            span = now - self._fps_t
            if self._has_frame:
                self._update_stats(span)
                st = self._stats
                parts = [f"{st['fps']:.0f} fps", f"{st['kbps'] / 1024:.1f} MB/s"]
                if st["latency_ms"] > 0:
                    parts.append(T("latency ≈ {ms} ms").format(ms=f"{st['latency_ms']:.0f}"))
                self.top.title(f"{self._base_title} — {' · '.join(parts)}   " + T("(F11: full screen, F9: measurements)"))
                self._refresh_overlay()
            self._frames, self._fps_t = 0, now
        self.top.after(10, self._pump)

    def _count(self, meta: dict, disp_ms: float) -> None:
        self._frames += 1
        a = self._acc
        a["frames"] += 1
        a["bytes"] += int(meta.get("bytes", 0))
        a["enc"] += float(meta.get("enc_ms", 0.0))
        a["dec"] += float(meta.get("decode_ms", 0.0))
        a["disp"] += disp_ms

    def _update_stats(self, span: float) -> None:
        a = self._acc
        n = max(a["frames"], 1)
        rtt = None
        if self.rtt_fn is not None:
            try:
                rtt = self.rtt_fn()
            except Exception:
                rtt = None
        enc, dec, disp = a["enc"] / n, a["dec"] / n, a["disp"] / n
        latency = enc + dec + disp + (rtt / 2.0 if rtt else 0.0) if a["frames"] else 0.0
        with self._lock:
            self._stats = {"fps": a["frames"] / span, "kbps": a["bytes"] / 1024.0 / span, "enc_ms": enc,
                           "decode_ms": dec, "display_ms": disp, "latency_ms": latency,
                           "rtt_ms": rtt or 0.0}
        self._acc = {"frames": 0, "bytes": 0, "enc": 0.0, "dec": 0.0, "disp": 0.0}

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.top.destroy()
        finally:
            self.on_close()
