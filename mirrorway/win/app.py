"""The Windows app: tray icon, settings window, screen viewer."""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageDraw

from .. import APP_NAME, __version__
from .. import prefs, quick
from ..clipsync import ClipboardSync
from ..config import CONFIG_DIR
from ..discovery import Listener
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


def _audio_capture_factory():
    """WASAPI loopback to send Windows' sound to the Mac (3.4); only on Windows."""
    if sys.platform != "win32":
        return None
    from .audio import default_capture
    return default_capture


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
        self.panel = None
        self.hotkeys = None
        self._reconnect_until = 0.0
        self._lockscreen_busy_until = 0.0

        self.discovery = Listener()
        self.discovery.start()
        self.resolver = Resolver(cfg, self.discovery)
        self.backend = WinInputBackend()
        self.remote = RemoteControl(self.backend, lambda mt, obj: self.control.send_json(mt, obj))
        self.remote.listeners.append(lambda active: self._post(self._refresh_icon))
        self.remote.listeners.append(lambda active: self._post(self._on_remote_active, active))
        self.ring = None
        if sys.platform == "win32":                      # where the Mac's pointer arrives: a ring for half a second
            from .ring import RingIndicator
            self.ring = RingIndicator(enabled=lambda: bool(self.cfg.get("switch_ring", True)))
            self.remote.enter_listeners.append(self.ring.show)

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
                                     audio_capture_factory=_audio_capture_factory(),
                                     on_notify=lambda title, msg: self._post(self._show_notification, title, msg),
                                     lock_fn=winapi.lock_workstation if sys.platform == "win32" else None)
        self.control.enable_log_forwarding()
        if self.control.winaudio is not None:          # Mirrorway stopped while it had muted the speakers
            try:
                self.control.winaudio.restore_speakers()
            except Exception as exc:
                log.info("Could not restore the speakers: %s", exc)
        self.session = None
        if sys.platform == "win32":                      # Windows locked or unlocked: tell the Mac
            from .session import SessionWatcher
            self.session = SessionWatcher(self.control.report_lock)

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
        panel = bool(self.cfg.get("quick_panel", True))
        menu = pystray.Menu(
            M(lambda item: self.status_text, lambda icon, item: None, enabled=False),
            pystray.Menu.SEPARATOR,
            # a left click on the icon runs the default item: the panel (or the screen window without the panel)
            M(T("Open Mirrorway"), lambda icon, item: self._post(self.toggle_panel), default=panel, visible=panel),
            M(T("Show the Mac's Screen"), lambda icon, item: self._post(self.toggle_viewer), default=not panel),
            M(T("Settings…"), lambda icon, item: self._post(self.open_settings)),
            M(T("Clipboard Sync"), lambda icon, item: self._post(self._toggle_clip),
              checked=lambda item: bool(self.cfg.get("clipboard"))),
            M(T("Reconnect"), lambda icon, item: self.control.kick()),
            M(T("Language"), pystray.Menu(*[
                M(label, self._language_action(key),
                  checked=lambda item, k=key: (self.cfg.get("language") or "auto") == k, radio=True)
                for key, label in prefs.languages()])),
            M(T("Open the Log Folder"), lambda icon, item: os.startfile(str(CONFIG_DIR))),
            M(T("Create Diagnostics Bundle"), lambda icon, item: self._post(self.make_diag)),
            pystray.Menu.SEPARATOR,
            M(T("Quit (Mirrorway {version})").format(version=__version__), lambda icon, item: self._post(self.quit)),
        )
        self.icon = pystray.Icon("mirrorway", make_icon(COLORS["idle"]), APP_NAME, menu)
        self.icon.run_detached()   # on Windows the tray runs on its own thread

    def _language_action(self, key: str):
        # pystray accepts actions with 0, 1 or 2 parameters only (a default argument counts as one)
        return lambda icon, item: self._post(self._set_language, key)

    def _set_language(self, key: str) -> None:
        if (self.cfg.get("language") or "auto") == key:
            return
        self.cfg.set("language", key)
        self._show_notification(APP_NAME, T("The new language is used after Mirrorway restarts."))

    def _toggle_clip(self) -> None:
        self.cfg.set("clipboard", not bool(self.cfg.get("clipboard")))
        self._refresh_icon()

    # ---- tray panel and shortcuts ---------------------------------------------
    def panel_model(self) -> "quick.Panel":
        v = self.viewer
        viewer_open = v is not None and not v.closed
        fps = 0.0
        if viewer_open:
            try:
                fps = float((v.stats() or {}).get("fps") or 0.0)
            except Exception:
                fps = 0.0
        c = self.control
        return quick.win_panel(state=self.state, status_text=self.status_text, cfg=self.cfg,
                               mac_name=c.server_name or self.creds.mac_name() or "", remote=self.remote.active,
                               viewer=viewer_open, side=getattr(c, "server_side", "right"), rtt_ms=c.last_rtt_ms,
                               fps=fps, audio_supported=c.can_ask_mac(), audio_on=c.audio_on,
                               reconnecting=time.monotonic() < self._reconnect_until,
                               audio_route=c.audio_route if c.can_route_audio() else "",
                               sending_audio=c.sending_audio)

    def toggle_panel(self) -> None:
        if self.panel is None:
            from .panel import TrayPanel
            self.panel = TrayPanel(self.root, self.panel_action)
        self.panel.model = self.panel_model()
        was = self.panel.is_shown()
        self.panel.toggle()
        if not was and self.panel.is_shown():
            self.root.after(1000, self._panel_tick)

    def _panel_tick(self) -> None:
        p = self.panel
        if p is None or not p.is_shown():
            return
        p.update(self.panel_model())
        self.root.after(1000, self._panel_tick)

    def panel_action(self, key: str) -> None:
        log.info("Panel: %s", key)
        c = self.control
        if key == "primary":
            self.toggle_screen()
        elif key == "tile:clipboard":
            self._toggle_clip()
        elif key == "tile:files":
            self.cfg.set("file_clipboard", not bool(self.cfg.get("file_clipboard", True)))
        elif key == "tile:audio":
            c.toggle_audio()
        elif key == "tile:reconnect":
            self._reconnect_until = time.monotonic() + 3.0
            c.kick()
        elif key in ("tile:pair", "settings"):
            if self.panel is not None:
                self.panel.hide()
            self.open_settings()
        elif key == "tile:diag":
            self.make_diag()
        elif key == "quit":
            self.quit()
            return
        if self.panel is not None:
            self.panel.update(self.panel_model())
            if key == "tile:audio":                 # the Mac answers with the new state
                self.root.after(600, lambda: self.panel.update(self.panel_model()) if self.panel else None)

    def toggle_screen(self) -> None:
        """Show or hide the Mac's screen (the panel's button and Control + Alt + S)."""
        v = self.viewer
        if v is not None and not v.closed:
            self.close_viewer("from the panel or the shortcut")
        else:
            self.toggle_viewer()

    def close_viewer(self, reason: str) -> None:
        v = self.viewer
        if v is not None and not v.closed:
            self._close_reason = reason
            v.close()

    def hotkey_action(self, action: str) -> None:
        log.info("Shortcut: %s", action)
        if action == "screen":
            self.toggle_screen()

    def _start_hotkeys(self) -> None:
        from .hotkeys import Hotkeys
        self.hotkeys = Hotkeys(quick.win_shortcut_letters(self.cfg), lambda a: self._post(self.hotkey_action, a))
        self.hotkeys.start()

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
            self.close_viewer("by the Mac")

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
            self.settings_win.top.deiconify()
            self.settings_win.top.lift()
            return
        from .settings import SettingsWindow
        self.settings_win = SettingsWindow(self)

    def settings_pages(self) -> list:
        c, creds = self.control, self.creds
        fp = ""
        if creds.paired():
            try:
                from ..devkeys import fingerprint
                fp = fingerprint(creds.mac_pub())
            except Exception:
                fp = ""
        found = self.discovery.all()
        if found:
            found_text = T("Mac found on the network: {list}").format(
                list=", ".join(f"{n} ({ip})" for ip, n, _ in found))
        elif self.discovery.error:
            found_text = T("Discovery is not working (UDP 24851 could not be opened); enter the address by hand.")
        else:
            found_text = T("Looking for the Mac on the network…")
        from . import startup
        ctx = {"state": self.state, "status_text": self.status_text, "mac_name": c.server_name or creds.mac_name(),
               "mac_ip": (c.file_target or ("",))[0], "rtt_ms": c.last_rtt_ms, "auth": c.auth_mode,
               "paired": creds.paired(), "fingerprint": fp, "found": found_text,
               "audio_supported": c.can_ask_mac(), "audio_on": c.audio_on,
               "audio_route": c.audio_route, "route_supported": c.can_route_audio(),
               "sending_audio": c.sending_audio,
               "audio_error": c.winaudio.last_error if c.winaudio is not None and c.audio_route == "mac" else "",
               "startup": {"available": startup.available(), "enabled": startup.is_enabled()},
               "lockscreen": self.lockscreen_state(),
               "version": __version__}
        return prefs.win_pages(self.cfg, ctx)

    # ---- lock screen support (a service, installed with administrator permission) -------------------------------
    def lockscreen_state(self) -> dict:
        from .svcinstall import status
        st = status()
        st["busy"] = time.monotonic() < self._lockscreen_busy_until
        try:
            res = json.loads((CONFIG_DIR / "lockscreen-result.json").read_text("utf-8"))
            if time.time() - float(res.get("time") or 0) < 600 and not res.get("ok"):
                st["error"] = str(res.get("error") or "")
        except Exception:
            pass
        from .lockscreen import SERVICE_PROTOCOL
        installed = bool(st.get("installed"))
        st["outdated"] = installed and int(st.get("protocol") or 0) < SERVICE_PROTOCOL     # must be updated
        st["older"] = installed and bool(st.get("version")) and st["version"] != __version__   # may be updated
        return st

    def lockscreen_action(self, action: str) -> bool:
        """Installs or removes lock screen support: starts the installer with administrator permission (UAC)."""
        if sys.platform != "win32":
            return False
        import ctypes
        py = Path(sys.executable)
        pyw = py.with_name("pythonw.exe")
        exe = pyw if pyw.exists() else py
        cfg_path = getattr(self.cfg, "path", CONFIG_DIR / "win.json")
        params = f'-m mirrorway.win.svcinstall {action} --config "{cfg_path}"'
        root = str(self.control.install_root)
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), params, root, 0)
        if int(r) <= 32:
            log.info("Lock screen support: the %s was not started (%s)", action, r)
            return False
        log.info("Lock screen support: %s started (administrator permission)", action)
        self._lockscreen_busy_until = time.monotonic() + 60
        return True

    _BOOL_SETTINGS = ("clipboard", "file_clipboard", "open_viewer_on_start", "hw_decode", "overlay_fullscreen",
                      "overlay_window", "overlay_extend", "quick_panel", "switch_ring")

    def apply_setting(self, key: str, value) -> None:
        """A change in the Settings window."""
        log.info("Setting changed: %s", key)
        c = self.control
        try:
            if key in self._BOOL_SETTINGS:
                self.cfg.set(key, bool(value))
            elif key == "viewer":
                self.cfg.set("viewer", value)
            elif key == "language":
                self._set_language(value)
            elif key == "host":
                host = str(value or "").strip()
                if host != (self.cfg.get("host") or ""):
                    self.cfg.set("host", host)
                    c.kick()
            elif key == "shortcut:screen":
                self.cfg.set("shortcuts", {"screen": value or ""})
                if self.hotkeys is not None:
                    self.hotkeys.stop()
                    self.hotkeys = None
                if sys.platform == "win32":
                    self._start_hotkeys()
            elif key == "startup":
                from . import startup
                if value:
                    startup.enable(c.install_root)
                else:
                    startup.disable()
            elif key == "action:reconnect":
                self._reconnect_until = time.monotonic() + 3.0
                c.kick()
            elif key == "action:audio":
                c.ask_mac({"audio": not c.audio_on})
            elif key == "audio_route":
                c.set_audio_route(str(value))
            elif key == "action:logs":
                if sys.platform == "win32":
                    os.startfile(str(CONFIG_DIR))
            elif key == "action:diag":
                self.make_diag()
            elif key in ("action:lockscreen_install", "action:lockscreen_remove"):
                self.lockscreen_action("install" if key.endswith("install") else "uninstall")
            else:
                log.warning("Unknown setting: %s", key)
                return
        except Exception as exc:
            log.exception("Could not apply the setting %s", key)
            self._show_notification(APP_NAME, T("Could not change the setting: {error}").format(error=exc))
        self._refresh_icon()

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
        if sys.platform == "win32":
            self._start_hotkeys()
        if self.ring is not None:
            self.ring.start()                            # the window and the frames are ready before the first switch
        if self.session is not None:
            self.session.start()
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
            if self.hotkeys is not None:
                self.hotkeys.stop()
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
