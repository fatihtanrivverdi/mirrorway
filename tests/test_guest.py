"""3.6: Windows's own keyboard and mouse on the Mac (reverse control)."""
import json
import threading
import time

from conftest import DictCfg
from test_router_remote import FakeClock, FakeMac, FakeSink, mac_cfg

from mirrorway import guest as GU
from mirrorway import keymap as K
from mirrorway import protocol as P
from mirrorway.geometry import Rect
from mirrorway.mac.guest import MacGuest
from mirrorway.mac.router import InputEvent, Router
from mirrorway.win.guest import ARROW_VK, OutBox, WinGuest


# ---- keys ------------------------------------------------------------------------------------------------------
def test_windows_keys_map_back_to_the_same_mac_keys():
    fwd = K.KeyTranslator(swap_cmd_ctrl=True, iso=True)
    back = GU.WinToMacKeys(swap_cmd_ctrl=True, iso=True)
    for mac in list(K._BASE) + [10, 50, 71]:
        if mac in K.MODIFIER_KEYCODES:
            continue
        flags, scan, vk = fwd.translate(mac)
        assert back.translate(flags, scan, vk) == mac, mac
    for mac in (K.MAC_LCMD, K.MAC_RCMD, K.MAC_LCTRL, K.MAC_RCTRL, K.MAC_LSHIFT, K.MAC_RSHIFT, K.MAC_LOPT,
                K.MAC_ROPT, K.MAC_CAPS):
        flags, scan, vk = fwd.translate(mac)
        assert back.translate(flags, scan, vk) == mac, mac
    plain = GU.WinToMacKeys(swap_cmd_ctrl=False)
    assert plain.translate(0, 0x1D, 0xA2) == K.MAC_LCTRL and plain.translate(P.KEYFLAG_EXTENDED, 0x5B, 0x5B) == 55
    assert back.translate(0, 0x21D, 0xA2) is None                 # the left Ctrl Windows adds to AltGr
    assert back.translate(P.KEYFLAG_EXTENDED, 0x38, 0xA5) == K.MAC_ROPT   # AltGr = right ⌥ (@ on Turkish Q)
    assert GU.WinToMacKeys(iso=False).translate(0, 0x29, 0xC0) == 50


def test_modifier_flags():
    m = GU.ModifierState()
    m.update(K.MAC_LCMD, True)
    m.update(K.MAC_LSHIFT, True)
    assert m.flags() == K.FLAG_COMMAND | K.FLAG_SHIFT | 0x0008 | 0x0002
    m.update(K.MAC_CAPS, True)
    assert m.flags() & K.FLAG_CAPS
    m.update(K.MAC_LCMD, False)
    assert not m.flags() & K.FLAG_COMMAND


# ---- the Mac's side --------------------------------------------------------------------------------------------
class FakePoster:
    def __init__(self, plat):
        self.plat = plat
        self.log = []

    def cursor_pos(self):
        return self.plat.cursor_pos()

    def move(self, x, y, dx, dy, button):
        self.plat.warps.append((x, y))
        self.log.append(("move", round(x, 3), round(y, 3), button))

    def button(self, button, down, x, y, clicks):
        self.log.append(("button", button, down, clicks))

    def scroll(self, v, h):
        self.log.append(("scroll", v, h))

    def key(self, keycode, down, flags):
        self.log.append(("key", keycode, down, flags))

    def flags_changed(self, keycode, flags):
        self.log.append(("flags", keycode, flags))


def make_mac(**kw):
    plat, sink, clock = FakeMac(), FakeSink(), FakeClock()
    router = Router(plat, sink, mac_cfg(**kw), clock=clock)
    sent = []
    poster = FakePoster(plat)
    g = MacGuest(router, poster, lambda m, o: sent.append((m, o)), clock=clock)
    router.on_enter_windows = g.end_local
    return g, router, plat, sink, poster, sent, clock


def test_mac_guest_moves_clicks_types_and_goes_back_across_the_edge():
    g, router, plat, sink, poster, sent, clock = make_mac(side="right")
    router.set_auto_scale(1.0)
    assert g.enter(0.5) == {"active": True}
    assert abs(plat.warps[-1][0] - (1440 - 1 - 4)) < 1e-6 and abs(plat.warps[-1][1] - 0.5 * 899) < 1e-6
    g.move(-100, 10)
    assert poster.log[-1] == ("move", 1335.0, 459.5, None)
    g.button(0, True)
    g.move(5, 0)
    assert poster.log[-1][-1] == 0                               # dragging with the left button
    g.button(0, False)
    clock.t += 0.1
    g.button(0, True)                                            # a second click soon after: a double click
    assert poster.log[-1] == ("button", 0, True, 2)
    g.button(0, False)
    g.scroll(120, 0)
    g.scroll(-60, 240)
    assert ("scroll", 3, 0) in poster.log and poster.log[-1] == ("scroll", -1, -6)
    g.key(True, 0, 0x1D, 0xA2)                                   # Ctrl = ⌘
    g.key(True, 0, 0x2E, 0x43)                                   # C
    assert poster.log[-2] == ("flags", K.MAC_LCMD, K.FLAG_COMMAND | 0x0008)
    assert poster.log[-1] == ("key", 8, True, K.FLAG_COMMAND | 0x0008)
    # back across the Mac's right edge (Windows is on the right): armed after a moment, then a push
    clock.t += 1.0
    g.move(500, 0)
    assert g.active
    g.move(5, 0)
    assert not g.active and sent[-1][0] == GU.MSG_G_EXIT
    assert abs(sent[-1][1]["ratio"] - plat.warps[-1][1] / 899) < 0.02
    released = [e for e in poster.log if e[0] in ("key", "flags")][-2:]
    assert ("key", 8, False, K.FLAG_COMMAND | 0x0008) in released and ("flags", K.MAC_LCMD, 0) in released
    n = len(poster.log)
    g.move(-10, 0)
    g.key(True, 0, 0x1E, 0x41)
    assert len(poster.log) == n                                  # nothing after it went back


def test_mac_guest_respects_the_arrangement_walls():
    g, router, plat, sink, poster, sent, clock = make_mac(side="bottom", layout={"side": "bottom", "offset": 300.0})
    router.set_win_monitors([Rect(0, 0, 1920, 1080)], [1.0])
    router.set_auto_scale(1.0)
    g.enter(1000 / 1919)                                         # Windows x=1000 -> Mac x=1300
    assert abs(plat.warps[-1][0] - 1300) < 1e-6
    clock.t += 1.0
    plat.warps.append((100.0, 895.0))                            # the Mac's own trackpad moved it to the left
    g.move(0, 10)
    g.move(0, 10)
    assert g.active and not sent                                 # x=100: no Windows below, a wall
    plat.warps.append((700.0, 899.0))
    g.move(0, 10)
    assert not g.active and abs(sent[-1][1]["ratio"] * 1919 - 400) < 0.01


def test_windows_devices_bring_the_macs_home_and_the_macs_send_them_back():
    g, router, plat, sink, poster, sent, clock = make_mac(side="right")
    router.enter_center()                                        # the Mac's keyboard and mouse are on Windows
    assert router.remote
    sink.msgs.clear()
    g.enter(0.25)
    assert not router.remote and g.active and not plat.hidden    # the Mac's came home, shown at the crossing
    assert P.MSG_EXIT in sink.types() and abs(plat.warps[-1][1] - 0.25 * 899) < 1e-6
    clock.t += 5
    router.handle(InputEvent("move", x=1439, y=300, dx=4))       # the Mac's trackpad goes to Windows
    assert router.remote and not g.active                        # and Windows's devices go home


def test_no_accessibility_no_guest():
    plat, sink = FakeMac(), FakeSink()
    router = Router(plat, sink, mac_cfg(), clock=FakeClock())
    g = MacGuest(router, FakePoster(plat), lambda m, o: None, trusted=lambda: False)
    reply = g.enter(0.5)
    assert reply["active"] is False and "Accessibility" in reply["error"] and not g.active


# ---- Windows's side --------------------------------------------------------------------------------------------
class FakeHooks:
    def __init__(self):
        self.mons = [Rect(0, 0, 1920, 1080)]
        self.pos = [960, 540]
        self.hooked = False
        self.hidden = False
        self.log = []

    def monitors(self):
        return self.mons

    def cursor_pos(self):
        return tuple(self.pos)

    def set_cursor(self, x, y):
        self.pos = [x, y]
        self.log.append(("cursor", x, y))

    def hide_cursor(self):
        self.hidden = True

    def show_cursor(self):
        self.hidden = False

    def capture(self, on):
        self.hooked = on


def make_win():
    hooks, sent, clock = FakeHooks(), [], FakeClock()
    g = WinGuest(hooks, lambda m, p=b"": sent.append((m, p)), clock=clock)
    return g, hooks, sent, clock


def test_windows_edge_push_captures_and_hotkey_returns():
    g, hooks, sent, clock = make_win()
    g.on_raw(5, 0)
    assert not g.capturing                                       # no edge known yet (the Mac has not said)
    g.configure("left", None)                                    # the Mac is to the left of this PC
    hooks.pos = [0, 540]
    g.on_raw(1, 2)                                               # along the edge: not enough push
    assert not g.capturing
    g.on_raw(-4, 0)                                              # pushed left past the edge
    assert g.capturing and hooks.hooked and hooks.hidden and hooks.pos == [960, 540]
    assert sent[-1][0] == GU.MSG_G_ENTER and abs(json.loads(sent[-1][1])["ratio"] - 540 / 1079) < 1e-6
    assert g.on_move(970, 535) is True and GU.unpack_move(sent[-1][1]) == (10.0, -5.0)
    assert g.on_button(0, True) and g.on_button(0, False)
    assert g.on_button(1, False) is False                        # pressed before: Windows gets its release
    assert g.on_wheel(120, 0) and GU.unpack_scroll(sent[-1][1]) == (120, 0)
    assert g.on_key(False, 0, 0x1D, 0xA2) is False               # a key held before (Ctrl): its release is local
    assert g.on_key(True, 0, 0x1E, 0x41) and GU.unpack_key(sent[-1][1]) == (True, 0, 0x1E, 0x41)
    assert g.on_key(False, 0, 0x1E, 0x41)
    assert g.on_key(True, P.KEYFLAG_EXTENDED, 0x30, 0xAF) is False   # volume up: stays on Windows
    assert g.on_key(True, 0, 0x21D, 0xA2) is True                     # AltGr's extra Ctrl: kept from Windows
    g.on_state({"active": True})
    g.on_key(True, 0, 0x1D, 0xA2)                                # Ctrl, Alt, then the arrow away from the Mac
    g.on_key(True, 0, 0x38, 0xA4)
    assert g.on_key(True, P.KEYFLAG_EXTENDED, 0x4D, ARROW_VK["right"]) is True
    assert not g.capturing and not hooks.hooked and not hooks.hidden and sent[-1][0] == GU.MSG_G_LEAVE
    assert hooks.pos == [0, 540]                                 # back where it was


def test_windows_walls_timeout_refusal_and_exit_at_ratio():
    g, hooks, sent, clock = make_win()
    g.configure("top", [0.5, 1.0])                               # only the right half faces the Mac
    hooks.pos = [200, 0]
    g.on_raw(0, -5)
    g.on_raw(0, -5)
    assert not g.capturing
    hooks.pos = [1500, 0]
    g.on_raw(0, -5)
    assert g.capturing
    clock.t += 3                                                 # the Mac never answered
    g.tick()
    assert not g.capturing and sent[-1][0] == GU.MSG_G_LEAVE
    g.hotkey()
    assert g.capturing and json.loads(sent[-1][1]) == {"ratio": None}
    g.on_state({"active": False, "error": "no Accessibility permission"})
    assert not g.capturing and "Accessibility" in g.last_error
    g.hotkey()
    g.on_state({"active": True})
    g.on_exit({"ratio": 0.75})                                  # the Mac sent it back across its edge
    assert not g.capturing and hooks.pos == [int(0.75 * 1919), 3]
    g.hotkey()
    g.on_mac_enter()                                             # the Mac's own devices arrive
    assert not g.capturing
    g.on_disconnect()
    g.hotkey()
    assert not g.capturing                                       # no Mac, no edge
    g2, hooks2, _, _ = make_win()
    g2.enabled = lambda: False
    g2.configure("left", None)
    g2.hotkey()
    assert not g2.capturing                                      # turned off in Settings


def test_outbox_merges_waiting_moves():
    got, gate = [], threading.Event()

    def send(m, p):
        gate.wait(2)
        got.append((m, p))
    box = OutBox(send)
    box(GU.MSG_G_KEY, GU.pack_key(True, 0, 0x1E, 0x41))
    time.sleep(0.05)
    for _ in range(5):
        box(GU.MSG_G_MOVE, GU.pack_move(1, 2))
    box(GU.MSG_G_BUTTON, GU.pack_button(0, True))
    gate.set()
    deadline = time.time() + 3
    while len(got) < 3 and time.time() < deadline:
        time.sleep(0.01)
    assert [m for m, _ in got] == [GU.MSG_G_KEY, GU.MSG_G_MOVE, GU.MSG_G_BUTTON]
    assert GU.unpack_move(got[1][1]) == (5.0, 10.0)


# ---- end to end -----------------------------------------------------------------------------------------------
def test_end_to_end_windows_mouse_visits_the_mac():
    from mirrorway.mac.server import MirrorwayServer
    from mirrorway.win.client import ControlClient, Resolver
    from mirrorway.win.remote import RemoteControl
    from test_e2e import free_port, wait
    from test_router_remote import FakeWin

    port = free_port()
    plat = FakeMac()
    poster = FakePoster(plat)
    srv = MirrorwayServer(mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", side="right"), plat, beacon=False,
                          guest_poster=poster)
    srv.start()
    hooks = FakeHooks()
    wcfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN")
    holder = {}
    guest = WinGuest(hooks, OutBox(lambda m, p: holder["c"]._send_raw(m, p)))
    c = ControlClient(wcfg, Resolver(wcfg, None), RemoteControl(FakeWin(), lambda m, o: None),
                      netinfo_fn=lambda: None, monitors_fn=lambda: [Rect(0, 0, 1920, 1080)], guest=guest)
    holder["c"] = c
    c.start()
    try:
        wait(lambda: srv.connected() and c.auth_mode == "key" and guest.edge == "left", timeout=20,
             msg="connected, edge known")
        srv.router.set_auto_scale(1.0)
        hooks.pos = [0, 270]
        guest.on_raw(-5, 0)
        wait(lambda: srv.guest.active and guest.confirmed, timeout=10, msg="on the Mac")
        assert abs(plat.warps[-1][1] - 270 / 1079 * 899) < 1.0
        guest.on_move(guest.park[0] - 300, guest.park[1])
        wait(lambda: poster.log and poster.log[-1][0] == "move", timeout=5, msg="moved on the Mac")
        assert abs(poster.log[-1][1] - (1435 - 300)) < 1e-6
        guest.on_key(True, 0, 0x1E, 0x41)
        guest.on_key(False, 0, 0x1E, 0x41)
        wait(lambda: ("key", 0, False, 0) in poster.log, timeout=5, msg="typed on the Mac")
        time.sleep(0.9)                                          # the Mac's edge arms
        for dx in (400, 10, 10):                                 # to the edge, then pushing against it
            guest.on_move(guest.park[0] + dx, guest.park[1])
            time.sleep(0.15)                                     # (moves sent together are merged)
        wait(lambda: not guest.capturing, timeout=5, msg="back on Windows")
        assert not srv.guest.active and hooks.pos[0] == 3 and not hooks.hidden
    finally:
        c.stop()
        srv.stop()
