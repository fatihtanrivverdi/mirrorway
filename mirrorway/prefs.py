"""The Settings window's pages, independent of the interface toolkit (tested on every platform).

mac_pages() turns the Mac app's settings and state into pages of groups of items; mac/settings.py draws them.
An item's key says what a change does: a setting name ("edge_switch"), a quick shortcut ("shortcut:screen") or
an action ("action:check_updates"); MirrorwayMacApp.apply_setting(key, value) carries it out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from . import quick
from .i18n import NAMES as LANGUAGE_NAMES
from .i18n import T

SPEEDS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
SCROLL_SPEEDS = [0.5, 1.0, 1.5, 2.0, 3.0]


def sides():
    return [("left", T("Left")), ("right", T("Right")), ("top", T("Above")), ("bottom", T("Below"))]


def qualities():
    return [("low", T("Low (smooth, little bandwidth)")), ("medium", T("Medium")), ("high", T("High (sharp text)"))]


def codecs():
    return [("h264", T("H.264 — smooth (recommended)")), ("jpeg", T("JPEG — sharpest text"))]


def layouts():
    return [("auto", T("Automatic")), ("iso", T("ISO (Turkish/European)")), ("ansi", T("ANSI (US)"))]


def languages():
    return [("auto", T("Automatic (system)"))] + list(LANGUAGE_NAMES.items())


@dataclass
class Item:
    kind: str                     # switch, segmented, popup, button, text, keys
    key: str
    title: str
    value: Any = None
    options: Sequence[Tuple[Any, str]] = ()
    detail: str = ""
    enabled: bool = True
    button: str = ""              # kind "button": the button's title
    tone: str = ""                # "ok" / "warn": colour of the detail

    def signature(self) -> tuple:
        """What decides the controls' layout (a change rebuilds the page; other changes update in place)."""
        return (self.kind, self.key, bool(self.detail), tuple(str(lab) for _v, lab in self.options))


@dataclass
class Group:
    title: str
    items: List[Item]
    note: str = ""


@dataclass
class Page:
    id: str
    title: str
    symbol: str                   # SF Symbol on the Mac
    color: str                    # the icon's background in the sidebar
    groups: List[Group] = field(default_factory=list)

    def items(self) -> List[Item]:
        return [it for g in self.groups for it in g.items]

    def item(self, key: str) -> Item:
        return next(it for it in self.items() if it.key == key)

    def signature(self) -> tuple:
        return tuple((g.title, bool(g.note), tuple(it.signature() for it in g.items)) for g in self.groups)


def _option_index(options, value) -> Optional[int]:
    for i, (v, _label) in enumerate(options):
        if v == value or (isinstance(v, float) and isinstance(value, (int, float)) and abs(v - value) < 1e-9):
            return i
    return None


def selected(item: Item) -> Optional[int]:
    """Index of the item's value among its options (None if it is not one of them)."""
    return _option_index(item.options, item.value)


def _general(cfg, ctx) -> Page:
    login = ctx.get("login") or {}
    keys = quick.shortcut_letters(cfg)
    side = cfg.get("side") if cfg.get("side") in quick.ARROWS else "right"
    to_win, to_mac = quick.ARROWS[side]
    letters = [("", T("Off"))] + [(c, quick.mac_shortcut(c)) for c in sorted(quick.MAC_KEYCODES)]
    items = [
        Item("segmented", "side", T("Windows is on this side of the screen"), side, sides()),
        Item("switch", "edge_switch", T("Switch at the screen edge"), bool(cfg.get("edge_switch"))),
        Item("switch", "switch_ring", T("Show a ring where the pointer appears"), bool(cfg.get("switch_ring", True)),
             detail=T("For half a second after switching computers; set separately on Windows")),
        Item("switch", "lock_sync", T("Lock both computers together"), bool(cfg.get("lock_sync", False)),
             detail=T("When the Mac locks, Windows locks too, and the other way round; unlock each as usual")),
        Item("switch", "login", T("Open at login"), bool(login.get("enabled")),
             detail="" if login.get("available") else T("Only when Mirrorway runs as Mirrorway.app"),
             enabled=bool(login.get("available"))),
        Item("switch", "menubar_state", T("Show the state next to the menu bar icon"),
             bool(cfg.get("menubar_state", True))),
        Item("switch", "quick_panel", T("Clicking the icon opens the quick panel"), bool(cfg.get("quick_panel", True)),
             detail=T("Otherwise the menu opens; right-click always opens the menu")),
        Item("popup", "language", T("Language"), cfg.get("language") or "auto", languages()),
    ]
    shortcuts = [
        Item("keys", "keys:to_win", T("Switch to Windows"), "⌃⌥" + to_win),
        Item("keys", "keys:to_mac", T("Back to the Mac"), "⌃⌥" + to_mac),
        Item("popup", "shortcut:screen", T("Mac screen on Windows"), keys["screen"], letters),
        Item("popup", "shortcut:second", T("Second screen on or off"), keys["second"], letters),
        Item("popup", "shortcut:audio", T("Audio on Windows on or off"), keys["audio"], letters),
        Item("popup", "shortcut:panel", T("Open the Mirrorway panel"), keys["panel"], letters),
        Item("keys", "keys:sas", T("Ctrl+Alt+Del on Windows"), "⌃⌥⌫",
             detail=T("Needs lock screen support on Windows (its Settings › General)")),
    ]
    perm_ok = ctx.get("permission", True)
    perms = [Item("button", "action:perm:" + k, label, button=T("Open…")) for k, label in
             (("accessibility", T("Accessibility")), ("input", T("Input Monitoring")),
              ("screen", T("Screen Recording")))]
    return Page("general", T("General"), "gearshape", "#5B3FD6", [
        Group("", items),
        Group(T("Quick shortcuts"), shortcuts,
              T("Shortcuts work everywhere while Mirrorway runs, also while you control Windows. "
                "The arrows follow the side of the screen.")),
        Group(T("Permissions"), perms,
              "" if perm_ok else T("Mirrorway needs Accessibility and Input Monitoring to send the keyboard and "
                                   "mouse to Windows. Turn it on in both lists, then restart Mirrorway.")),
    ])


def _keyboard(cfg, st) -> Page:
    speeds = [(s, f"{s:g}×") for s in SPEEDS]
    scrolls = [(s, f"{s:g}×") for s in SCROLL_SPEEDS]
    auto_detail = ""
    if st.get("connected") and st.get("auto_scale"):
        auto_detail = T("{scale}× screen ratio (total {total}×)").format(
            scale=f"{st['auto_scale']:.2f}", total=f"{st.get('effective_speed', 1.0):.2f}")
    return Page("keyboard", T("Keyboard and Mouse"), "keyboard", "#3B6FD8", [
        Group(T("Pointer"), [
            Item("switch", "auto_speed", T("Automatic speed (screen ratio)"), bool(cfg.get("auto_speed", True)),
                 detail=auto_detail),
            Item("popup", "speed", T("Pointer speed on Windows"), float(cfg.get("speed") or 1.0), speeds),
            Item("switch", "gestures", T("Trackpad gestures on Windows"), bool(cfg.get("gestures", True)),
                 detail=T("Swipes and pinches act on Windows while you control it")),
        ]),
        Group(T("Scrolling"), [
            Item("popup", "scroll_speed", T("Scrolling speed"), float(cfg.get("scroll_speed") or 1.0), scrolls),
            Item("switch", "invert_scroll", T("Reverse direction"), bool(cfg.get("invert_scroll"))),
        ]),
        Group(T("Keyboard"), [
            Item("switch", "swap_cmd_ctrl", "Cmd → Ctrl, Ctrl → Win", bool(cfg.get("swap_cmd_ctrl"))),
            Item("switch", "altgr", T("Left Option → AltGr (for @, € etc.)"), cfg.get("left_option") == "altgr"),
            Item("switch", "option_unicode", T("Special characters with Option (@ € …)"),
                 bool(cfg.get("option_unicode", True))),
            Item("popup", "keyboard_layout", T("Keyboard type"), cfg.get("keyboard_layout") or "auto", layouts()),
        ]),
    ])


def _screen(cfg, st, ctx) -> Page:
    displays = ctx.get("displays") or [(0, T("Display {n}").format(n=1))]
    choices = ctx.get("extend_choices") or [(T("Off"), None)]
    ext_options = [(tuple(v) if v else None, label) for label, v in choices]
    sel = cfg.get("extend_monitor")
    return Page("screen", T("Screen and Video"), "display", "#6E56CF", [
        Group("", [
            Item("switch", "screen_share", T("Share the Mac's screen"), bool(cfg.get("screen_share")),
                 detail=T("Windows shows it in the screen window")),
            Item("popup", "display_index", T("Shared display"), int(cfg.get("display_index") or 0), displays),
            Item("popup", "quality", T("Picture quality"), cfg.get("quality") or "medium", qualities()),
            Item("popup", "video_codec", T("Video format"), cfg.get("video_codec") or "h264", codecs()),
            Item("switch", "adaptive_quality", T("Adjust to the connection automatically (H.264)"),
                 bool(cfg.get("adaptive_quality", True))),
        ]),
        Group(T("Second screen"), [
            Item("popup", "extend_monitor", T("Windows monitor"), tuple(sel) if isinstance(sel, list) else None,
                 ext_options, detail=ctx.get("extend_text", "")),
        ]),
    ])


def audio_routes():
    """Where sound plays (3.4), as the Mac's settings name it."""
    return [("own", T("Each on its own")), ("windows", T("All on Windows")), ("mac", T("All on the Mac"))]


def win_audio_routes():
    """The same choice as Windows' settings name it."""
    return [("own", T("Each computer plays its own sound")), ("windows", T("All sound on this PC")),
            ("mac", T("All sound on the Mac"))]


def audio_app_modes():
    return [("all", T("All apps")), ("selected", T("Only the apps below"))]


def selected_audio_apps(cfg) -> dict:
    apps = cfg.get("audio_apps") or {}
    return {str(k): str(v) for k, v in apps.items()} if isinstance(apps, dict) else {}


def mac_audio_route(cfg) -> str:
    if cfg.get("audio"):
        return "windows"
    return "mac" if cfg.get("audio_from_windows") else "own"


def _audio(cfg, st, ctx) -> Page:
    mode = cfg.get("audio_apps_mode") if cfg.get("audio_apps_mode") in ("all", "selected") else "all"
    route = mac_audio_route(cfg)
    first = [Item("segmented", "audio_route", T("Where sound plays"), route, audio_routes())]
    if route == "windows":
        first.append(Item("switch", "audio_mute_mac", T("Mute the Mac's speakers meanwhile"),
                          bool(cfg.get("audio_mute_mac", True))))
    first.append(Item("text", "audio_state", ctx.get("audio_text") or T("Status: —")))
    notes = {"own": T("The Mac's sound plays on the Mac, Windows' sound on Windows."),
             "windows": T("The Mac's sound plays on Windows too, so everything is heard from Windows' speakers."),
             "mac": T("Windows' sound plays on the Mac (Windows' speakers are muted meanwhile), so everything is "
                      "heard from the Mac.")}
    groups = [Group("", first, notes[route])]
    if route != "windows":
        return Page("audio", T("Audio"), "speaker.wave.2", "#D9485F", groups)
    groups.append(Group(T("Apps"), [Item("segmented", "audio_apps_mode", T("Send the sound of"), mode,
                                         audio_app_modes())],
                        T("Only the chosen apps' sound goes to Windows. With 'Mute the Mac's speakers' on, the other "
                          "apps are silent on the Mac too; turn it off to hear them on the Mac (the chosen apps then "
                          "play on both).") if mode == "selected" else ""))
    if mode == "selected":
        chosen = selected_audio_apps(cfg)
        apps = dict(ctx.get("audio_apps") or [])                 # running apps: {bundle id: name}
        for bid, name in chosen.items():
            apps.setdefault(bid, name)
        rows = [Item("switch", "audio_app:" + bid, name, bid in chosen,
                     detail="" if bid in dict(ctx.get("audio_apps") or []) else T("not running"))
                for bid, name in sorted(apps.items(), key=lambda kv: kv[1].lower())]
        groups.append(Group(T("Apps that play on Windows"), rows or [
            Item("text", "no_audio_apps", T("No apps are open"))]))
    return Page("audio", T("Audio"), "speaker.wave.2", "#D9485F", groups)


def _clipboard(cfg, st, ctx) -> Page:
    return Page("clipboard", T("Clipboard and Files"), "doc.on.clipboard", "#0F8F80", [
        Group("", [
            Item("switch", "clipboard", T("Clipboard sync"), bool(cfg.get("clipboard")),
                 detail=T("Text, formatted text and images, both ways")),
            Item("switch", "file_clipboard", T("File copy and paste"), bool(cfg.get("file_clipboard", True)),
                 detail=T("Copy in Finder, paste in Explorer (and back)")),
            Item("text", "files_state", ctx.get("files_text") or T("File transfer: —")),
        ]),
    ])


def _network(cfg, st) -> Page:
    net = st.get("network") or {}
    state = net.get("state")
    tone = "ok" if state == "trusted" else ("warn" if state == "untrusted" else "")
    extra = f"  (+{', '.join(st['all_ips'][1:])})" if len(st.get("all_ips") or []) > 1 else ""
    return Page("network", T("Network"), "network", "#2E9E4F", [
        Group("", [
            Item("text", "net_state", T("This network"), detail=net.get("text") or T("Network could not be determined"),
                 tone=tone),
            Item("switch", "network_lock", T("Work only on the home network"), bool(cfg.get("network_lock", True)),
                 detail=T("At a café or at work Mirrorway turns itself off")),
            Item("button", "action:trust_network", T("Trust this network"), button=T("Trust")),
            Item("button", "action:reset_networks", T("Reset trusted networks (keep only this one)"),
                 button=T("Reset…")),
        ]),
        Group(T("This Mac"), [
            Item("button", "action:copy_ip", T("Address"), detail=f"{st.get('ip', '')}:{st.get('port', '')}{extra}",
                 button=T("Copy")),
        ]),
    ])


def _devices(st) -> Page:
    devs = st.get("devices") or []
    items = []
    for d in devs:
        state = T("connected") if d["id"] == st.get("client_device") else ""
        if d.get("pending"):
            state = T("verifying")
        detail = d["id"] + (f" · {state}" if state else "")
        items.append(Item("button", "action:remove_device:" + d["id"], d.get("name") or d["id"], detail=detail,
                          button=T("Remove…"), tone="ok" if state == T("connected") else ""))
    if not items:
        items.append(Item("text", "no_devices", T("No paired devices")))
    p = st.get("pairing")
    pair = Item("button", "action:pair", T("Pair a new device"), button=T("Pair…"),
                detail=T("Code {code} · {time}").format(code=p["display"], time=quick.clock(p.get("remaining", 0)))
                if p else T("Shows a 6-digit code to enter on Windows"), tone="ok" if p else "")
    return Page("devices", T("Paired Devices"), "laptopcomputer.and.iphone", "#D9822B", [
        Group("", items),
        Group("", [pair]),
    ])


def _updates(cfg, ctx) -> Page:
    u = ctx.get("update") or {}
    available = u.get("available")
    prev = u.get("previous")
    return Page("updates", T("Updates"), "arrow.triangle.2.circlepath", "#6B6B70", [
        Group("", [
            Item("text", "version", T("Version"), detail=u.get("text") or f"Mirrorway {ctx.get('version', '')}"),
            Item("button", "action:check_updates", T("Check for updates"),
                 button=T("Checking…") if u.get("busy") else T("Check"), enabled=not u.get("busy")),
            Item("button", "action:install_update",
                 T("Install {version} and restart").format(version=available) if available else T("No new version"),
                 button=T("Install"), enabled=bool(available), tone="ok" if available else ""),
            Item("switch", "auto_update_check", T("Check automatically once a day"),
                 bool(cfg.get("auto_update_check", True))),
            Item("button", "action:rollback",
                 T("Return to the previous version ({version})").format(version=prev) if prev
                 else T("Return to the previous version"), button=T("Return…"), enabled=bool(prev)),
        ]),
        Group(T("Help"), [
            Item("button", "action:diag", T("Diagnostics bundle"), detail=T("Logs and settings in one zip, "
                                                                          "without codes or keys"),
                 button=T("Create")),
            Item("button", "action:show_log", T("Log files"), button=T("Show")),
        ]),
    ])


def mac_pages(cfg, st: dict, ctx: Optional[dict] = None) -> List[Page]:
    """The Mac Settings window.

    ctx: displays [(index, label)], extend_choices [(label, value)], extend_text, audio_text, files_text,
         login {"available", "enabled"}, update {"text", "busy", "available", "previous"}, permission, version
    """
    ctx = ctx or {}
    return [_general(cfg, ctx), _keyboard(cfg, st), _screen(cfg, st, ctx), _audio(cfg, st, ctx),
            _clipboard(cfg, st, ctx), _network(cfg, st), _devices(st), _updates(cfg, ctx)]


# ---- Windows Settings window -------------------------------------------------------------------------------------
def win_pages(cfg, ctx: Optional[dict] = None) -> List[Page]:
    """The Windows Settings window.

    ctx: state, status_text, mac_name, mac_ip, rtt_ms, auth ("key"/"legacy"), paired, fingerprint, found (text),
         audio_supported, audio_on, startup {"available", "enabled"}, version
    """
    ctx = ctx or {}
    connected = ctx.get("state") == "connected"
    paired = bool(ctx.get("paired"))
    if connected:
        parts = [p for p in (ctx.get("mac_ip"), f"{ctx['rtt_ms']:.0f} ms" if ctx.get("rtt_ms") is not None else "")
                 if p]
        parts.append(T("encrypted with this PC's device key") if ctx.get("auth") == "key"
                     else T("encrypted with the pairing code"))
        status = Item("button", "action:reconnect", T("Connected to {name}").format(name=ctx.get("mac_name") or "Mac"),
                      detail=" · ".join(parts), button=T("Reconnect"), tone="ok")
    else:
        status = Item("button", "action:reconnect", ctx.get("status_text") or T("Not connected"),
                      detail=ctx.get("found") or "", button=T("Reconnect"),
                      tone="warn" if ctx.get("state") in ("error", "auth", "unpaired") else "")
    conn = [status]
    if paired:
        conn.append(Item("button", "action:unpair", T("Paired Mac"),
                         detail=f"{ctx.get('mac_name') or 'Mac'} · " + T("fingerprint {id}").format(
                             id=ctx.get("fingerprint") or "—"), button=T("Remove pairing")))
    conn.append(Item("code", "action:pair", T("Pair with another Mac") if paired else T("Pair with a Mac"),
                     detail=T("On the Mac, open the Mirrorway panel, choose Pair a New Device… and type the 6-digit "
                              "code it shows."), button=T("Pair")))
    conn.append(Item("entry", "host", T("Mac address"), cfg.get("host") or "",
                     detail=T("Only needed when the Mac is not found by itself (a firewall blocks UDP 24851)")))
    start = ctx.get("startup") or {}
    conn.append(Item("switch", "startup", T("Start Mirrorway with Windows"), bool(start.get("enabled")),
                     enabled=bool(start.get("available", True))))

    viewers = [("auto", T("GPU (SDL2), Tk if it is not available")), ("tk", T("Tk (compatible, slower)"))]
    screen = [
        Item("switch", "open_viewer_on_start", T("Open the Mac's screen when Mirrorway starts"),
             bool(cfg.get("open_viewer_on_start"))),
        Item("popup", "viewer", T("Screen window"), cfg.get("viewer") or "auto", viewers),
        Item("switch", "hw_decode", T("Decode H.264 on the graphics card (D3D11VA)"), bool(cfg.get("hw_decode", True)),
             detail=T("Applies the next time the screen opens")),
        Item("switch", "overlay_fullscreen", T("Measurements in full screen (F9)"),
             bool(cfg.get("overlay_fullscreen", True))),
        Item("switch", "overlay_window", T("Measurements box in a window"), bool(cfg.get("overlay_window"))),
        Item("switch", "overlay_extend", T("Measurements on the second screen"), bool(cfg.get("overlay_extend"))),
    ]
    route = ctx.get("audio_route") or ""
    if ctx.get("route_supported") and route in ("own", "windows", "mac"):      # 3.4: the Mac and this PC
        if not connected:
            state = T("Not connected to the Mac")
        elif route == "mac":
            state = (T("This PC's sound plays on the Mac; this PC's speakers are muted meanwhile")
                     if ctx.get("sending_audio") else T("Starting…"))
            if ctx.get("audio_error"):
                state = T("⚠︎ {error}").format(error=ctx["audio_error"][:90])
        elif route == "windows":
            state = T("The Mac's sound plays on this PC") if ctx.get("audio_on") else T("Starting…")
        else:
            state = T("Each computer plays its own sound")
        audio = [Item("popup", "audio_route", T("Where sound plays"), route, win_audio_routes(),
                      detail=state, enabled=connected)]
    else:
        if not connected:
            sound = T("Off")
        elif not ctx.get("audio_supported"):
            sound = T("Update Mirrorway on the Mac to turn it on from here")
        else:
            sound = T("The Mac's sound plays on this PC") if ctx.get("audio_on") else T("Off")
            if route and not ctx.get("route_supported"):
                sound += " · " + T("Update Mirrorway on the Mac to send this PC's sound there")
        audio = [Item("button", "action:audio", T("The Mac's sound"), detail=sound,
                      button=T("Turn off") if ctx.get("audio_on") else T("Turn on"),
                      enabled=connected and bool(ctx.get("audio_supported")))]

    clip = [
        Item("switch", "clipboard", T("Clipboard sync"), bool(cfg.get("clipboard")),
             detail=T("Text, formatted text and images, both ways")),
        Item("switch", "file_clipboard", T("File copy and paste"), bool(cfg.get("file_clipboard", True)),
             detail=T("Copy in Explorer, paste in Finder (and back)")),
    ]
    letters = [("", T("Off"))] + [(c, quick.win_shortcut(c)) for c in sorted(quick.MAC_KEYCODES)]
    shortcuts = [
        Item("popup", "shortcut:screen", T("Show or hide the Mac's screen"), quick.win_shortcut_letters(cfg)["screen"],
             letters),
        Item("text", "mac_keys", T("On the Mac"),
             detail=T("⌃⌥ + arrow switches between the computers; "
                      "⌃⌥S, ⌃⌥D, ⌃⌥A and ⌃⌥M work there")),
    ]
    general = [
        Item("switch", "quick_panel", T("Clicking the tray icon opens the panel"), bool(cfg.get("quick_panel", True)),
             detail=T("Otherwise it opens the Mac's screen (after Mirrorway restarts)")),
        Item("switch", "switch_ring", T("Show a ring where the pointer appears"), bool(cfg.get("switch_ring", True)),
             detail=T("When the Mac's pointer arrives, for half a second")),
        Item("popup", "language", T("Language"), cfg.get("language") or "auto", languages(),
             detail=T("Used after Mirrorway restarts")),
        Item("button", "action:logs", T("Log files"), button=T("Open")),
        Item("button", "action:diag", T("Diagnostics bundle"),
             detail=T("Logs and settings in one zip, without codes or keys"), button=T("Create")),
        Item("text", "version", T("Version"), detail=f"Mirrorway {ctx.get('version', '')}"),
    ]
    return [
        Page("connection", T("Connection"), "", "#5B3FD6", [Group("", conn)]),
        Page("screen", T("Mac screen"), "", "#6E56CF", [Group("", screen)]),
        Page("audio", T("Audio"), "", "#D9485F", [Group("", audio)]),
        Page("clipboard", T("Clipboard and files"), "", "#0F8F80", [Group("", clip)]),
        Page("shortcuts", T("Shortcuts"), "", "#3B6FD8", [Group("", shortcuts)]),
        Page("general", T("General"), "", "#6B6B70", [Group("", general), _lockscreen_group(ctx)]),
    ]


def _lockscreen_group(ctx) -> Group:
    """Lock screen support (Windows): a service, installed with administrator permission."""
    ls = ctx.get("lockscreen") or {}
    installed, running, busy = bool(ls.get("installed")), bool(ls.get("running")), bool(ls.get("busy"))
    if ls.get("error") and not installed:
        state = T("Could not install: {error}").format(error=ls["error"])
    elif installed and ls.get("outdated"):
        state = T("Version {version} is installed; update it to match this Mirrorway").format(
            version=ls.get("version") or "?")
    elif installed and running:
        state = T("On: the Mac's keyboard and mouse work on the lock screen, Ctrl+Alt+Del (⌃⌥⌫ on the Mac) "
                  "and UAC prompts")
        if ls.get("older"):
            state += " · " + T("version {version}").format(version=ls.get("version") or "?")
    elif installed:
        state = T("Installed, but the service is not running")
    elif busy:
        state = T("Installing… confirm the Windows prompt")
    else:
        state = T("Off: the lock screen and UAC prompts take only this PC's own keyboard and mouse")
    items = [Item("text", "lockscreen_state", T("Status"), detail=state)]
    if not installed or ls.get("outdated") or ls.get("older"):
        items.append(Item("button", "action:lockscreen_install",
                          T("Update lock screen support") if installed else T("Install lock screen support"),
                          detail=T("Asks for administrator permission once; uses this PC's pairing with the Mac"),
                          button=T("Update…") if installed else T("Install…"),
                          enabled=bool(ctx.get("paired")) and not busy))
    if installed:
        items.append(Item("button", "action:lockscreen_remove", T("Remove lock screen support"),
                          button=T("Remove…"), enabled=not busy))
    return Group(T("Lock screen"), items)
