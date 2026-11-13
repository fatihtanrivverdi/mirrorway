"""Simple JSON settings files (~/.mirrorway/<name>.json).

Mirrorway was called Köprü before 3.0. On first start, settings found in the old folder (~/.kopru) are
imported once (see migrate_settings); the old folder is left untouched.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from pathlib import Path

log = logging.getLogger("mirrorway.config")

CONFIG_DIR = Path(os.environ.get("MIRRORWAY_HOME", Path.home() / ".mirrorway"))
LEGACY_CONFIG_DIR = Path(os.environ.get("MIRRORWAY_LEGACY_HOME", Path.home() / ".kopru"))   # Köprü 1.x–2.x

DEFAULT_PORT = 24850          # TCP: control + video
DISCOVERY_PORT = 24851        # UDP: discovery beacon

# No easily confused characters (0/O, 1/I/L).
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

MAC_DEFAULTS = {
    "port": DEFAULT_PORT,
    "pair_code": "",
    "language": "auto",         # interface language: auto (system), en, tr
    "side": "right",            # where Windows sits relative to the Mac: right/left/top/bottom
    "speed": 1.0,               # pointer speed multiplier on Windows (on top of the automatic speed)
    "auto_speed": True,         # base speed = Windows monitor width / Mac screen width
    "option_unicode": True,     # Option + key -> the character the Mac would type, sent to Windows as Unicode
    "scroll_speed": 1.0,
    "invert_scroll": False,
    "swap_cmd_ctrl": True,      # Cmd -> Ctrl, Ctrl -> Win
    "left_option": "alt",       # "alt" or "altgr"
    "keyboard_layout": "auto",  # "auto", "iso", "ansi"
    "edge_switch": True,
    "screen_share": True,
    "display_index": 0,
    "quality": "medium",        # low / medium / high
    "video_codec": "h264",      # h264 (smooth, low bandwidth) / jpeg (changed regions, sharpest text)
    "adaptive_quality": True,   # adjust H.264 bitrate and frame rate to the connection automatically
    "clipboard": True,
    "gestures": True,           # while Windows is controlled, trackpad gestures become their Windows equivalents
    "file_clipboard": True,     # copied files and folders are transferred and put on the other clipboard
    "file_clipboard_max_mb": 4096,
    "audio": False,             # play the Mac's sound on Windows ("all sound on Windows")
    "audio_from_windows": False,  # 3.4: play Windows' sound on the Mac ("all sound on the Mac"; never both on)
    "audio_route_last": "windows",  # the direction the quick toggle turns on ("windows" or "mac")
    "audio_mute_mac": True,     # mute the Mac's speakers while its sound plays on Windows (restored afterwards)
    "audio_muted_by_app": False,
    "audio_apps_mode": "all",   # whose sound goes to Windows: "all" apps, or only the "selected" ones
    "audio_apps": {},           # the selected apps: {bundle id: name}
    "extend_monitor": None,     # second screen: None = off, [x, y, w, h] = the Windows monitor that extends the Mac
    "win_monitors": [],         # monitors Windows reported last (for the menu while Windows is not connected)
    "extend_last": None,        # the monitor chosen last ([x, y, w, h]); the panel's tile turns it back on
    "quick_panel": True,        # clicking the menu bar icon opens the panel (False: the classic menu)
    "menubar_state": True,      # "•" (connected) and " Win" (controlling Windows) next to the menu bar icon
    "switch_ring": True,        # a ring where the pointer appears when control comes back, for half a second
    "lock_sync": False,         # lock both computers together: the Mac locks -> Windows locks, and the other way
    "shortcuts": {"screen": "s", "second": "d", "audio": "a", "panel": "m"},   # Control + Option + letter
}

WIN_DEFAULTS = {
    "host": "",                 # empty = discover automatically
    "fallback_host": "",        # last known Mac address, tried when discovery finds nothing
    "port": DEFAULT_PORT,
    "pair_code": "",
    "language": "auto",
    "clipboard": True,
    "open_viewer_on_start": False,
    "viewer": "auto",           # auto: GPU window (SDL2), Tk as a fallback; "tk": always Tk
    "hw_decode": True,          # decode H.264 on the GPU (D3D11VA), software as a fallback
    "overlay_fullscreen": True, # stats in the top right corner in full screen (toggle with F9)
    "overlay_window": False,    # stats box in windowed mode (the title bar already shows them)
    "overlay_extend": False,    # stats box on the second screen (over the Mac desktop; off by default)
    "file_clipboard": True,     # copied files go to the Mac and back (the Files tile)
    "quick_panel": True,        # clicking the tray icon opens the panel (False: the screen window, as before 3.1.2)
    "switch_ring": True,        # a ring where the Mac's pointer arrives, for half a second
    "audio_muted_by_app": False,  # 3.4: Mirrorway muted the speakers while Windows' sound played on the Mac
    "shortcuts": {"screen": "s"},   # Control + Alt + letter: show or hide the Mac's screen
}

# Settings renamed in 3.0: old key -> new key, and old values -> new values
_RENAMED_KEYS = {"audio_muted_by_kopru": "audio_muted_by_app"}
_RENAMED_VALUES = {"quality": {"dusuk": "low", "orta": "medium", "yuksek": "high"}}


def generate_pair_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"


def normalize_pair_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def migrate_settings(data: dict) -> dict:
    """Köprü 2.x settings -> Mirrorway 3.0: renamed keys and values; paired devices get their new IDs
    (the ID is a hash that includes the app name)."""
    out = dict(data)
    for old, new in _RENAMED_KEYS.items():
        if old in out:
            out.setdefault(new, out.pop(old))
    for key, table in _RENAMED_VALUES.items():
        if out.get(key) in table:
            out[key] = table[out[key]]
    devices = out.get("devices")
    if isinstance(devices, list):
        from .devkeys import b64d, fingerprint
        fixed = []
        for dev in devices:
            dev = dict(dev)
            try:
                dev["id"] = fingerprint(b64d(dev["pub"]))
            except Exception:
                pass
            fixed.append(dev)
        out["devices"] = fixed
    return out


class Config:
    def __init__(self, name: str, defaults: dict):
        self.path = CONFIG_DIR / f"{name}.json"
        self._defaults = dict(defaults)
        self._data = dict(defaults)
        self._lock = threading.Lock()
        self.migrated_from = None
        self._import_legacy(name)
        self.load()

    def _import_legacy(self, name: str) -> None:
        old = LEGACY_CONFIG_DIR / f"{name}.json"
        if self.path.exists() or not old.exists():
            return
        try:
            with open(old, "r", encoding="utf-8-sig") as fh:
                stored = json.load(fh)
            if not isinstance(stored, dict):
                return
            self._data.update(migrate_settings(stored))
            self.save()
            self._data = dict(self._defaults)
            self.migrated_from = old
            log.info("Settings imported from %s", old)
        except Exception as exc:
            log.warning("Could not import old settings from %s: %s", old, exc)

    def load(self) -> None:
        try:
            if self.path.exists():
                with open(self.path, "r", encoding="utf-8-sig") as fh:  # tolerate a BOM
                    stored = json.load(fh)
                if isinstance(stored, dict):
                    self._data.update(stored)
        except Exception as exc:  # corrupt file -> defaults
            log.warning("Could not read the settings file (%s), using defaults", exc)

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, indent=2, ensure_ascii=False)
                os.replace(tmp, self.path)
            except Exception as exc:
                log.error("Could not save settings: %s", exc)

    def get(self, key: str, default=None):
        return self._data.get(key, self._defaults.get(key, default))

    def __getitem__(self, key: str):
        return self.get(key)

    def set(self, key: str, value, save: bool = True) -> None:
        self._data[key] = value
        if save:
            self.save()

    def as_dict(self) -> dict:
        return dict(self._data)


def setup_logging(name: str, debug: bool = False) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = CONFIG_DIR / f"{name}.log"
    handlers = [logging.StreamHandler()]
    try:
        from logging.handlers import RotatingFileHandler
        handlers.append(RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=logging.DEBUG if debug or os.environ.get("MIRRORWAY_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    return log_path
