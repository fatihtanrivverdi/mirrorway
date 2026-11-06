"""The quick-access panel model (mirrorway/quick.py) and the Mac quick shortcuts in the router."""
from mirrorway import quick as Q
from mirrorway.keymap import FLAG_COMMAND, FLAG_CONTROL, FLAG_OPTION, FLAG_SHIFT
from mirrorway.mac.router import PASS, SUPPRESS, InputEvent, Router


class Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)

    def set(self, k, v, save=True):
        self[k] = v


def cfg(**kw):
    base = {"side": "right", "screen_share": True, "clipboard": True, "file_clipboard": True, "audio": False}
    base.update(kw)
    return Cfg(base)


def status(**kw):
    st = {"connected": False, "remote": False, "client_name": "", "video": False, "video_fps": 0.0,
          "video_codec": "", "rtt_ms": None, "ip": "192.168.1.20", "port": 24800, "error": "", "vpn": False,
          "wol": None, "network": {"state": "trusted", "allowed": True}, "devices": [], "pairing": None,
          "extend": {"desired": False, "active": False, "error": "", "monitor": None},
          "extend_supported": False, "files": None,
          "audio": {"enabled": False, "active": False, "error": "", "supported": False}}
    st.update(kw)
    return st


def connected(**kw):
    base = dict(connected=True, client_name="Home-PC", rtt_ms=3.2, video=True, video_fps=58.4, video_codec="h264",
                extend_supported=True, wol={"mac": "aa:bb", "ip": "192.168.1.30"},
                audio={"enabled": True, "active": True, "error": "", "supported": True})
    base.update(kw)
    return status(**base)


# ---- shortcuts ------------------------------------------------------------------------------------------
def test_shortcut_letters_defaults_and_validation():
    assert Q.shortcut_letters(cfg()) == {"screen": "s", "second": "d", "audio": "a", "panel": "m"}
    got = Q.shortcut_letters(cfg(shortcuts={"screen": "X", "second": "x", "audio": "", "panel": "9"}))
    assert got == {"screen": "x", "second": "", "audio": "", "panel": ""}    # repeated / off / not a letter
    assert Q.shortcut_letters(cfg(shortcuts="nonsense"))["screen"] == "s"
    assert Q.mac_shortcut("s") == "⌃⌥S" and Q.mac_shortcut("") == ""
    assert Q.mac_action_keys(cfg()) == {1: "screen", 2: "second", 0: "audio", 46: "panel"}


class Platform:
    def displays(self):
        return []

    def main_display(self):
        return None

    def warp(self, x, y):
        pass

    def hide_cursor(self):
        pass

    def show_cursor(self):
        pass


class Sink:
    def __init__(self):
        self.sent = []

    def connected(self):
        return True

    def send(self, mtype, payload=b""):
        self.sent.append(mtype)

    def send_json(self, mtype, obj):
        self.sent.append(mtype)


def _key(kc, flags, down=True):
    return InputEvent(kind="key_down" if down else "key_up", keycode=kc, flags=flags)


def test_router_quick_shortcuts():
    sink = Sink()
    r = Router(Platform(), sink, cfg())
    got = []
    r.on_action = got.append
    ctl_opt = FLAG_CONTROL | FLAG_OPTION
    assert r.handle(_key(1, ctl_opt)) == SUPPRESS and got == ["screen"]
    assert r.handle(_key(1, ctl_opt)) == SUPPRESS and got == ["screen"]          # key repeat: once
    assert r.handle(_key(1, ctl_opt, down=False)) == SUPPRESS
    assert r.handle(_key(46, ctl_opt)) == SUPPRESS and got[-1] == "panel"
    r.handle(_key(46, ctl_opt, down=False))
    # with Command or Shift, or without Option: not a quick shortcut
    for flags in (ctl_opt | FLAG_COMMAND, ctl_opt | FLAG_SHIFT, FLAG_CONTROL):
        assert r.handle(_key(0, flags)) == PASS
        r.handle(_key(0, flags, down=False))
    assert got == ["screen", "panel"]
    # a turned-off shortcut passes; changes apply after reload_settings()
    r.settings["shortcuts"] = {"audio": ""}
    r.reload_settings()
    assert r.handle(_key(0, ctl_opt)) == PASS
    r.handle(_key(0, ctl_opt, down=False))
    # while controlling Windows the shortcut is not sent to Windows
    r.remote = True
    sink.sent.clear()
    assert r.handle(_key(2, ctl_opt)) == SUPPRESS and got[-1] == "second" and sink.sent == []
    assert r.handle(_key(2, ctl_opt, down=False)) == SUPPRESS and sink.sent == []
    # without a listener the keys are left alone
    r.remote = False
    r.on_action = None
    assert r.handle(_key(1, ctl_opt)) == PASS


# ---- panel ---------------------------------------------------------------------------------------------
def test_connected_panel():
    p = Q.mac_panel(connected(), cfg(), version="3.1.0")
    assert (p.header.state, p.header.title) == ("connected", "Connected to Home-PC")
    assert p.header.detail == "Home network · 3 ms · 58 fps · H.264"
    assert (p.primary.action, p.primary.title, p.primary.shortcut, p.primary.enabled) == \
        ("to_win", "Switch to Windows", "⌃⌥→", True)
    assert [t.key for t in p.tiles] == ["screen", "second", "audio", "clipboard", "files", "wake"]
    s = p.tile("screen")
    assert s.on and s.enabled and s.subtitle == "On Windows · ⌃⌥S"
    assert p.tile("second").subtitle == "Off · ⌃⌥D" and p.tile("second").enabled and not p.tile("second").on
    assert p.tile("audio").on and p.tile("audio").detail == "On Windows"
    assert p.tile("clipboard").on and p.tile("clipboard").subtitle == "Text and images"
    assert p.tile("files").detail == "Copy and paste"
    w = p.tile("wake")
    assert w.detail == "Windows is on" and not w.enabled
    assert [r.key for r in p.rows] == ["pair", "network", "disconnect"]
    assert p.row("network").detail == "Home ✓" and p.row("network").tone == "ok"
    assert p.row("disconnect").enabled
    assert p.footer == "Mirrorway 3.1.0" and p.update is None


def test_remote_panel_and_sides():
    p = Q.mac_panel(connected(remote=True, video_codec="jpeg"), cfg(side="left"))
    assert p.header.state == "remote" and p.header.title == "You are controlling Home-PC"
    assert p.header.detail.endswith("JPEG")
    assert (p.primary.action, p.primary.title, p.primary.shortcut) == ("to_mac", "Back to the Mac", "⌃⌥→")
    assert Q.mac_panel(connected(), cfg(side="top")).primary.shortcut == "⌃⌥↑"
    assert Q.mac_panel(connected(), cfg(side="nonsense")).primary.shortcut == "⌃⌥→"


def test_waiting_panel():
    p = Q.mac_panel(status(wol={"mac": "aa", "ip": "1.2.3.4"}, devices=[{"id": "x"}, {"id": "y"}]),
                    cfg(clipboard=False, file_clipboard=False))
    assert p.header.state == "waiting" and p.header.title == "Waiting for Windows…"
    assert p.header.detail == "This Mac: 192.168.1.20:24800"
    assert not p.primary.enabled
    assert not p.tile("screen").enabled and p.tile("screen").detail == "Off"
    assert not p.tile("second").enabled
    assert p.tile("audio").enabled and p.tile("audio").detail == "On the Mac"
    assert p.tile("clipboard").detail == "Off" and not p.tile("clipboard").on
    assert p.tile("files").detail == "Off"
    w = p.tile("wake")
    assert w.enabled and w.detail == "Wake-on-LAN" and not w.on
    assert p.row("pair").detail == "2 paired" and not p.row("disconnect").enabled
    assert Q.mac_panel(status(error="Port in use"), cfg()).header.detail == "Port in use"
    assert "VPN" in Q.mac_panel(status(vpn=True), cfg()).header.detail
    assert Q.mac_panel(status(), cfg()).tile("wake").detail == "Connect first"
    assert not Q.mac_panel(status(), cfg()).tile("wake").enabled


def test_waking_permission_and_paused():
    p = Q.mac_panel(status(wol={"mac": "aa"}), cfg(), waking=42)
    assert p.tile("wake").on and p.tile("wake").detail == "Waking… 42 s" and not p.tile("wake").enabled
    p = Q.mac_panel(connected(), cfg(), permission=False)
    assert p.header.state == "permission" and not p.primary.enabled
    p = Q.mac_panel(connected(network={"state": "untrusted", "allowed": False}), cfg())
    assert p.header.state == "paused" and not p.primary.enabled and not p.tile("screen").enabled
    assert p.row("network").detail == "Unknown ⏸" and p.row("network").tone == "warn"
    assert Q.mac_panel(status(network={"state": "off", "allowed": True}), cfg()).row("network").detail == "Any network"


def test_tile_details():
    p = Q.mac_panel(connected(video=False, extend={"desired": True, "active": True, "monitor": 1, "error": ""}),
                    cfg(screen_share=False))
    assert p.tile("screen").detail == "Sharing off" and not p.tile("screen").on
    assert p.tile("second").on and p.tile("second").detail == "Monitor 2"
    st = connected(extend={"desired": True, "active": False, "error": ""})
    assert Q.mac_panel(st, cfg()).tile("second").detail == "Starting…"
    st = status(extend={"desired": True, "active": False, "error": ""})
    sec = Q.mac_panel(st, cfg()).tile("second")
    assert sec.detail == "Waiting" and sec.enabled            # can be turned off while waiting
    st = connected(extend={"desired": True, "active": False, "error": "gone"})
    assert Q.mac_panel(st, cfg()).tile("second").detail == "Needs attention"
    st = connected(extend_supported=False)
    sec = Q.mac_panel(st, cfg()).tile("second")
    assert sec.detail == "Update needed" and not sec.enabled
    for audio, text in (({"enabled": True, "error": "x"}, "Needs attention"),
                        ({"enabled": True, "supported": False}, "Update needed"),
                        ({"enabled": True, "supported": True}, "Starting…")):
        assert Q.mac_panel(connected(audio=audio), cfg()).tile("audio").detail == text
    assert Q.mac_panel(status(audio={"enabled": True}), cfg()).tile("audio").detail == "Waiting"
    st = connected(files={"state": "running", "percent": 37})
    assert Q.mac_panel(st, cfg()).tile("files").detail == "37%"
    p = Q.mac_panel(connected(), cfg(shortcuts={"screen": "", "panel": "s"}))
    assert p.tile("screen").subtitle == "On Windows" and p.tile("files").subtitle == "Copy and paste"


def test_pairing_and_update_rows():
    p = Q.mac_panel(status(pairing={"display": "123 456", "remaining": 272}), cfg(), update="3.1.1")
    assert p.row("pair").title == "Pairing code: 123 456" and p.row("pair").detail == "4:32"
    assert p.update == "3.1.1" and p.footer == "Update 3.1.1 available"
    assert Q.mac_panel(status(network={}), cfg()).row("network").detail == "—"


# ---- Windows tray panel -------------------------------------------------------------------------------------------
def test_win_panel_connected_and_remote():
    c = cfg(clipboard=True)
    p = Q.win_panel(state="connected", status_text="Connected to MacBook-Pro", cfg=c, mac_name="MacBook-Pro",
                    viewer=True, rtt_ms=3.2, fps=58.4, audio_supported=True, audio_on=True, version="3.1.2")
    assert p.header.subtitle == "Connected to MacBook-Pro" and p.header.state == "connected"
    assert p.header.title == "The Mac's keyboard and mouse can reach this PC"
    assert p.header.detail == "Push the Mac's pointer against its right edge · 3 ms · 58 fps"
    assert (p.primary.action, p.primary.title, p.primary.shortcut, p.primary.enabled) == \
        ("hide_screen", "Hide the Mac's screen", "Ctrl+Alt+S", True)
    assert [t.key for t in p.tiles] == ["clipboard", "audio", "files", "reconnect", "pair", "diag"]
    assert p.tile("audio").on and p.tile("audio").enabled and p.tile("clipboard").on and p.tile("files").on
    assert p.footer == "Mirrorway 3.1.2"
    p = Q.win_panel(state="connected", status_text="", cfg=cfg(shortcuts={"screen": ""}), remote=True, side="left")
    assert p.header.state == "remote" and p.header.title == "The Mac's keyboard and mouse are on this PC"
    assert p.header.detail == "Push the pointer against the right edge to go back to the Mac"
    assert p.primary.action == "show_screen" and p.primary.shortcut == ""
    assert p.tile("audio").detail == "Update needed" and not p.tile("audio").enabled


def test_win_panel_not_connected():
    p = Q.win_panel(state="idle", status_text="Looking for the Mac on the network…",
                    cfg=cfg(clipboard=False, file_clipboard=False), reconnecting=True)
    assert p.header.state == "waiting" and p.header.subtitle == "Looking for the Mac on the network…"
    assert not p.primary.enabled and p.primary.action == "show_screen"
    assert not p.tile("clipboard").on and not p.tile("files").on and p.tile("audio").detail == "Off"
    r = p.tile("reconnect")
    assert r.on and not r.enabled and r.detail == "Connecting…"
    assert Q.win_panel(state="unpaired", status_text="x", cfg=cfg()).header.state == "permission"
    assert Q.win_panel(state="error", status_text="refused", cfg=cfg()).header.detail == "refused"
    assert Q.win_shortcut_letters(cfg(shortcuts={"screen": "K"})) == {"screen": "k"}
