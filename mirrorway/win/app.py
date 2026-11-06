"""The Windows app: tray icon, settings window, screen viewer."""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw

from .. import APP_NAME, __version__
from ..clipsync import ClipboardSync
from ..config import CONFIG_DIR
from ..discovery import Listener
from ..i18n import NAMES as LANGUAGE_NAMES
from ..i18n import T
from ..protocol import MSG_LOG
from . import winapi
from .backend import WinClipboard, WinInputBackend
from .client import ControlClient, Credentials, Resolver, VideoClient
from .remote import RemoteControl
from .viewer import Viewer

log = logging.getLogger("mirrorway.winapp")

# Tray icon colour by state: the logo's violet when connected, its teal while the Mac's keyboard and mouse are here
COLORS = {"connected": (91, 63, 214), "active": (13, 148, 136), "idle": (120, 120, 120), "error": (220, 38, 38)}


ICON_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "mirrorway.ico")
# The logo's monogram in logo coordinates (the rounded square spans 100..924): M on top, its faded reflection W below
_MONOGRAM = ((((300, 512), (300, 296), (512, 468), (724, 296), (724, 512)), 255),
             (((300, 532), (300, 728), (512, 556), (724, 728), (724, 532)), 128))


def make_icon(color) -> Image.Image:
    """Tray icon: the Mirrorway monogram on a rounded square in the colour of the current state."""
    size = 256                                   # drawn large, then scaled down for smooth edges
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((4, 4, size - 5, size - 5), radius=56, fill=tuple(color) + (255,))
    k = (size - 8) / 824
    width = round(68 * k)
    for points, alpha in _MONOGRAM:
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        xy = [(4 + (x - 100) * k, 4 + (y - 100) * k) for x, y in points]
        d.line(xy, fill=(255, 255, 255, alpha), width=width, joint="curve")
        r = width / 2
        for x, y in (xy[0], xy[-1]):            # round line ends
            d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, alpha))
        img.alpha_composite(layer)
    return img.resize((64, 64), Image.LANCZOS)


def _audio_player_factory():
    """SDL2 to play the Mac's sound (the same library as the screen window); without it there is no audio."""
    import importlib.util
    try:
        if importlib.util.find_spec("sdl2") is None:
            log.info("Cannot play sound: SDL2 (pysdl2) is not installed")
            return None
        from .audio import SDLAudioPlayer
        return SDLAudioPlayer
    except Exception as exc:
        log.info("Cannot play sound (no SDL2): %s", exc)
        return None


class WinApp:
    def __init__(self, cfg, open_viewer: bool = False):
        self.cfg = cfg
        self._open_viewer = open_viewer
        winapi.set_dpi_aware()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        try:
            self.root.iconbitmap(default=ICON_FILE)        # window icon of every Tk window (Windows)
        except Exception:
            pass
        self.ui_q: "queue.Queue" = queue.Queue()
        self.status_text = T("Starting…")
        self.state = "idle"
        self.viewer = None
        self.video = None
        self.settings_win = None
        self.icon = None

        self.discovery = Listener()
        self.discovery.start()
        self.resolver = Resolver(cfg, self.discovery)
        self.backend = WinInputBackend()
        self.remote = RemoteControl(self.backend, lambda mt, obj: self.control.send_json(mt, obj))
        self.remote.listeners.append(lambda active: self._post(self._refresh_icon))
        self.remote.listeners.append(lambda active: self._post(self._on_remote_active, active))

        def clip_factory(send_fn):
            try:
                from .clipboard import WinRichClipboard
                backend = WinRichClipboard()           # text, HTML, RTF, images
            except Exception as exc:
                log.info("Rich clipboard unavailable (%s); text only", exc)
                try:
                    backend = WinClipboard()
                except Exception as exc2:
                    log.warning("Clipboard unavailable: %s", exc2)
                    return None
            return ClipboardSync(backend, send_fn, lambda: bool(self.cfg.get("clipboard")), peer="Mac",
                                 files_fn=lambda paths: (self.control.files.upload(paths)
                                                         if self.control.files is not None else False))

        self.creds = Credentials(cfg)
        self.control = ControlClient(cfg, self.resolver, self.remote, clipsync_factory=clip_factory,
                                     on_status=self._on_status, monitors_fn=winapi.monitors,
                                     on_update=lambda v: self._post(self.restart_for_update, v),
                                     credentials=self.creds,
                                     on_view=lambda req: self._post(self._view_requested, req),
                                     files_dir=CONFIG_DIR,
                                     audio_player_factory=_audio_player_factory(),
                                     on_notify=lambda title, msg: self._post(self._show_notification, title, msg))
        self.control.enable_log_forwarding()

    # ---- UI queue (from other threads to Tk) -----------------------
    def _post(self, fn, *args) -> None:
        self.ui_q.put((fn, args))

    def _drain(self) -> None:
        while True:
            try:
                fn, args = self.ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:
                log.exception("UI action failed")
        self.root.after(50, self._drain)

    # ---- status --------------------------------------------------------------
    def _on_status(self, state: str, text: str) -> None:
        self.state = state
        self.status_text = text
        self._post(self._refresh_icon)
        if state in ("auth", "unpaired"):
            self._post(self._maybe_open_settings)
        if state == "paired":
            self._post(self._on_paired)

    def _refresh_icon(self) -> None:
        if self.icon is None:
            return
        if self.remote.active:
            color, tip = COLORS["active"], APP_NAME + ": " + T("the Mac's keyboard and mouse are on this PC")
        elif self.state == "connected":
            color, tip = COLORS["connected"], f"{APP_NAME}: {self.status_text}"
        elif self.state in ("auth", "error", "unpaired"):
            color, tip = COLORS["error"], f"{APP_NAME}: {self.status_text}"
        else:
            color, tip = COLORS["idle"], f"{APP_NAME}: {self.status_text}"
        try:
            self.icon.icon = make_icon(color)
            self.icon.title = tip[:120]
            self.icon.update_menu()
        except Exception:
            pass
        if self.settings_win is not None:
            try:
                self.settings_win.status_var.set(self.status_text)
            except Exception:
                pass

    def _on_paired(self) -> None:
        if self.settings_win is not None:
            self.settings_win.refresh_pairing()
        try:
            if self.icon is not None:
                self.icon.notify(T("Paired with {name}.").format(name=self.creds.mac_name()), APP_NAME)
        except Exception:
            pass

    def make_diag(self):
        from pathlib import Path
        from .. import diag
        try:
            out = diag.default_output_dir() / diag.bundle_name("windows")
            status = {"state": self.state, "status": self.status_text, "auth": self.control.auth_mode,
                      "paired": self.creds.paired(), "mac": self.creds.mac_name(),
                      "video": dict(self.video.info) if self.video is not None else None,
                      "viewer": self.viewer.stats() if self.viewer is not None else None}
            diag.make_bundle(out, diag.log_files(Path(CONFIG_DIR), "mirrorway-win"),
                             self.cfg.as_dict() if hasattr(self.cfg, "as_dict") else {}, status=status)
            log.info("Diagnostics bundle created: %s", out)
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(out)])
            return out
        except Exception as exc:
            log.exception("Could not create the diagnostics bundle: %s", exc)
            return None

    def _maybe_open_settings(self) -> None:
        if self.settings_win is None:
            self.open_settings()

    # ---- tray --------------------------------------------------------------
    def _build_tray(self):
        import pystray
        M = pystray.MenuItem
        menu = pystray.Menu(
            M(lambda item: self.status_text, lambda icon, item: None, enabled=False),
            pystray.Menu.SEPARATOR,
            M(T("Show the Mac's Screen"), lambda icon, item: self._post(self.toggle_viewer), default=True),
            M(T("Settings…"), lambda icon, item: self._post(self.open_settings)),
            M(T("Clipboard Sync"), lambda icon, item: self._post(self._toggle_clip),
              checked=lambda item: bool(self.cfg.get("clipboard"))),
            M(T("Reconnect"), lambda icon, item: self.control.kick()),
            M(T("Language"), pystray.Menu(*[
                M(label, lambda icon, item, k=key: self._post(self._set_language, k),
                  checked=lambda item, k=key: (self.cfg.get("language") or "auto") == k, radio=True)
                for key, label in [("auto", T("Automatic (system)"))] + list(LANGUAGE_NAMES.items())])),
            M(T("Open the Log Folder"), lambda icon, item: os.startfile(str(CONFIG_DIR))),
            M(T("Create Diagnostics Bundle"), lambda icon, item: self._post(self.make_diag)),
            pystray.Menu.SEPARATOR,
            M(T("Quit (Mirrorway {version})").format(version=__version__), lambda icon, item: self._post(self.quit)),
        )
        self.icon = pystray.Icon("mirrorway", make_icon(COLORS["idle"]), APP_NAME, menu)
        self.icon.run_detached()   # on Windows the tray runs on its own thread

    def _set_language(self, key: str) -> None:
        if (self.cfg.get("language") or "auto") == key:
            return
        self.cfg.set("language", key)
        self._show_notification(APP_NAME, T("The new language is used after Mirrorway restarts."))

    def _toggle_clip(self) -> None:
        self.cfg.set("clipboard", not bool(self.cfg.get("clipboard")))
        self._refresh_icon()

    # ---- screen viewer --------------------------------------------------
    def toggle_viewer(self) -> None:
        if self.viewer is not None and not self.viewer.closed:
            self.viewer.show()
            return
        title = APP_NAME + " — " + T("Mac screen")
        self.viewer = None
        if str(self.cfg.get("viewer") or "auto") != "tk":
            try:
                from .sdlview import SDLViewer
                self.viewer = SDLViewer(title, on_close=self._viewer_closed_any_thread,
                                        rtt_fn=lambda: self.control.last_rtt_ms,
                                        info_fn=self._video_info, prefs=self.cfg)
                self._remote_info(f"Screen window: GPU ({self.viewer.renderer_name})")
            except Exception as exc:
                log.warning("Could not open the GPU window (%s); using the Tk window", exc)
                self.viewer = None
        if self.viewer is None:
            self.viewer = Viewer(self.root, title, on_close=self._viewer_closed,
                                 rtt_fn=lambda: self.control.last_rtt_ms,
                                 info_fn=self._video_info, prefs=self.cfg)
        self._viewer_opened_at = time.monotonic()
        self._start_video()

    def _start_video(self) -> None:
        v = self.viewer
        hw = None
        if sys.platform == "win32" and self.cfg.get("hw_decode", True):
            hw = "d3d11va"
        self.video = VideoClient(self.cfg, self.resolver, on_frame=v.submit, on_status=v.set_status,
                                 target_size=v.target_size, stats_fn=v.stats,
                                 on_patches=v.submit_patches, needs_full=v.needs_full,
                                 on_video=getattr(v, "submit_video", None),
                                 hw_decode=hw if hasattr(v, "submit_video") else None,
                                 credentials=self.creds, on_info=self._on_video_info)
        self.video.start()

    def _show_notification(self, title: str, msg: str) -> None:
        icon = self.icon
        if icon is None:
            return
        try:
            icon.notify(msg, f"{APP_NAME}: {title}")
        except Exception as exc:
            log.debug("Could not show the notification: %s", exc)

    # ---- second screen -----------------------------------------------------------
    def _view_requested(self, req: dict) -> None:
        """The Mac asked for the screen window (second screen, or the Screen tile in the Mac's panel)."""
        is_open = self.viewer is not None and not self.viewer.closed
        if req.get("open") and not is_open:
            log.info("The Mac had the screen window opened")
            self.toggle_viewer()
        elif req.get("open") is False and is_open:
            log.info("The Mac had the screen window closed")
            self._close_reason = "by the Mac"
            self.viewer.close()

    def _on_video_info(self, info: dict) -> None:
        """From the video thread: if the second screen is on, the window goes full screen on that monitor."""
        v = self.viewer
        fn = getattr(v, "set_extend", None) if v is not None else None
        if fn is None:
            return
        ext = info.get("extend") or None
        rect = ext.get("rect") if isinstance(ext, dict) else None
        fn(rect if isinstance(rect, list) and len(rect) == 4 else None)

    def _on_remote_active(self, active: bool) -> None:
        """On a single-monitor PC the second screen covers the whole display: the window minimises when control moves to Windows."""
        v = self.viewer
        fn = getattr(v, "set_hidden", None) if v is not None else None
        if fn is None:
            return
        ext = (self.video.info.get("extend") if self.video is not None else None) or None
        try:
            single = len(winapi.monitors()) <= 1
        except Exception:
            single = False
        fn(bool(active and ext and single))

    def _video_info(self) -> dict:
        v = self.video
        return dict(v.info) if v is not None else {}

    def _viewer_closed_any_thread(self) -> None:
        if threading.current_thread() is threading.main_thread():
            self._viewer_closed()
        else:
            self._post(self._viewer_closed)

    def _remote_info(self, msg: str) -> None:
        """Forward an info line to the Mac log (for events that are not warnings)."""
        try:
            self.control.send_json(MSG_LOG, {"level": "INFO", "msg": msg})
        except Exception:
            pass

    def _viewer_closed(self) -> None:
        if self.viewer is None or not self.viewer.closed:
            return                      # a late close notice from an old window
        opened = getattr(self, "_viewer_opened_at", None)
        dur = f" (open for {time.monotonic() - opened:.0f} s)" if opened else ""
        who, self._close_reason = getattr(self, "_close_reason", None) or "by the user", None
        log.info("Screen window closed %s%s", who, dur)
        self._remote_info(f"Screen window closed {who}{dur}")
        if self.video is not None:
            self.video.stop()
            self.video = None
        self.viewer = None

    # ---- settings ------------------------------------------------------------
    def open_settings(self) -> None:
        if self.settings_win is not None:
            self.settings_win.top.lift()
            return
        self.settings_win = SettingsWindow(self)

    def apply_settings(self, host: str, code: str) -> None:
        self.cfg.set("host", host.strip(), save=False)
        code = code.strip()
        digits = "".join(ch for ch in code if ch.isdigit())
        if len(digits) == 6 and not any(ch.isalpha() for ch in code):
            self.cfg.set("pairing_code", digits, save=False)      # new method: the 6-digit code from the Mac
        elif code:
            self.cfg.set("pair_code", code.upper(), save=False)   # old long code (migration)
        save = getattr(self.cfg, "save", None)
        if save is not None:
            save()
        self.control.kick()
        if self.video is not None:
            self.video.stop()
            self.video = None
            if self.viewer is not None:
                self._start_video()

    # ---- run / quit -------------------------------------------------------
    def unpair(self) -> None:
        self.creds.forget()
        self.cfg.set("pair_code", "")
        self.control.kick()

    def run(self) -> None:
        self._build_tray()
        self.control.start()
        self.root.after(50, self._drain)
        if not (self.creds.paired() or self.creds.legacy_code()):
            self.root.after(300, self.open_settings)
        elif self.cfg.get("open_viewer_on_start") or self._open_viewer:
            self.root.after(500, self.toggle_viewer)
        self.root.mainloop()

    def restart_for_update(self, version: str) -> None:
        """An update was applied: show a notification, start the new version, end this process."""
        log.info("Restarting with the new version: %s", version)
        viewer_open = self.viewer is not None and not self.viewer.closed
        try:
            if self.icon is not None:
                self.icon.notify(T("Updated to Mirrorway {version}.").format(version=version), APP_NAME)
        except Exception:
            pass
        try:
            from .updater import restart_command
            cmd = restart_command()
            if viewer_open and "--viewer" not in cmd:
                cmd.append("--viewer")
            flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(cmd, cwd=str(self.control.install_root), creationflags=flags, close_fds=True)
        except Exception:
            log.exception("Could not start the new version")
        self.quit()

    def quit(self) -> None:
        try:
            self.remote.on_exit()
            self.control.stop()
            if self.video is not None:
                self.video.stop()
            v, self.viewer = self.viewer, None
            if v is not None and hasattr(v, "submit_video"):
                v.closed = True                  # the SDL window closes on its own thread
                v._wake()
            if self.icon is not None:
                self.icon.stop()
        finally:
            self.root.quit()


class SettingsWindow:
    def __init__(self, app: WinApp):
        self.app = app
        cfg = app.cfg
        top = self.top = tk.Toplevel(app.root)
        top.title(APP_NAME + " — " + T("Settings"))
        top.resizable(False, False)
        top.protocol("WM_DELETE_WINDOW", self.close)
        frm = ttk.Frame(top, padding=16)
        frm.grid(sticky="nsew")

        self.pair_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.pair_var, wraplength=400, foreground="#15803d").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.help_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.help_var, wraplength=400).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.code_label = ttk.Label(frm, text=T("Pairing code:"))
        self.code_label.grid(row=2, column=0, sticky="w")
        self.code_var = tk.StringVar(value="")
        code_entry = ttk.Entry(frm, textvariable=self.code_var, width=24, font=("Consolas", 12))
        code_entry.grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(frm, text=T("Mac address:")).grid(row=3, column=0, sticky="w")
        self.host_var = tk.StringVar(value=cfg.get("host") or "")
        ttk.Entry(frm, textvariable=self.host_var, width=24).grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Label(frm, text=T("Leave it empty to find the Mac automatically. If it is not found, enter the "
                              "'This Mac: …' address from the Mac menu."), wraplength=400, foreground="#666").grid(
            row=4, column=0, columnspan=2, sticky="w")

        self.found_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.found_var, foreground="#2563eb").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.status_var = tk.StringVar(value=app.status_text)
        ttk.Label(frm, textvariable=self.status_var, wraplength=400).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(4, 10))

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=2, sticky="ew")
        self.unpair_btn = ttk.Button(btns, text=T("Remove Pairing"), command=self.unpair)
        ttk.Button(btns, text=T("Save and Connect"), command=self.save).pack(side="right")
        ttk.Button(btns, text=T("Close"), command=self.close).pack(side="right", padx=(0, 8))

        top.bind("<Return>", lambda e: self.save())
        code_entry.focus_set()
        self.refresh_pairing()
        self._tick()

    def refresh_pairing(self) -> None:
        creds = self.app.creds
        if creds.paired():
            from ..devkeys import fingerprint
            pub = creds.mac_pub()
            self.pair_var.set(T("Paired Mac: {name} (fingerprint {id})").format(name=creds.mac_name(), id=fingerprint(pub)))
            self.help_var.set(T("This PC connects to the Mac with its device key. To pair with another Mac, "
                                "click Mirrorway › Pair a New Device… on that Mac and enter the code below."))
            self.code_label.configure(text=T("New pairing code:"))
            self.unpair_btn.pack(side="left")
        else:
            self.pair_var.set("")
            self.help_var.set(T("On the Mac, click the Mirrorway icon in the menu bar, choose 'Pair a New Device…' "
                                "and enter the 6-digit code it shows below."))
            self.code_label.configure(text=T("Pairing code:"))
            self.unpair_btn.pack_forget()

    def unpair(self) -> None:
        from tkinter import messagebox
        if messagebox.askyesno(APP_NAME, T("Remove this PC's pairing with the Mac? Connecting again will need "
                                           "a new code from the Mac."), parent=self.top):
            self.app.unpair()
            self.refresh_pairing()

    def _tick(self) -> None:
        if self.top is None:
            return
        found = self.app.discovery.all()
        if found:
            self.found_var.set(T("Mac found on the network: {list}").format(list=", ".join(f"{n} ({ip})" for ip, n, _ in found)))
        elif self.app.discovery.error:
            self.found_var.set(T("Discovery is not working (UDP 24851 could not be opened); enter the address by hand."))
        else:
            self.found_var.set(T("Looking for the Mac on the network…"))
        self.top.after(1000, self._tick)

    def save(self) -> None:
        self.app.apply_settings(self.host_var.get(), self.code_var.get())
        self.code_var.set("")
        self.status_var.set(T("Connecting…"))

    def close(self) -> None:
        top, self.top = self.top, None
        if top is not None:
            top.destroy()
        self.app.settings_win = None
