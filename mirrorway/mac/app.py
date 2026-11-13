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
from .. import prefs, quick
from ..config import CONFIG_DIR
from ..extend import choose_monitor
from ..i18n import T
from ..prefs import SPEEDS, codecs, layouts, qualities, sides
from ..wol import WakeSession
from . import loginitem
from . import selfupdate as U
from .capture import list_displays
from .clipboard import MacClipboard
from .server import MirrorwayServer, wol_warnings
from .tap import EventTap, MacPlatform, accessibility_trusted, request_input_monitoring

log = logging.getLogger("mirrorway.app")

ARROWS = {"right": "→", "left": "←", "top": "↑", "bottom": "↓"}

PRIVACY_URLS = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "input": "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
}


def _open(url: str) -> None:
    subprocess.Popen(["open", url])


def _main_thread(fn, *args) -> None:
    """Run fn on the main (menu) thread soon; directly where there is no Cocoa run loop (tests)."""
    try:
        from PyObjCTools import AppHelper
    except ImportError:
        fn(*args)
        return
    AppHelper.callAfter(fn, *args)


def running_apps() -> list:
    """The open apps (those with a Dock icon), as [(bundle id, name)], without Mirrorway itself."""
    try:
        from AppKit import NSWorkspace
        out = {}
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            bid = app.bundleIdentifier()
            if app.activationPolicy() != 0 or not bid or str(bid).startswith("app.mirrorway"):
                continue
            out[str(bid)] = str(app.localizedName() or bid)
        return sorted(out.items(), key=lambda kv: kv[1].lower())
    except Exception:
        return []


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
        # quick panel (left click on the icon); the classic menu opens with a right click
        self.panel = None
        self._panel_tried = False
        self._ns_menu = None
        self._status_target = None
        self.settings_win = None
        self.server.router.on_action = self._on_shortcut
        self.ring = None
        self.server.router.on_return = lambda x, y: _main_thread(self._show_ring, x, y)
        self._lock_observer = None
        self.server.lock_mac = lambda: _main_thread(self._lock_the_mac)
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
        self.route_items = {}                   # where sound plays (3.4), one of three
        for route, title in (("own", T("Each Computer Plays Its Own Sound")), ("windows", T("All Sound on Windows")),
                             ("mac", T("All Sound on the Mac"))):
            self.route_items[route] = rumps.MenuItem(title, callback=self._make_route(route))
            self.m_audio.add(self.route_items[route])
        self._check_routes()
        self.m_audio.add(None)
        self.mi_audio_mute = rumps.MenuItem(T("Mute the Mac's Speakers Meanwhile"), callback=self._toggle_audio_mute)
        self.mi_audio_mute.state = int(bool(cfg.get("audio_mute_mac", True)))
        self.mi_audio_state = rumps.MenuItem(T("Status: —"))
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
        self._displays = []
        for d in displays:
            label = T("Display {n} ({w}×{h})").format(n=d["index"] + 1, w=d["w"], h=d["h"])
            if d.get("main"):
                label += " — " + T("main")
            self._displays.append((d["index"], label))
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
        try:
            if loginitem.follow(self.bundle):
                log.info("The login item now opens %s", self.bundle)
        except Exception:
            log.exception("Could not update the login item")
        if self.bundle and not cfg.get("login_item_set"):
            try:
                loginitem.enable(self.bundle)
            except Exception:
                log.exception("Could not add the login item")
            cfg.set("login_item_set", True)
        self.mi_login.state = int(bool(self.bundle) and loginitem.is_enabled())
        self.mi_panel = rumps.MenuItem(T("Click Opens the Quick Panel"), callback=self._toggle_quick_panel)
        self.mi_panel.state = int(bool(cfg.get("quick_panel", True)))

        # Language
        self.m_lang = rumps.MenuItem(T("Language"))
        self.lang_items = {}
        for key, label in prefs.languages():
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
            rumps.MenuItem(T("Settings…"), callback=lambda _s: self.open_settings(), key=","),
            self.m_update,
            self.mi_login,
            self.mi_panel,
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

    def _toggle_audio(self, sender=None):
        """The panel tile and ⌃⌥A: sharing off <-> the last direction (all on Windows by default)."""
        self.server.toggle_audio("on the Mac")
        self._check_routes()

    def _make_route(self, route: str):
        def cb(sender):
            self.server.set_audio_route(route, "in the menu")
            self._check_routes()
        return cb

    def _check_routes(self) -> None:
        from .. import prefs
        now = prefs.mac_audio_route(self.cfg)
        for route, item in self.route_items.items():
            item.state = int(route == now)

    def _toggle_audio_mute(self, sender):
        val = not bool(self.cfg.get("audio_mute_mac", True))
        self.cfg.set("audio_mute_mac", val)
        sender.state = int(val)
        threading.Thread(target=self.server.audio_mute_changed, daemon=True, name="mirrorway-audio-mute").start()

    @staticmethod
    def _audio_text(a, connected: bool) -> str:
        route = (a or {}).get("route") or ("windows" if (a or {}).get("enabled") else "own")
        if route == "own":
            return T("Status: each computer plays its own sound")
        if route == "mac":
            if a.get("from_windows_error"):
                return T("Status: ⚠︎ {error}").format(error=a["from_windows_error"][:70])
            if a.get("from_windows"):
                return T("Status: Windows' sound plays on the Mac")
            if connected and not a.get("from_windows_supported"):
                return T("Status: available once Mirrorway on Windows is updated")
            return T("Status: starts when Windows connects") if not connected else T("Status: starting…")
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
        self._set_login(not loginitem.is_enabled())
        sender.state = int(loginitem.is_enabled())

    def _set_login(self, on: bool) -> None:
        try:
            if on and not loginitem.is_enabled():
                loginitem.enable(self.bundle)
            elif not on and loginitem.is_enabled():
                loginitem.disable()
        except Exception as exc:
            rumps.alert(APP_NAME, T("Could not change the setting: {error}").format(error=exc))

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
        if suffix in ("•", " Win") and not self.cfg.get("menubar_state", True):
            suffix = ""
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
        if value:
            self.cfg.set("extend_last", list(value))
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

    # ---- quick panel and shortcuts ---------------------------------------------------------------
    def _on_shortcut(self, action: str) -> None:
        """Called by the router on the event tap thread (Control + Option + letter)."""
        _main_thread(self.quick_action, action)

    def quick_action(self, action: str) -> None:
        log.info("Quick shortcut: %s", action)
        try:
            if action == "sas":
                self.server.send_sas()
            elif action == "panel":
                self.toggle_panel()
            else:
                self.panel_action("tile:" + action)
        except Exception:
            log.exception("Quick shortcut %s failed", action)

    def toggle_screen(self, st=None) -> None:
        """Open or close the Mac's screen window on Windows."""
        st = st or self.server.status()
        if not st.get("connected"):
            return
        if st.get("video"):
            self.server.request_viewer(False)
            return
        if not self.cfg.get("screen_share"):
            self.cfg.set("screen_share", True)
            self.mi_share.state = 1
            self._request_screen_permission()
        self.server.request_viewer(True)

    def toggle_second_screen(self, st=None) -> None:
        """Turn the second screen off, or on with the monitor chosen last (or the best one)."""
        st = st or self.server.status()
        if (st.get("extend") or {}).get("desired"):
            self._set_extend(None)
            return
        mons = self.server.extend.win_monitors() or list(self.cfg.get("win_monitors") or [])
        if not mons:
            return
        last = self.cfg.get("extend_last")
        i = choose_monitor(mons, last) if last else None
        if i is None:
            i = choose_monitor(mons, "auto")
        if i is not None:
            self._set_extend(mons[i])

    def _toggle_setting(self, key: str, item, default=False) -> None:
        val = not bool(self.cfg.get(key, default))
        self.cfg.set(key, val)
        item.state = int(val)

    def panel_action(self, key: str) -> None:
        """A click in the panel (or a quick shortcut, as "tile:<name>")."""
        st = self.server.status()
        if key == "primary":
            self.close_panel()
            if self.server.router.remote:
                self.server.router.leave_to_mac()
            else:
                self.switch_to_windows(None)
        elif key == "tile:screen":
            self.toggle_screen(st)
        elif key == "tile:second":
            self.toggle_second_screen(st)
        elif key == "tile:audio":
            self._toggle_audio()
        elif key == "tile:clipboard":
            self._toggle_setting("clipboard", self.mi_clip)
        elif key == "tile:files":
            self._toggle_setting("file_clipboard", self.mi_files, True)
        elif key == "tile:wake":
            if not st.get("connected") and self.server.wol_target() is not None and self.wake.state != "waking":
                self.wake_windows(None)
        elif key == "row:pair":
            self.close_panel()
            self.pair_new_device(None)
        elif key == "row:network":
            self._network_popup(st)
        elif key == "row:disconnect":
            self.server.disconnect_all()
        elif key in ("settings", "footer:settings"):
            self.close_panel()
            _main_thread(self.open_settings)
        elif key == "footer:update":
            self.close_panel()
            self.install_update()
        elif key == "footer:quit":
            self.quit_app(None)
            return
        self.refresh_panel()

    def _network_popup(self, st: dict) -> None:
        if self.panel is None:
            return
        net = st.get("network") or {}
        self.panel.popup("row:network", [
            (net.get("text") or T("Network could not be determined"), None, False),
            None,
            (T("Trust This Network"), lambda: self._trust_network(None), False),
            (T("Work Only on the Home Network"), lambda: self._toggle_netlock(self.mi_net_lock),
             bool(self.cfg.get("network_lock", True))),
            (T("Reset Trusted Networks (keep only this one)"), lambda: self._reset_networks(None), False),
        ])

    def panel_model(self, st: dict) -> "quick.Panel":
        tap = self.tap
        permission = not (tap is not None and tap.error == "permission" and not tap.is_alive())
        waking = self.wake.remaining() if self.wake.state == "waking" else None
        u = self.updater
        update = u.info.version if (u.state == "available" and u.info is not None) else None
        return quick.mac_panel(st, self.cfg, permission=permission, waking=waking, update=update)

    def refresh_panel(self, st=None) -> None:
        if self.panel is None:
            return
        try:
            self.panel.update(self.panel_model(st or self.server.status()))
        except Exception:
            log.exception("Could not update the panel")

    def close_panel(self) -> None:
        if self.panel is not None:
            self.panel.close()

    def _status_item(self):
        return getattr(getattr(self, "_nsapp", None), "nsstatusitem", None)

    def _install_panel(self) -> None:
        """Left click on the icon opens the panel, right click (or Control-click) the classic menu."""
        if self._panel_tried or not self.cfg.get("quick_panel", True):
            return
        item = self._status_item()
        if item is None:
            return
        self._panel_tried = True
        try:
            from AppKit import NSEventMaskLeftMouseUp, NSEventMaskRightMouseUp
            from .panel import QuickPanel, make_target
            panel = QuickPanel(self.panel_action)
            target = make_target(click=lambda _tag: self._status_clicked(), menu_closed=self._menu_closed)
            menu = item.menu()
            btn = item.button()
            btn.setTarget_(target)
            btn.setAction_("clicked:")
            btn.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)
            self.panel, self._ns_menu, self._status_target = panel, menu, target
            item.setMenu_(None)
            log.info("Quick panel ready")
        except Exception:
            log.exception("Could not set up the quick panel; the menu stays")
            self.panel = None

    def _status_clicked(self) -> None:
        from AppKit import NSApplication, NSEventModifierFlagControl, NSEventTypeRightMouseUp
        ev = NSApplication.sharedApplication().currentEvent()
        right = ev is not None and (ev.type() == NSEventTypeRightMouseUp
                                    or bool(ev.modifierFlags() & NSEventModifierFlagControl))
        if right or self.panel is None or not self.cfg.get("quick_panel", True):
            self.show_menu()
            return
        try:
            self.toggle_panel()
        except Exception:
            log.exception("Could not open the panel; showing the menu")
            self.show_menu()

    def toggle_panel(self) -> None:
        item = self._status_item()
        if item is None:
            return
        if self.panel is None or not self.cfg.get("quick_panel", True):
            self.show_menu()
            return
        if not self.panel.is_shown():
            self.server.router.leave_to_mac()          # the panel opens on the Mac's screen
            self.refresh_panel()
        self.panel.toggle(item.button())

    def show_menu(self) -> None:
        """The classic menu under the icon (right click, Settings…, or the panel turned off)."""
        item = self._status_item()
        menu = self._ns_menu or (item.menu() if item is not None else None)
        if item is None or menu is None:
            return
        self.close_panel()
        if self._status_target is not None:
            menu.setDelegate_(self._status_target)
        item.setMenu_(menu)
        item.button().performClick_(None)

    def _menu_closed(self) -> None:
        _main_thread(self._detach_menu)

    def _detach_menu(self) -> None:
        item = self._status_item()
        if item is not None and self.panel is not None and self.cfg.get("quick_panel", True):
            item.setMenu_(None)                         # the next click opens the panel again

    def _toggle_quick_panel(self, sender) -> None:
        val = not bool(self.cfg.get("quick_panel", True))
        self.cfg.set("quick_panel", val)
        sender.state = int(val)
        if val and self.panel is None:
            self._panel_tried = False
            self._install_panel()

    # ---- settings window ---------------------------------------------------------------------------
    def settings_pages(self, st=None) -> list:
        st = st or self.server.status()
        u = self.updater
        info = u.info if u.state == "available" else None
        tap = self.tap
        ctx = {
            "displays": self._displays,
            "extend_choices": self.server.extend.choices(),
            "extend_text": self._extend_state_text(st),
            "audio_text": self._audio_text(st.get("audio"), bool(st.get("connected"))),
            "audio_apps": running_apps() if self.cfg.get("audio_apps_mode") == "selected" else [],
            "files_text": self._files_text(st.get("files")),
            "login": {"available": bool(self.bundle), "enabled": bool(self.bundle) and loginitem.is_enabled()},
            "update": {"text": u.text, "busy": u.busy, "available": info.version if info else None,
                       "previous": U.previous_version()},
            "permission": not (tap is not None and tap.error == "permission" and not tap.is_alive()),
            "version": __version__,
        }
        return prefs.mac_pages(self.cfg, st, ctx)

    def open_settings(self, page=None) -> None:
        try:
            if self.settings_win is None:
                from .settings import SettingsWindow
                self.settings_win = SettingsWindow(self.apply_setting)
            self.settings_win.update(self.settings_pages())
            self.settings_win.show(page)
        except Exception:
            log.exception("Could not open the Settings window; showing the menu")
            self.show_menu()

    def _watch_lock(self) -> None:
        """The Mac's lock: control comes back from Windows; with "Lock both computers together" Windows locks too."""
        try:
            from . import lock as L
            self._lock_observer = L.watch(self.server.mac_lock_changed)
        except Exception:
            log.exception("Could not follow the Mac's lock")

    def _lock_the_mac(self) -> None:
        try:
            from . import lock as L
            log.info("Mac locked (%s)", L.lock_mac())
        except Exception:
            log.exception("Could not lock the Mac")

    def _show_ring(self, x: float, y: float) -> None:
        """Control came back from Windows: a ring where the pointer appeared (main thread)."""
        if not self.cfg.get("switch_ring", True):
            return
        try:
            if self.ring is None:
                from .ring import RingWindow
                self.ring = RingWindow()
            self.ring.show(x, y)
        except Exception:
            log.exception("Could not show the switch ring")

    def refresh_settings(self, st=None) -> None:
        w = self.settings_win
        if w is None or not w.visible:
            return
        try:
            w.update(self.settings_pages(st))
        except Exception:
            log.exception("Could not update the Settings window")

    _BOOL_SETTINGS = ("edge_switch", "auto_speed", "invert_scroll", "option_unicode", "gestures", "clipboard",
                      "file_clipboard", "auto_update_check", "menubar_state", "switch_ring", "lock_sync")

    def apply_setting(self, key: str, value) -> None:
        """A change in the Settings window (the same effect as the menu item)."""
        cfg, router = self.cfg, self.server.router
        log.info("Setting changed: %s", key)
        try:
            if key.startswith("action:"):
                self._settings_action(key[len("action:"):])
            elif key.startswith("shortcut:"):
                action = key.split(":", 1)[1]
                letters = quick.shortcut_letters(cfg)
                for other, letter in letters.items():          # a letter can belong to one shortcut only
                    if value and letter == value and other != action:
                        letters[other] = ""
                letters[action] = value or ""
                cfg.set("shortcuts", letters)
                router.reload_settings()
            elif key == "side":
                cfg.set("side", value)
                router.reload_settings()
            elif key in ("speed", "scroll_speed"):
                cfg.set(key, float(value))
            elif key in ("keyboard_layout", "swap_cmd_ctrl"):
                cfg.set(key, value)
                router.reload_settings()
            elif key == "altgr":
                cfg.set("left_option", "altgr" if value else "alt")
                router.reload_settings()
            elif key in ("display_index", "quality", "video_codec", "adaptive_quality"):
                cfg.set(key, value)
                self.server.restart_video()
            elif key == "screen_share":
                cfg.set(key, bool(value))
                if value:
                    self._request_screen_permission()
                else:
                    self.server.restart_video()
            elif key == "extend_monitor":
                self._set_extend(list(value) if value else None)
            elif key == "audio_route":
                self.server.set_audio_route(str(value), "in Settings")
                self._check_routes()
            elif key == "audio_mute_mac":
                cfg.set(key, bool(value))
                self._bg(self.server.audio_mute_changed, "mirrorway-audio-mute")
            elif key == "audio_apps_mode":
                cfg.set(key, "selected" if value == "selected" else "all")
                self.server.audio_apps_changed()
                self.refresh_settings()                        # the app list appears or goes
            elif key.startswith("audio_app:"):
                bid = key.split(":", 1)[1]
                apps = prefs.selected_audio_apps(cfg)
                if value:
                    apps[bid] = dict(running_apps()).get(bid, bid)
                else:
                    apps.pop(bid, None)
                cfg.set("audio_apps", apps)
                self.server.audio_apps_changed()
            elif key == "network_lock":
                cfg.set(key, bool(value))
                self.server.apply_network(self.server.netlock.refresh())
            elif key == "login":
                self._set_login(bool(value))
            elif key == "language":
                self._set_language(value)
            elif key == "quick_panel":
                if bool(value) != bool(cfg.get("quick_panel", True)):
                    self._toggle_quick_panel(self.mi_panel)
            elif key in self._BOOL_SETTINGS:
                cfg.set(key, bool(value))
            else:
                log.warning("Unknown setting: %s", key)
                return
        except Exception:
            log.exception("Could not apply the setting %s", key)
        self._sync_menu()
        self.refresh_settings()
        self.refresh_panel()

    def _settings_action(self, name: str) -> None:
        if name.startswith("perm:"):
            _open(PRIVACY_URLS[name[len("perm:"):]])
        elif name.startswith("remove_device:"):
            dev_id = name[len("remove_device:"):]
            dev = next((d for d in self.server.devices() if d["id"] == dev_id), None)
            if dev is not None:
                self._remove_device(dev)
        else:
            fn = {"trust_network": lambda: self._trust_network(None),
                  "reset_networks": lambda: self._reset_networks(None),
                  "copy_ip": lambda: self.copy_ip(None),
                  "pair": lambda: self.pair_new_device(None),
                  "check_updates": self.check_updates,
                  "install_update": self.install_update,
                  "rollback": self.rollback_update,
                  "diag": self.make_diag,
                  "show_log": lambda: _open(str(CONFIG_DIR))}.get(name)
            if fn is None:
                log.warning("Unknown action: %s", name)
            else:
                fn()

    def _sync_menu(self) -> None:
        """Menu check marks from the settings (after a change in the Settings window)."""
        cfg = self.cfg
        self._check_group(self.side_items, cfg.get("side"))
        self._check_group(self.speed_items, float(cfg.get("speed") or 1.0))
        self._check_group(self.scroll_items, float(cfg.get("scroll_speed") or 1.0))
        self._check_group(self.layout_items, cfg.get("keyboard_layout"))
        self._check_group(self.display_items, int(cfg.get("display_index") or 0))
        self._check_group(self.quality_items, cfg.get("quality"))
        self._check_group(self.codec_items, cfg.get("video_codec") or "h264")
        self._check_group(self.lang_items, cfg.get("language") or "auto")
        for item, key, default in (
                (self.mi_auto_speed, "auto_speed", True), (self.mi_invert, "invert_scroll", False),
                (self.mi_swap, "swap_cmd_ctrl", False), (self.mi_unicode, "option_unicode", True),
                (self.mi_edge, "edge_switch", False), (self.mi_clip, "clipboard", False),
                (self.mi_gestures, "gestures", True), (self.mi_files, "file_clipboard", True),
                (self.mi_audio_mute, "audio_mute_mac", True),
                (self.mi_share, "screen_share", False), (self.mi_adaptive, "adaptive_quality", True),
                (self.mi_upd_auto, "auto_update_check", True), (self.mi_net_lock, "network_lock", True),
                (self.mi_panel, "quick_panel", True)):
            item.state = int(bool(cfg.get(key, default)))
        self.mi_altgr.state = int(cfg.get("left_option") == "altgr")
        self.mi_login.state = int(bool(self.bundle) and loginitem.is_enabled())
        self._extend_sig = None
        self._refresh_static()

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
        if loginitem.temporary(self.bundle):
            rumps.alert(APP_NAME, T("Mirrorway is running from the disk image or the Downloads folder. Drag it into "
                                    "the Applications folder first, then open it from there."), ok=T("Quit"))
            raise SystemExit(0)
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
        self._watch_lock()
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
        self._install_panel()
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
        self._check_routes()                                         # Windows can change it too
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
        if self.panel is not None and self.panel.is_shown():
            self.refresh_panel(st)
        self.refresh_settings(st)


def run(cfg) -> None:
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().setActivationPolicy_(1)  # no Dock icon
    except Exception:
        pass
    app = MirrorwayMacApp(cfg)
    app.start()
    app.run()
