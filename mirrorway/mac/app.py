"""The Mac menu bar app (rumps)."""
from __future__ import annotations

import errno
import logging
import os
import signal
import subprocess
import threading
import time

import rumps

from .. import APP_NAME, __version__
from ..config import CONFIG_DIR
from ..i18n import NAMES as LANGUAGE_NAMES
from ..i18n import T
from ..wol import WakeSession
from . import loginitem
from . import selfupdate as U
from .capture import list_displays
from .clipboard import MacClipboard
from .server import MirrorwayServer, wol_warnings
from .tap import EventTap, MacPlatform, accessibility_trusted, request_input_monitoring

log = logging.getLogger("mirrorway.app")

SPEEDS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
ARROWS = {"right": "→", "left": "←", "top": "↑", "bottom": "↓"}

PRIVACY_URLS = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "input": "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
}


def sides():
    return [("right", T("Right")), ("left", T("Left")), ("top", T("Above")), ("bottom", T("Below"))]


def qualities():
    return [("low", T("Low (smooth, little bandwidth)")), ("medium", T("Medium")), ("high", T("High (sharp text)"))]


def codecs():
    return [("h264", T("H.264 — smooth (recommended)")), ("jpeg", T("JPEG — sharpest text"))]


def layouts():
    return [("auto", T("Automatic")), ("iso", T("ISO (Turkish/European)")), ("ansi", T("ANSI (US)"))]


def _open(url: str) -> None:
    subprocess.Popen(["open", url])


def _reveal(path) -> None:
    """Show the file selected in Finder."""
    subprocess.Popen(["/usr/bin/open", "-R", str(path)])


def other_instances(port: int) -> list:
    """PIDs of other Mirrorway processes listening on the port."""
    try:
        out = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    pids = []
    for tok in out.split():
        try:
            pid = int(tok)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            cmd = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            cmd = ""
        if "mirrorway" in cmd or "kopru" in cmd:          # also an old Köprü still running
            pids.append(pid)
    return pids


def stop_processes(pids: list, timeout: float = 4.0) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + timeout
    alive = list(pids)
    while alive and time.time() < deadline:
        time.sleep(0.2)
        still = []
        for pid in alive:
            try:
                os.kill(pid, 0)
                still.append(pid)
            except ProcessLookupError:
                pass
        alive = still
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.3)


# Menu bar icon: a template image (macOS tints it for light and dark menu bars), 40 px shown at 20 pt
MENUBAR_ICON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "menubar.png")


class MirrorwayMacApp(rumps.App):
    def __init__(self, cfg):
        icon = MENUBAR_ICON if os.path.exists(MENUBAR_ICON) else None
        super().__init__(APP_NAME, title=None if icon else "⇆", icon=icon, template=True, quit_button=None)
        self._has_icon = icon is not None
        self.cfg = cfg
        self.platform = MacPlatform()
        self.clip_backend = MacClipboard()
        self.server = MirrorwayServer(cfg, self.platform, clipboard_backend=self.clip_backend)
        self.tap = None
        self._perm_alert_shown = False
        # Sending the packets involves short waits; run in the background so the menu does not freeze
        self.wake = WakeSession(lambda: threading.Thread(target=self.server.wake_windows, daemon=True,
                                                         name="mirrorway-wol").start())
        self._wake_msg = ""
        self._wake_msg_until = 0.0
        self.updater = U.Updater(cfg)
        self._restart_pending = False
        self._build_menu()

    # ---- menu ---------------------------------------------------------------
    def _check_group(self, items: dict, selected) -> None:
        for key, item in items.items():
            item.state = 1 if key == selected else 0

    def _build_menu(self) -> None:
        cfg = self.cfg
        self.mi_status = rumps.MenuItem(T("Starting…"))
        self.mi_stats = rumps.MenuItem(T("Measurements: —"))
        # Note: rumps keys menus by title; the initial titles must be unique.
        self.mi_code = rumps.MenuItem(T("Pair a New Device…"), callback=self.pair_new_device)
        self.m_net = rumps.MenuItem(T("Network"))
        self.mi_net_state = rumps.MenuItem(T("Network status"))
        self.mi_net_trust = rumps.MenuItem(T("Trust This Network"), callback=self._trust_network)
        self.mi_net_lock = rumps.MenuItem(T("Work Only on the Home Network"), callback=self._toggle_netlock)
        self.mi_net_lock.state = int(bool(cfg.get("network_lock", True)))
        self.mi_net_reset = rumps.MenuItem(T("Reset Trusted Networks (keep only this one)"),
                                           callback=self._reset_networks)
        for it in (self.mi_net_state, rumps.separator, self.mi_net_trust, self.mi_net_lock, self.mi_net_reset):
            self.m_net.add(it)
        self.m_devices = rumps.MenuItem(T("Paired Devices"))
        self.m_devices.add(rumps.MenuItem(T("(none)")))
        self._devices_sig = None
        self.m_extend = rumps.MenuItem(T("Second Screen (Windows monitor)"))
        self.m_extend.add(rumps.MenuItem(T("Available once Windows connects")))
        self._extend_sig = None
        self.mi_ip = rumps.MenuItem(T("This Mac"), callback=self.copy_ip)
        self.mi_switch = rumps.MenuItem(T("Switch to Windows"), callback=self.switch_to_windows)
        self.mi_wake = rumps.MenuItem(T("Wake Windows"), callback=self.wake_windows)

        # Where Windows is
        self.m_side = rumps.MenuItem(T("Windows Is on This Side of the Screen"))
        self.side_items = {}
        for key, label in sides():
            it = rumps.MenuItem(label, callback=self._make_setter("side", key, self.side_items))
            self.side_items[key] = it
            self.m_side.add(it)
        self._check_group(self.side_items, cfg.get("side"))

        # Pointer speed
        self.m_speed = rumps.MenuItem(T("Pointer Speed (Windows)"))
        self.mi_auto_speed = rumps.MenuItem(T("Automatic Speed (screen ratio)"), callback=self._make_toggle("auto_speed"))
        self.mi_auto_speed.state = int(bool(cfg.get("auto_speed", True)))
        self.m_speed.add(self.mi_auto_speed)
        self.m_speed.add(rumps.separator)
        self.speed_items = {}
        for sp in SPEEDS:
            it = rumps.MenuItem(f"{sp:g}×", callback=self._make_setter("speed", sp, self.speed_items))
            self.speed_items[sp] = it
            self.m_speed.add(it)
        self._check_group(self.speed_items, float(cfg.get("speed")))

        self.m_scroll = rumps.MenuItem(T("Scrolling"))
        self.scroll_items = {}
        for sp in (0.5, 1.0, 1.5, 2.0, 3.0):
            it = rumps.MenuItem(T("Speed {n}×").format(n=f"{sp:g}"),
                                callback=self._make_setter("scroll_speed", sp, self.scroll_items))
            self.scroll_items[sp] = it
            self.m_scroll.add(it)
        self._check_group(self.scroll_items, float(cfg.get("scroll_speed")))
        self.mi_invert = rumps.MenuItem(T("Reverse Direction"), callback=self._make_toggle("invert_scroll"))
        self.mi_invert.state = int(bool(cfg.get("invert_scroll")))
        self.m_scroll.add(rumps.separator)
        self.m_scroll.add(self.mi_invert)

        # Keyboard
        self.m_kb = rumps.MenuItem(T("Keyboard"))
        self.mi_swap = rumps.MenuItem("Cmd → Ctrl, Ctrl → Win", callback=self._make_toggle("swap_cmd_ctrl", True))
        self.mi_swap.state = int(bool(cfg.get("swap_cmd_ctrl")))
        self.mi_altgr = rumps.MenuItem(T("Left Option → AltGr (for @, € etc.)"), callback=self._toggle_altgr)
        self.mi_altgr.state = int(cfg.get("left_option") == "altgr")
        self.m_kb.add(self.mi_swap)
        self.m_kb.add(self.mi_altgr)
        self.mi_unicode = rumps.MenuItem(T("Special Characters with Option (@ € …)"),
                                         callback=self._make_toggle("option_unicode"))
        self.mi_unicode.state = int(bool(cfg.get("option_unicode", True)))
        self.m_kb.add(self.mi_unicode)
        self.m_kb.add(rumps.separator)
        self.layout_items = {}
        for key, label in layouts():
            it = rumps.MenuItem(T("Keyboard Type: {name}").format(name=label),
                                callback=self._make_setter("keyboard_layout", key, self.layout_items, True))
            self.layout_items[key] = it
            self.m_kb.add(it)
        self._check_group(self.layout_items, cfg.get("keyboard_layout"))

        self.mi_edge = rumps.MenuItem(T("Switch at the Screen Edge"), callback=self._make_toggle("edge_switch"))
        self.mi_edge.state = int(bool(cfg.get("edge_switch")))
        self.mi_clip = rumps.MenuItem(T("Clipboard Sync"), callback=self._make_toggle("clipboard"))
        self.mi_clip.state = int(bool(cfg.get("clipboard")))
        self.mi_gestures = rumps.MenuItem(T("Trackpad Gestures on Windows"), callback=self._make_toggle("gestures"))
        self.mi_gestures.state = int(bool(cfg.get("gestures", True)))
        self.mi_files = rumps.MenuItem(T("File Copy and Paste"), callback=self._make_toggle("file_clipboard"))
        self.mi_files.state = int(bool(cfg.get("file_clipboard", True)))
        self.mi_files_state = rumps.MenuItem(T("File transfer: —"))
        self.m_audio = rumps.MenuItem(T("Audio"))
        self.mi_audio = rumps.MenuItem(T("Play the Mac's Sound on Windows"), callback=self._toggle_audio)
        self.mi_audio.state = int(bool(cfg.get("audio")))
        self.mi_audio_mute = rumps.MenuItem(T("Mute the Mac's Speakers Meanwhile"), callback=self._toggle_audio_mute)
        self.mi_audio_mute.state = int(bool(cfg.get("audio_mute_mac", True)))
        self.mi_audio_state = rumps.MenuItem(T("Status: —"))
        self.m_audio.add(self.mi_audio)
        self.m_audio.add(self.mi_audio_mute)
        self.m_audio.add(None)
        self.m_audio.add(self.mi_audio_state)

        # Screen sharing
        self.mi_share = rumps.MenuItem(T("Screen Sharing"), callback=self._toggle_share)
        self.mi_share.state = int(bool(cfg.get("screen_share")))
        self.m_display = rumps.MenuItem(T("Shared Display"))
        self.display_items = {}
        try:
            displays = list_displays()
        except Exception:
            displays = [{"index": 0, "w": 0, "h": 0, "main": True}]
        for d in displays:
            label = T("Display {n} ({w}×{h})").format(n=d["index"] + 1, w=d["w"], h=d["h"])
            if d.get("main"):
                label += " — " + T("main")
            it = rumps.MenuItem(label, callback=self._make_setter("display_index", d["index"],
                                                                  self.display_items, video=True))
            self.display_items[d["index"]] = it
            self.m_display.add(it)
        self._check_group(self.display_items, int(cfg.get("display_index") or 0))
        self.m_quality = rumps.MenuItem(T("Picture Quality"))
        self.quality_items = {}
        for key, label in qualities():
            it = rumps.MenuItem(label, callback=self._make_setter("quality", key, self.quality_items, video=True))
            self.quality_items[key] = it
            self.m_quality.add(it)
        self._check_group(self.quality_items, cfg.get("quality"))
        self.m_quality.add(rumps.separator)
        self.mi_adaptive = rumps.MenuItem(T("Adjust to the Connection Automatically (H.264)"),
                                          callback=self._toggle_adaptive)
        self.mi_adaptive.state = int(bool(cfg.get("adaptive_quality", True)))
        self.m_quality.add(self.mi_adaptive)
        self.m_codec = rumps.MenuItem(T("Video Format"))
        self.codec_items = {}
        for key, label in codecs():
            it = rumps.MenuItem(label, callback=self._make_setter("video_codec", key, self.codec_items, video=True))
            self.codec_items[key] = it
            self.m_codec.add(it)
        self._check_group(self.codec_items, cfg.get("video_codec") or "h264")

        # Open at login (only when running as Mirrorway.app)
        self.bundle = loginitem.bundle_path()
        self.mi_login = rumps.MenuItem(T("Open at Login"), callback=self._toggle_login if self.bundle else None)
        try:
            if loginitem.replace_legacy(self.bundle):
                log.info("Replaced Köprü's login item with Mirrorway's")
                cfg.set("login_item_set", True)
        except Exception:
            log.exception("Could not replace the old login item")
        if self.bundle and not cfg.get("login_item_set"):
            try:
                loginitem.enable(self.bundle)
            except Exception:
                log.exception("Could not add the login item")
            cfg.set("login_item_set", True)
        self.mi_login.state = int(bool(self.bundle) and loginitem.is_enabled())

        # Language
        self.m_lang = rumps.MenuItem(T("Language"))
        self.lang_items = {}
        for key, label in [("auto", T("Automatic (system)"))] + list(LANGUAGE_NAMES.items()):
            it = rumps.MenuItem(label, callback=lambda _s, k=key: self._set_language(k))
            self.lang_items[key] = it
            self.m_lang.add(it)
        self._check_group(self.lang_items, cfg.get("language") or "auto")

        # Updates (signed releases on GitHub)
        self.m_update = rumps.MenuItem(T("Updates"))
        self.mi_upd_state = rumps.MenuItem(f"Mirrorway {__version__}")
        self.mi_upd_check = rumps.MenuItem(T("Check for Updates"), callback=self.check_updates)
        self.mi_upd_install = rumps.MenuItem(T("No new version"), callback=self.install_update)
        self.mi_upd_auto = rumps.MenuItem(T("Check Automatically Once a Day"), callback=self._toggle_auto_update)
        self.mi_upd_auto.state = int(bool(cfg.get("auto_update_check", True)))
        self.mi_upd_rollback = rumps.MenuItem(T("Return to the Previous Version"), callback=self.rollback_update)
        for it in (self.mi_upd_state, self.mi_upd_check, self.mi_upd_install, rumps.separator,
                   self.mi_upd_auto, self.mi_upd_rollback):
            self.m_update.add(it)

        # Permissions
        self.m_perm = rumps.MenuItem(T("Open Permission Settings"))
        for key, label in (("accessibility", T("Accessibility")), ("input", T("Input Monitoring")),
                           ("screen", T("Screen Recording"))):
            self.m_perm.add(rumps.MenuItem(label, callback=lambda _s, u=PRIVACY_URLS[key]: _open(u)))

        self.menu = [
            self.mi_status,
            self.mi_stats,
            self.mi_code,
            self.m_devices,
            self.m_net,
            self.mi_ip,
            None,
            self.mi_switch,
            self.mi_wake,
            None,
            self.m_side,
            self.m_speed,
            self.m_scroll,
            self.m_kb,
            self.mi_edge,
            self.mi_clip,
            self.mi_files,
            self.mi_files_state,
            self.mi_gestures,
            self.m_audio,
            None,
            self.mi_share,
            self.m_extend,
            self.m_display,
            self.m_quality,
            self.m_codec,
            None,
            self.m_update,
            self.mi_login,
            self.m_lang,
            self.m_perm,
            rumps.MenuItem(T("Disconnect"), callback=lambda _s: self.server.disconnect_all()),
            rumps.MenuItem(T("Show Log"), callback=lambda _s: _open(str(CONFIG_DIR))),
            rumps.MenuItem(T("Create Diagnostics Bundle"), callback=self.make_diag),
            None,
            rumps.MenuItem(T("Quit Mirrorway {version}").format(version=__version__), callback=self.quit_app),
        ]
        self._refresh_static()

    def _refresh_static(self) -> None:
        arrow = ARROWS.get(self.cfg.get("side"), "→")
        self.mi_switch.title = T("Switch to Windows   ⌃⌥{arrow}   (back: ⌃⌥ + opposite arrow)").format(arrow=arrow)

    def _make_setter(self, key, value, group, reload_router=False, video=False):
        def cb(_sender):
            self.cfg.set(key, value)
            self._check_group(group, value)
            if reload_router or key == "side":
                self.server.router.reload_settings()
            if video:
                self.server.restart_video()
            self._refresh_static()
        return cb

    def _make_toggle(self, key, reload_router=False):
        def cb(sender):
            val = not bool(self.cfg.get(key))
            self.cfg.set(key, val)
            sender.state = int(val)
            if reload_router:
                self.server.router.reload_settings()
        return cb

    def _set_language(self, key: str) -> None:
        if (self.cfg.get("language") or "auto") == key:
            return
        self.cfg.set("language", key)
        self._check_group(self.lang_items, key)
        if rumps.alert(APP_NAME, T("The new language is used after Mirrorway restarts. Restart now?"),
                       ok=T("Restart"), cancel=T("Later")) == 1:
            if U.spawn_restart():
                self.quit_app(None)

    def _toggle_audio(self, sender):
        val = not bool(self.cfg.get("audio"))
        self.cfg.set("audio", val)
        sender.state = int(val)
        threading.Thread(target=self.server.send_audio_state, daemon=True, name="mirrorway-audio-state").start()

    def _toggle_audio_mute(self, sender):
        val = not bool(self.cfg.get("audio_mute_mac", True))
        self.cfg.set("audio_mute_mac", val)
        sender.state = int(val)
        threading.Thread(target=self.server.audio_mute_changed, daemon=True, name="mirrorway-audio-mute").start()

    @staticmethod
    def _audio_text(a, connected: bool) -> str:
        if not a or not a.get("enabled"):
            return T("Status: off")
        if a.get("error"):
            return T("Status: ⚠︎ {error}").format(error=a["error"][:70])
        if a.get("active"):
            return T("Status: playing on Windows")
        if connected and not a.get("supported"):
            return T("Status: available once Mirrorway on Windows is updated")
        return T("Status: starts when Windows connects") if not connected else T("Status: starting…")

    def _toggle_altgr(self, sender):
        val = "alt" if self.cfg.get("left_option") == "altgr" else "altgr"
        self.cfg.set("left_option", val)
        sender.state = int(val == "altgr")
        self.server.router.reload_settings()

    def _toggle_login(self, sender):
        try:
            if loginitem.is_enabled():
                loginitem.disable()
            else:
                loginitem.enable(self.bundle)
        except Exception as exc:
            rumps.alert(APP_NAME, T("Could not change the setting: {error}").format(error=exc))
        sender.state = int(loginitem.is_enabled())

    def _toggle_adaptive(self, sender):
        val = not bool(self.cfg.get("adaptive_quality", True))
        self.cfg.set("adaptive_quality", val)
        sender.state = int(val)
        self.server.restart_video()

    def _toggle_share(self, sender):
        val = not bool(self.cfg.get("screen_share"))
        self.cfg.set("screen_share", val)
        sender.state = int(val)
        if val:
            self._request_screen_permission()
        else:
            self.server.restart_video()

    # ---- pairing ---------------------------------------------------------------
    def pair_new_device(self, _sender):
        st = self.server.pairing_status() or None
        if st is None:
            self.server.start_pairing()
            st = self.server.pairing_status()
        if st is None:
            return
        rumps.alert(APP_NAME, T("Open Mirrorway's settings on Windows and enter this code:\n\n{code}\n\n"
                                "The code is valid for {time} minutes and works once. "
                                "The device appears in the menu once pairing is complete.").format(
            code=st["display"], time=f"{st['remaining'] // 60}:{st['remaining'] % 60:02d}"))

    # ---- updates ---------------------------------------------------------------------
    def _bg(self, fn, name):
        threading.Thread(target=fn, daemon=True, name=name).start()

    def check_updates(self, _sender=None):
        self._bg(self.updater.check, "mirrorway-upd-check")

    def install_update(self, _sender=None):
        info = self.updater.info
        if info is None or self.updater.state != "available":
            rumps.alert(APP_NAME, T("There is no new version to install. Try 'Check for Updates' first."))
            return
        notes = (info.manifest.get("notes") or info.notes or "").strip()
        msg = T("Download and install Mirrorway {version}? Mirrorway restarts; Windows is updated too "
                "when it connects.").format(version=info.version)
        if notes:
            msg += "\n\n" + T("What's new:") + "\n" + notes[:900]
        if rumps.alert(APP_NAME, msg, ok=T("Install and Restart"), cancel=T("Cancel")) != 1:
            return
        self._bg(self._install_and_restart, "mirrorway-upd-install")

    def _install_and_restart(self):
        if self.updater.install_available() == "installed":
            self._restart_pending = True          # restarted on the menu thread (_tick)

    def _toggle_auto_update(self, sender):
        sender.state = 0 if sender.state else 1
        self.cfg.set("auto_update_check", bool(sender.state))

    def rollback_update(self, _sender=None):
        prev = U.previous_version()
        if prev is None:
            rumps.alert(APP_NAME, T("There is no previous version to return to."))
            return
        if rumps.alert(APP_NAME, T("Return to the previous version ({version})? Mirrorway restarts.").format(
                version=prev), ok=T("Return and Restart"), cancel=T("Cancel")) != 1:
            return
        try:
            U.rollback()
        except Exception as exc:
            rumps.alert(APP_NAME, T("Could not return: {error}").format(error=exc))
            return
        self._restart_pending = True

    def _restart_after_update(self):
        self._restart_pending = False
        if U.spawn_restart():
            log.info("Restarting after the update")
            self.quit_app(None)
        else:
            rumps.alert(APP_NAME, T("The update is installed. Quit and reopen Mirrorway for it to take effect."))

    def _refresh_update_menu(self) -> None:
        u = self.updater
        info = u.info if u.state == "available" else None
        self.mi_upd_state.title = u.text or f"Mirrorway {__version__}"
        self.mi_upd_install.title = (T("Install {version} and Restart").format(version=info.version) if info
                                     else T("No new version"))
        self.mi_upd_check.title = T("Checking…") if u.busy else T("Check for Updates")
        prev = U.previous_version()
        self.mi_upd_rollback.title = (T("Return to the Previous Version ({version})").format(version=prev) if prev
                                      else T("Return to the Previous Version"))
        self.m_update.title = T("Update Available: {version} ●").format(version=info.version) if info else T("Updates")
        if info and self.cfg.get("update_notified") != info.version:
            self.cfg.set("update_notified", info.version)
            from .notify import notify
            notify(APP_NAME, T("You can install it from Mirrorway › Updates."),
                   subtitle=T("Mirrorway {version} is ready").format(version=info.version))

    def _set_title(self, suffix: str) -> None:
        """State marks next to the menu bar icon ("•" connected, " Win" controlling Windows, "!" needs permission)."""
        title = suffix if self._has_icon else "⇆" + suffix
        if self.title != (title or None):
            self.title = title or None

    # ---- diagnostics ------------------------------------------------------------------------
    def make_diag(self, _sender=None):
        from pathlib import Path
        from .. import diag
        try:
            out = diag.default_output_dir() / diag.bundle_name("mac")
            diag.make_bundle(out, diag.log_files(Path(CONFIG_DIR), "mirrorway-mac"),
                             self.cfg.as_dict() if hasattr(self.cfg, "as_dict") else {},
                             status=self.server.status(),
                             extra={"permissions": {"tap": getattr(self.tap, "error", None) if self.tap else None},
                                    "update": {"state": self.updater.state, "text": self.updater.text,
                                               "previous_version": U.previous_version()}})
            log.info("Diagnostics bundle created: %s", out)
            _reveal(out)
            return out
        except Exception as exc:
            log.exception("Could not create the diagnostics bundle")
            rumps.alert(APP_NAME, T("Could not create the diagnostics bundle: {error}").format(error=exc))
            return None

    # ---- network --------------------------------------------------------------------------
    def _trust_network(self, _sender):
        n = self.server.trust_current_network()
        if n is None:
            rumps.alert(APP_NAME, T("This network could not be recognised (no gateway found). "
                                    "Check the Wi-Fi or Ethernet connection."))

    def _toggle_netlock(self, sender):
        val = not bool(self.cfg.get("network_lock", True))
        self.cfg.set("network_lock", val)
        sender.state = int(val)
        self.server.apply_network(self.server.netlock.refresh())

    def _reset_networks(self, _sender):
        if rumps.alert(APP_NAME, T("Delete the list of trusted networks? The current network is saved as the "
                                   "home network again."), ok=T("Reset"), cancel=T("Cancel")) == 1:
            self.server.netlock.forget_all()
            self.server.apply_network(True)

    def _device_menu_sig(self, st: dict):
        return (tuple((d["id"], d.get("name"), d.get("pending")) for d in st.get("devices") or []),
                st.get("client_device"))

    def _refresh_devices(self, st: dict) -> None:
        sig = self._device_menu_sig(st)
        if sig == self._devices_sig:
            return
        self._devices_sig = sig
        devs = st.get("devices") or []
        self.m_devices.title = T("Paired Devices ({n})").format(n=len(devs))
        try:
            self.m_devices.clear()
        except Exception:
            pass
        if not devs:
            self.m_devices.add(rumps.MenuItem(T("No paired devices")))
            return
        for d in devs:
            state = " — " + T("connected") if d["id"] == st.get("client_device") else ""
            if d.get("pending"):
                state = " — " + T("verifying")
            title = f"{d.get('name')} · {d['id']}{state}"
            self.m_devices.add(rumps.MenuItem(title, callback=lambda _s, dev=d: self._remove_device(dev)))
        self.m_devices.add(rumps.separator)
        self.m_devices.add(rumps.MenuItem(T("Click a device to remove its pairing")))

    @staticmethod
    def _files_text(f) -> str:
        if not f:
            return T("File transfer: none yet")
        out = f["dir"] == "out"
        if f["state"] == "running":
            from ..filexfer import human_size
            text = T("File transfer: to Windows {percent}% ({done} / {total})") if out else \
                T("File transfer: from Windows {percent}% ({done} / {total})")
            return text.format(percent=f["percent"], done=human_size(f["done"]), total=human_size(f["total"]))
        if f["state"] == "done":
            text = T("File transfer: to Windows, done — {summary}") if out else \
                T("File transfer: from Windows, done — {summary}")
            return text.format(summary=f["summary"])
        text = T("File transfer: to Windows, interrupted — {error}") if out else \
            T("File transfer: from Windows, interrupted — {error}")
        return text.format(error=f["error"][:60])

    # ---- second screen ---------------------------------------------------------------------------
    def _extend_state_text(self, st: dict) -> str:
        ex = st.get("extend") or {}
        if ex.get("error"):
            return "⚠︎ " + ex["error"]
        if ex.get("active") and ex.get("size"):
            w, h = ex["size"]
            return T("On — a {w}×{h} virtual display on the Mac").format(w=w, h=h)
        if ex.get("desired"):
            return T("Selected — starts when the screen window opens on Windows")
        if st.get("connected") and not st.get("extend_supported"):
            return T("Available once Mirrorway on Windows is updated")
        return T("One of the Windows monitors becomes a second screen for the Mac")

    def _refresh_extend(self, st: dict) -> None:
        choices = self.server.extend.choices()
        selected = self.cfg.get("extend_monitor")
        state = self._extend_state_text(st)
        sig = (tuple((label, tuple(v) if v else None) for label, v in choices),
               tuple(selected) if isinstance(selected, list) else selected, state)
        if sig == self._extend_sig:
            return
        self._extend_sig = sig
        ex = st.get("extend") or {}
        self.m_extend.title = T("Second Screen (Windows monitor)") + (" ✓" if ex.get("active") else "")
        try:
            self.m_extend.clear()
        except Exception:
            pass
        self.m_extend.add(rumps.MenuItem(state))
        self.m_extend.add(rumps.separator)
        for label, value in choices:
            it = rumps.MenuItem(label, callback=lambda _s, v=value: self._set_extend(v))
            if value is None:
                it.state = int(not selected)
            else:
                it.state = int(isinstance(selected, list) and list(selected) == list(value))
            self.m_extend.add(it)
        if len(choices) <= 1:
            self.m_extend.add(rumps.MenuItem(T("(the monitors appear once Windows connects)")))

    def _set_extend(self, value) -> None:
        self.cfg.set("extend_monitor", list(value) if value else None)
        self._extend_sig = None
        threading.Thread(target=self.server.extend.changed, daemon=True, name="mirrorway-extend").start()

    def _remove_device(self, dev: dict) -> None:
        if rumps.alert(APP_NAME, T("Remove the pairing with '{name}' ({id})?\n\nThis device will no longer be able "
                                   "to connect; to use it again, pair it with a new code.").format(
                name=dev.get("name"), id=dev["id"]), ok=T("Remove"), cancel=T("Cancel")) == 1:
            self.server.remove_device(dev["id"])
            self._devices_sig = None

    def copy_ip(self, _sender):
        self.clip_backend.copy(self.server.status()["ip"])

    def switch_to_windows(self, _sender):
        if not self.server.router.enter_center():
            rumps.alert(APP_NAME, T("Windows is not connected. Start Mirrorway on Windows and enter the pairing code."))

    def wake_windows(self, _sender):
        if self.server.connected():
            rumps.alert(APP_NAME, T("Windows is already on and connected."))
            return
        if self.server.wol_target() is None:
            rumps.alert(APP_NAME, T("The address of the Windows network adapter is not known yet.\n\n"
                                    "It is saved the first time Windows connects to Mirrorway; "
                                    "after that you can wake it from this menu."))
            return
        self.wake.start()
        self._tick(None)

    def _wake_help(self) -> str:
        t = self.server.wol_target() or {}
        notes = wol_warnings(t)
        lines = [f"• {n}" for n in notes] or [
            "• " + T("On Windows, in Device Manager > Network adapters > the adapter > Power Management, 'Allow this "
                     "device to wake the computer' and 'Only allow a magic packet…' must be on"),
            "• " + T("On the Advanced tab, 'Wake on Magic Packet' must be Enabled"),
            "• " + T("If it was shut down completely: turn off 'Fast startup' in Control Panel > Power Options "
                     "and enable Wake-on-LAN in the BIOS"),
        ]
        return (T("Windows did not connect to Mirrorway within {seconds} seconds ({mac}, {ip}).").format(
            seconds=int(self.wake.TIMEOUT), mac=t.get("mac", "?"), ip=t.get("ip", "?")) + "\n\n" + "\n".join(lines)
            + "\n\n" + T("If the computer turned on but nobody has signed in to Windows yet, Mirrorway connects "
                         "after sign-in."))

    def _wake_title(self, connected: bool) -> str:
        state = self.wake.tick(connected)
        now = time.monotonic()
        if state == "waking":
            return T("Waking Windows… ({seconds} s)").format(seconds=self.wake.remaining())
        if state == "woke":
            self.wake.state = "idle"
            self._wake_msg_until = now + 20
            self._wake_msg = T("Windows woke up ✓ ({seconds} s)").format(seconds=f"{self.wake.elapsed:.0f}")
        elif state == "timeout":
            self.wake.state = "idle"
            self._wake_msg_until = now + 60
            self._wake_msg = T("Windows did not wake up (Windows settings may be needed)")
            rumps.alert(APP_NAME, self._wake_help())
        if now < self._wake_msg_until:
            return self._wake_msg
        t = self.server.wol_target()
        if connected:
            return T("Wake Windows (Windows is on)")
        if t is None:
            return T("Wake Windows (connect once first)")
        return T("Wake Windows ({name})").format(name=t.get("name") or t.get("ip"))

    def quit_app(self, _sender):
        try:
            if self.tap is not None:
                self.tap.stop()
            self.server.stop()
        finally:
            rumps.quit_application()

    # ---- permissions / startup ----------------------------------------------
    def _request_screen_permission(self) -> None:
        try:
            import Quartz
            if not Quartz.CGPreflightScreenCaptureAccess():
                Quartz.CGRequestScreenCaptureAccess()
        except Exception:
            pass

    def _start_tap(self) -> None:
        if self.tap is not None and self.tap.is_alive():
            return
        self.tap = EventTap(self.server.router)
        self.tap.start()

    def start(self) -> None:
        port = int(self.cfg.get("port"))
        try:
            self.server.start()
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                rumps.alert(APP_NAME, T("Could not open port {port}: {error}").format(port=port, error=exc))
                raise SystemExit(1)
            others = other_instances(port)
            if not others:
                rumps.alert(APP_NAME, T("Port {port} is in use by another app.").format(port=port))
                raise SystemExit(1)
            choice = rumps.alert(
                APP_NAME,
                T("Mirrorway (or the old Köprü) is already running (the Mirrorway icon in the menu bar).\n\n"
                  "On MacBooks with a full menu bar the icon may hide behind the camera notch.\n\n"
                  "Quit the running one and start this one? (Needed after granting permissions.)"),
                ok=T("Restart"), cancel=T("Cancel"))
            if choice != 1:
                raise SystemExit(0)
            stop_processes(others)
            try:
                self.server.start()
            except OSError as exc2:
                rumps.alert(APP_NAME, T("Port {port} is still in use: {error}").format(port=port, error=exc2))
                raise SystemExit(1)
        request_input_monitoring()
        accessibility_trusted(prompt=True)
        if self.cfg.get("screen_share"):
            self._request_screen_permission()
        self._start_tap()
        self._timer = rumps.Timer(self._tick, 1.0)
        self._timer.start()
        try:  # keep updating while the menu is open (rumps adds the timer to the default mode only)
            from Foundation import NSRunLoop, NSRunLoopCommonModes
            NSRunLoop.currentRunLoop().addTimer_forMode_(self._timer._nstimer, NSRunLoopCommonModes)
        except Exception:
            log.debug("Could not add the timer to the common modes", exc_info=True)

    def _permission_text(self) -> str:
        who = "'Mirrorway'" if self.bundle else T("the app you started Mirrorway from (e.g. Terminal)")
        return T("In System Settings > Privacy & Security, turn on {who} in both the 'Accessibility' and the "
                 "'Input Monitoring' list. If it is not listed, add it with the + button. "
                 "After granting permission you may need to quit and reopen Mirrorway.").format(who=who)

    @staticmethod
    def _stats_text(st: dict) -> str:
        if not st.get("connected"):
            return T("Measurements: —")
        parts = []
        if st.get("rtt_ms") is not None:
            parts.append(T("latency {ms} ms").format(ms=f"{st['rtt_ms']:.0f}"))
        if st.get("video"):
            codec = "H.264" if st.get("video_codec") == "h264" else "JPEG"
            if st.get("video_codec") == "h264" and "software" in str(st.get("video_encoder", "")):
                codec += " (" + T("software") + ")"
            if st.get("video_codec") == "h264" and st.get("video_target_kbps"):
                codec += f" {st['video_target_kbps'] / 1000:.0f} Mbit/s"
            parts.append(T("screen {codec} {fps} fps, {mbs} MB/s, encoding {ms} ms").format(
                codec=codec, fps=f"{st['video_fps']:.0f}", mbs=f"{st['video_kbps'] / 1024:.1f}",
                ms=f"{st['video_enc_ms']:.0f}"))
            cs = st.get("client_stats") or {}
            if cs.get("latency_ms"):
                parts.append(T("picture latency ≈ {ms} ms").format(ms=f"{cs['latency_ms']:.0f}"))
        return T("Measurements: {text}").format(text=" · ".join(parts) if parts else T("waiting…"))

    def _tick(self, _timer) -> None:
        if self._restart_pending:
            self._restart_after_update()
            return
        if self.updater.due():
            self._bg(self.updater.check, "mirrorway-upd-check")
        self._refresh_update_menu()
        # If the event tap is waiting for permission, try again
        if self.tap is not None and not self.tap.is_alive() and self.tap.error == "permission":
            if not self._perm_alert_shown:
                self._perm_alert_shown = True
                rumps.alert(APP_NAME, T("Permission is needed to send the keyboard and mouse to Windows.")
                            + "\n\n" + self._permission_text())
                _open(PRIVACY_URLS["accessibility"])
            self._start_tap()
        st = self.server.status()
        if self.tap is not None and self.tap.error == "permission" and not self.tap.is_alive():
            status = T("⚠︎ Permission needed (Accessibility + Input Monitoring)")
            self._set_title("!")
        elif st["connected"]:
            ver = f" ({st['client_version']})" if st.get("client_version") else ""
            text = T("● You are controlling {name}{version}") if st["remote"] else T("● {name}{version} connected")
            status = text.format(name=st["client_name"], version=ver)
            self._set_title(" Win" if st["remote"] else "•")
        else:
            status = T("○ Waiting for Windows…")
            if st["error"]:
                status += f"  ({st['error']})"
            self._set_title("")
        if not st["connected"] and st.get("vpn"):
            status += "  — " + T("VPN is on: Windows may not be able to connect")
        net = st.get("network") or {}
        if not net.get("allowed", True):
            status = T("⏸ Unknown network — Mirrorway is off on this network (Mirrorway › Network)")
            self._set_title("⏸")
        self.mi_net_state.title = net.get("text") or T("Network could not be determined")
        state = net.get("state")
        if state == "trusted":
            self.m_net.title = T("Network: home network ✓") + (" " + T("(weak recognition)") if net.get("weak") else "")
        elif state == "off":
            self.m_net.title = T("Network: restriction off")
        elif state == "untrusted":
            self.m_net.title = T("Network: unknown network ⏸")
        else:
            self.m_net.title = T("Network: undetermined")
        self.mi_status.title = status
        self.mi_stats.title = self._stats_text(st)
        p = st.get("pairing")
        self.mi_code.title = (T("Pairing code: {code}  ({time})").format(
            code=p["display"], time=f"{p['remaining'] // 60}:{p['remaining'] % 60:02d}") if p
            else T("Pair a New Device…"))
        self._refresh_devices(st)
        self._refresh_extend(st)
        self.mi_files_state.title = self._files_text(st.get("files"))
        self.mi_audio_state.title = self._audio_text(st.get("audio"), bool(st.get("connected")))
        ev = self.server.file_event
        if ev is not None:
            self.server.file_event = None
            from .notify import notify
            notify(APP_NAME, ev.get("msg", ""), subtitle=ev.get("title", ""))
        if self.server.last_paired is not None:
            dev, self.server.last_paired = self.server.last_paired, None
            rumps.alert(APP_NAME, T("'{name}' is paired (fingerprint {id}).").format(name=dev.get("name"), id=dev["id"]))
        self.mi_wake.title = self._wake_title(st["connected"])
        if st["connected"]:
            self.mi_auto_speed.title = T("Automatic speed: {scale}× screen ratio (total {total}×)").format(
                scale=f"{st['auto_scale']:.2f}", total=f"{st['effective_speed']:.2f}")
        extra = f"  (+{', '.join(st['all_ips'][1:])})" if len(st.get("all_ips", [])) > 1 else ""
        self.mi_ip.title = T("This Mac: {address}{extra}  (copy)").format(address=f"{st['ip']}:{st['port']}", extra=extra)


def run(cfg) -> None:
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().setActivationPolicy_(1)  # no Dock icon
    except Exception:
        pass
    app = MirrorwayMacApp(cfg)
    app.start()
    app.run()
