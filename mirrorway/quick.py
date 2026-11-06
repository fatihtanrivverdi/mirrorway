"""The quick-access panel's content, independent of the interface toolkit (tested on every platform).

The Mac menu bar panel (mac/panel.py) draws what mac_panel() returns: a status card, the main button, six
tiles and three rows. The functions here only turn the app's state into texts and on/off flags.

Quick shortcuts: Control + Option + a letter on the Mac. The letters come from the "shortcuts" setting
({"screen": "s", ...}); an empty letter turns that shortcut off.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import __version__
from .i18n import T

ACTIONS = ("screen", "second", "audio", "panel")
DEFAULT_SHORTCUTS = {"screen": "s", "second": "d", "audio": "a", "panel": "m"}

# Mac virtual key codes of the letters (ANSI positions; macOS reports the same codes on ISO keyboards)
MAC_KEYCODES = {"a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
                "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31, "u": 32, "i": 34, "p": 35,
                "l": 37, "j": 38, "k": 40, "n": 45, "m": 46}

# side of the screen where Windows is -> (arrow to Windows, arrow back to the Mac)
ARROWS = {"right": ("→", "←"), "left": ("←", "→"), "top": ("↑", "↓"), "bottom": ("↓", "↑")}


def shortcut_letters(cfg) -> Dict[str, str]:
    """{action: letter} from the setting; unknown, repeated or invalid letters fall back to off ("")."""
    raw = cfg.get("shortcuts") if cfg is not None else None
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, str] = {}
    used = set()
    for action in ACTIONS:
        letter = raw.get(action, DEFAULT_SHORTCUTS[action])
        letter = str(letter or "").strip().lower()
        if letter not in MAC_KEYCODES or letter in used:
            letter = ""
        if letter:
            used.add(letter)
        out[action] = letter
    return out


def mac_shortcut(letter: str) -> str:
    return "⌃⌥" + letter.upper() if letter else ""


def mac_action_keys(cfg) -> Dict[int, str]:
    """{Mac key code: action} for the router."""
    return {MAC_KEYCODES[letter]: action for action, letter in shortcut_letters(cfg).items() if letter}


@dataclass
class Header:
    state: str            # connected, remote, waiting, permission, paused
    title: str
    detail: str


@dataclass
class Primary:
    action: str           # to_win, to_mac
    title: str
    shortcut: str
    enabled: bool


@dataclass
class Tile:
    key: str
    title: str
    detail: str
    on: bool
    enabled: bool
    shortcut: str = ""

    @property
    def subtitle(self) -> str:
        return f"{self.detail} · {self.shortcut}" if self.shortcut and self.detail else (self.detail or self.shortcut)


@dataclass
class Row:
    key: str
    title: str
    detail: str = ""
    tone: str = ""        # "ok", "warn" or "" (colour of the detail)
    enabled: bool = True


@dataclass
class Panel:
    header: Header
    primary: Primary
    tiles: List[Tile] = field(default_factory=list)
    rows: List[Row] = field(default_factory=list)
    footer: str = ""
    update: Optional[str] = None      # a new version that can be installed

    def tile(self, key: str) -> Tile:
        return next(t for t in self.tiles if t.key == key)

    def row(self, key: str) -> Row:
        return next(r for r in self.rows if r.key == key)


def _clock(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _header(st: dict, permission: bool) -> Header:
    net = st.get("network") or {}
    if not permission:
        return Header("permission", T("Permission needed"), T("Turn on Accessibility and Input Monitoring"))
    if not net.get("allowed", True):
        return Header("paused", T("Off on this network"), T("Trust this network from Network below"))
    if st.get("connected"):
        name = st.get("client_name") or "Windows"
        remote = bool(st.get("remote"))
        title = (T("You are controlling {name}") if remote else T("Connected to {name}")).format(name=name)
        parts = []
        if net.get("state") == "trusted":
            parts.append(T("Home network"))
        if st.get("rtt_ms") is not None:
            parts.append(f"{st['rtt_ms']:.0f} ms")
        if st.get("video"):
            parts.append(f"{st.get('video_fps') or 0:.0f} fps")
            parts.append("H.264" if st.get("video_codec") == "h264" else "JPEG")
        return Header("remote" if remote else "connected", title, " · ".join(parts))
    if st.get("error"):
        detail = str(st["error"])
    elif st.get("vpn"):
        detail = T("VPN is on: Windows may not be able to connect")
    else:
        detail = T("This Mac: {address}").format(address=f"{st.get('ip', '')}:{st.get('port', '')}")
    return Header("waiting", T("Waiting for Windows…"), detail)


def _second_tile(st: dict, connected: bool, letter: str) -> Tile:
    ex = st.get("extend") or {}
    supported = bool(st.get("extend_supported"))
    desired = bool(ex.get("desired"))
    if desired and ex.get("error"):
        detail = T("Needs attention")
    elif ex.get("active"):
        m = ex.get("monitor")
        detail = T("Monitor {n}").format(n=m + 1) if isinstance(m, int) else T("On")
    elif desired:
        detail = T("Starting…") if connected else T("Waiting")
    elif connected and not supported:
        detail = T("Update needed")
    else:
        detail = T("Off")
    return Tile("second", T("Second screen"), detail, desired, desired or (connected and supported),
                mac_shortcut(letter))


def _audio_tile(st: dict, connected: bool, letter: str) -> Tile:
    a = st.get("audio") or {}
    on = bool(a.get("enabled"))
    if not on:
        detail = T("On the Mac")
    elif a.get("error"):
        detail = T("Needs attention")
    elif a.get("active"):
        detail = T("On Windows")
    elif connected and not a.get("supported"):
        detail = T("Update needed")
    elif not connected:
        detail = T("Waiting")
    else:
        detail = T("Starting…")
    return Tile("audio", T("Audio"), detail, on, True, mac_shortcut(letter))


def _wake_tile(st: dict, connected: bool, waking: Optional[int]) -> Tile:
    target = st.get("wol")
    if waking is not None:
        detail = T("Waking… {seconds} s").format(seconds=int(waking))
    elif connected:
        detail = T("Windows is on")
    elif not target:
        detail = T("Connect first")
    else:
        detail = "Wake-on-LAN"
    return Tile("wake", T("Wake the PC"), detail, waking is not None,
                not connected and bool(target) and waking is None)


def mac_panel(st: dict, cfg, *, permission: bool = True, waking: Optional[int] = None,
              update: Optional[str] = None, version: str = __version__) -> Panel:
    """The Mac panel from the server status (MirrorwayServer.status()) and the settings.

    permission: the event tap has Accessibility + Input Monitoring permission
    waking:     seconds left while Windows is being woken (None otherwise)
    update:     version of an update ready to install
    """
    net = st.get("network") or {}
    allowed = bool(net.get("allowed", True))
    connected = bool(st.get("connected"))
    remote = bool(st.get("remote"))
    keys = shortcut_letters(cfg)
    side = cfg.get("side") if cfg.get("side") in ARROWS else "right"
    to_win, to_mac = ARROWS[side]

    if remote:
        primary = Primary("to_mac", T("Back to the Mac"), "⌃⌥" + to_mac, True)
    else:
        primary = Primary("to_win", T("Switch to Windows"), "⌃⌥" + to_win, connected and permission and allowed)

    video = bool(st.get("video"))
    if video:
        screen_detail = T("On Windows")
    elif not cfg.get("screen_share"):
        screen_detail = T("Sharing off")
    else:
        screen_detail = T("Off")
    files = st.get("files")
    files_on = bool(cfg.get("file_clipboard", True))
    if files_on and files and files.get("state") == "running":
        files_detail = f"{files.get('percent', 0)}%"
    else:
        files_detail = T("Copy and paste") if files_on else T("Off")
    clip_on = bool(cfg.get("clipboard"))
    tiles = [
        Tile("screen", T("Screen"), screen_detail, video, connected and allowed, mac_shortcut(keys["screen"])),
        _second_tile(st, connected, keys["second"]),
        _audio_tile(st, connected, keys["audio"]),
        Tile("clipboard", T("Clipboard"), T("Text and images") if clip_on else T("Off"), clip_on, True),
        Tile("files", T("Files"), files_detail, files_on, True),
        _wake_tile(st, connected, waking),
    ]

    p = st.get("pairing")
    devices = st.get("devices") or []
    if p:
        pair = Row("pair", T("Pairing code: {code}").format(code=p["display"]), _clock(p.get("remaining", 0)), "ok")
    else:
        pair = Row("pair", T("Pair a New Device…"), T("{n} paired").format(n=len(devices)) if devices else "")
    state = net.get("state")
    if state == "trusted":
        network = Row("network", T("Network"), T("Home ✓"), "ok")
    elif state == "off":
        network = Row("network", T("Network"), T("Any network"))
    elif state == "untrusted":
        network = Row("network", T("Network"), T("Unknown ⏸"), "warn")
    else:
        network = Row("network", T("Network"), "—")
    rows = [pair, network, Row("disconnect", T("Disconnect"), enabled=connected)]

    footer = T("Update {version} available").format(version=update) if update else f"Mirrorway {version}"
    return Panel(_header(st, permission), primary, tiles, rows, footer, update)
