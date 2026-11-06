import json

from conftest import DictCfg

from mirrorway import protocol as P
from mirrorway.geometry import Rect
from mirrorway.keymap import FLAG_CONTROL, FLAG_OPTION, FLAG_SHIFT
from mirrorway.mac.router import PASS, PASS_AS_MOVE, SUPPRESS, InputEvent, Router
from mirrorway.win.remote import MASK_VK, RemoteControl


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeMac:
    def __init__(self):
        self.rects = [Rect(0, 0, 1440, 900)]
        self.warps = []
        self.hidden = False

    def displays(self):
        return self.rects

    def main_display(self):
        return self.rects[0]

    def warp(self, x, y):
        self.warps.append((x, y))

    def hide_cursor(self):
        self.hidden = True

    def show_cursor(self):
        self.hidden = False

    def cursor_pos(self):
        return self.warps[-1] if self.warps else (100.0, 100.0)


class FakeSink:
    def __init__(self):
        self.msgs = []
        self.is_connected = True

    def connected(self):
        return self.is_connected

    def send(self, m, p=b""):
        self.msgs.append((m, p))

    def send_json(self, m, obj):
        self.msgs.append((m, json.dumps(obj).encode()))

    def types(self):
        return [m for m, _ in self.msgs]


def mac_cfg(**kw):
    base = dict(side="right", speed=1.0, scroll_speed=1.0, invert_scroll=False, swap_cmd_ctrl=True,
                left_option="alt", keyboard_layout="auto", edge_switch=True)
    base.update(kw)
    return DictCfg(**base)


def make_router(**kw):
    plat, sink, clock = FakeMac(), FakeSink(), FakeClock()
    r = Router(plat, sink, mac_cfg(**kw), clock=clock)
    return r, plat, sink, clock


def test_edge_enters_remote_and_forwards_motion():
    r, plat, sink, clock = make_router(speed=2.0)
    assert r.handle(InputEvent("move", x=800, y=450, dx=5)) == PASS
    assert not r.remote
    assert r.handle(InputEvent("move", x=1439, y=225, dx=4)) == PASS
    assert r.remote and plat.hidden
    m, p = sink.msgs[-1]
    assert m == P.MSG_ENTER
    info = json.loads(p)
    assert info["from"] == "left" and info["exit"] == "left" and abs(info["ratio"] - 0.25) < 1e-6
    assert plat.warps[-1] == (720.0, 450.0)          # parking point (centre of the main display)
    # movement comes from the position change (the delta field is ignored)
    assert r.handle(InputEvent("move", x=723, y=449, dx=99, dy=99)) == PASS_AS_MOVE
    assert sink.msgs[-1] == (P.MSG_MOVE, P.pack_move(6.0, -2.0))
    # a drag also passes as movement
    assert r.handle(InputEvent("drag", x=724, y=449)) == PASS_AS_MOVE
    assert sink.msgs[-1] == (P.MSG_MOVE, P.pack_move(2.0, 0.0))
    # once the pointer moves away from the parking point it is pulled back; the warp is not sent as movement
    n = len(plat.warps)
    r.handle(InputEvent("move", x=850, y=449))
    assert len(plat.warps) == n + 1
    sink.msgs.clear()
    r.handle(InputEvent("move", x=721, y=450))       # after the warp: park + (1, 0)
    assert sink.msgs == [(P.MSG_MOVE, P.pack_move(2.0, 0.0))]


def test_stale_event_after_warp_is_ignored():
    r, plat, sink, clock = make_router(side="top")
    # switch to Windows from the top edge: pointer (700, 0) -> park (720, 450)
    r.handle(InputEvent("move", x=700, y=0, dy=-3))
    assert r.remote
    sink.msgs.clear()
    # an event with a stale position recorded before the warp was processed -> must be dropped (no huge jump down)
    r.handle(InputEvent("move", x=700, y=0))
    assert sink.msgs == []
    r.handle(InputEvent("move", x=720, y=445))
    assert sink.msgs == [(P.MSG_MOVE, P.pack_move(0.0, -5.0))]
    # an excessively large single step is clamped
    sink.msgs.clear()
    clock.t += 1
    r.handle(InputEvent("move", x=720, y=445 - 500))
    dx, dy = P.unpack_move(sink.msgs[-1][1])
    assert abs(dy + 250) < 1e-3


def test_no_switch_when_disconnected_or_disabled():
    r, plat, sink, clock = make_router()
    sink.is_connected = False
    r.handle(InputEvent("move", x=1439, y=450, dx=4))
    assert not r.remote
    r2, _, sink2, _ = make_router(edge_switch=False)
    r2.handle(InputEvent("move", x=1439, y=450, dx=4))
    assert not r2.remote


def test_keys_buttons_scroll_in_remote():
    r, plat, sink, clock = make_router()
    r.enter_center()
    sink.msgs.clear()
    assert r.handle(InputEvent("key_down", keycode=0)) == SUPPRESS
    assert r.handle(InputEvent("key_up", keycode=0)) == SUPPRESS
    assert sink.msgs == [(P.MSG_KEY, P.pack_key(True, 0, 0x1E, 0)), (P.MSG_KEY, P.pack_key(False, 0, 0x1E, 0))]
    sink.msgs.clear()
    assert r.handle(InputEvent("down", button=0)) == SUPPRESS
    assert r.handle(InputEvent("up", button=0)) == SUPPRESS
    assert sink.types() == [P.MSG_BUTTON, P.MSG_BUTTON]
    sink.msgs.clear()
    assert r.handle(InputEvent("scroll", scroll_v=1, scroll_h=0, continuous=False)) == SUPPRESS
    assert sink.msgs[-1] == (P.MSG_SCROLL, P.pack_scroll(120, 0))
    r.handle(InputEvent("scroll", scroll_v=0.3, scroll_h=0, continuous=True))   # accumulates
    r.handle(InputEvent("scroll", scroll_v=0.3, scroll_h=-2, continuous=True))
    assert sink.msgs[-1] == (P.MSG_SCROLL, P.pack_scroll(1, 4))


def test_local_mode_passes_everything():
    r, plat, sink, clock = make_router()
    for ev in (InputEvent("key_down", keycode=0), InputEvent("key_up", keycode=0),
               InputEvent("down"), InputEvent("up"), InputEvent("scroll", scroll_v=1),
               InputEvent("flags", keycode=56, flags=FLAG_SHIFT | 2)):
        assert r.handle(ev) == PASS
    assert sink.msgs == []


def test_hotkeys_and_key_bookkeeping():
    r, plat, sink, clock = make_router()
    hot = FLAG_CONTROL | FLAG_OPTION
    # Ctrl+Option+→ : switch to Windows, the arrow key is swallowed (its release too)
    assert r.handle(InputEvent("key_down", keycode=124, flags=hot)) == SUPPRESS
    assert r.remote
    assert r.handle(InputEvent("key_down", keycode=124, flags=hot)) == SUPPRESS   # repeat
    assert r.handle(InputEvent("key_up", keycode=124, flags=hot)) == SUPPRESS
    assert P.MSG_KEY not in sink.types()
    # with a key held down on Windows, return with Ctrl+Option+←
    r.handle(InputEvent("key_down", keycode=1))
    assert r.handle(InputEvent("key_down", keycode=123, flags=hot)) == SUPPRESS
    assert not r.remote and not plat.hidden
    assert sink.types()[-2:] == [P.MSG_RELEASE_ALL, P.MSG_EXIT]
    # the release of the key that went to Windows now arrives on the Mac: not swallowed, but the Mac never saw the press -> PASS is harmless
    assert r.handle(InputEvent("key_up", keycode=123, flags=hot)) == SUPPRESS
    # a key pressed on the Mac before switching to remote mode: its repeats do not go to Windows, its release goes to the Mac
    r.handle(InputEvent("key_down", keycode=2))
    r.enter_center()
    sink.msgs.clear()
    assert r.handle(InputEvent("key_down", keycode=2)) == SUPPRESS
    assert r.handle(InputEvent("key_up", keycode=2)) == PASS
    assert sink.msgs == []


def test_hotkey_follows_side():
    r, plat, sink, clock = make_router(side="left")
    hot = FLAG_CONTROL | FLAG_OPTION
    assert r.handle(InputEvent("key_down", keycode=124, flags=hot)) == PASS   # → is no longer Windows
    assert r.handle(InputEvent("key_down", keycode=123, flags=hot)) == SUPPRESS
    assert r.remote


def test_modifier_flags_forwarded_but_passed():
    r, plat, sink, clock = make_router()
    r.enter_center()
    sink.msgs.clear()
    assert r.handle(InputEvent("flags", keycode=55, flags=0x100000 | 0x08)) == PASS   # Cmd pressed
    assert r.handle(InputEvent("flags", keycode=55, flags=0)) == PASS                 # released
    assert sink.msgs == [(P.MSG_KEY, P.pack_key(True, 0, 0x1D, 0)), (P.MSG_KEY, P.pack_key(False, 0, 0x1D, 0))]
    sink.msgs.clear()
    r.handle(InputEvent("flags", keycode=57, flags=0x10000))   # Caps Lock -> press/release
    assert len(sink.msgs) == 2


def test_leave_from_client_positions_cursor_at_edge():
    r, plat, sink, clock = make_router()
    r.enter_center()
    r.leave_from_client(0.5)
    assert not r.remote
    x, y = plat.warps[-1]
    assert x == 1440 - 1 - 4 and abs(y - 449.5) < 1
    # bounce-back protection
    r.handle(InputEvent("move", x=1439, y=450, dx=5))
    assert not r.remote
    clock.t += 1
    r.handle(InputEvent("move", x=1439, y=450, dx=5))
    assert r.remote


# ---------------------------------------------------------------------------
class FakeWin:
    def __init__(self):
        self.mons = [Rect(0, 0, 1920, 1080)]
        self.pos = [500, 500]
        self.log = []

    def get_pos(self):
        return tuple(self.pos)

    def monitors(self):
        return self.mons

    def move_to(self, x, y):
        r = self.mons[0]
        self.pos = [min(max(int(round(x)), r.left), r.right - 1), min(max(int(round(y)), r.top), r.bottom - 1)]
        self.log.append(("move", self.pos[0], self.pos[1]))

    def button(self, b, down):
        self.log.append(("btn", b, down))

    def wheel(self, v, h):
        self.log.append(("wheel", v, h))

    def key(self, down, flags, scan, vk):
        self.log.append(("key", down, flags, scan, vk))


def test_remote_control_enter_move_leave():
    w, sent, clock = FakeWin(), [], FakeClock()
    rc = RemoteControl(w, lambda m, o: sent.append((m, o)), clock=clock)
    rc.on_move(10, 0)          # ignored while inactive
    assert w.log == []
    rc.on_enter({"from": "left", "ratio": 0.5, "exit": "left"})
    assert rc.active and w.pos[0] == 3
    clock.t += 1
    rc.on_move(0.4, 0)
    rc.on_move(0.4, 0)         # fractions accumulate
    assert w.pos[0] == 3
    rc.on_move(0.4, 0)
    assert w.pos[0] == 4
    rc.on_key(True, 0, 0x1E, 0)
    rc.on_button(0, True)
    rc.on_move(-50, 0)         # x=0'a kenetlenir
    assert w.pos[0] == 0 and rc.active
    rc.on_move(-3, 0)          # pushed against the outer edge -> back to the Mac
    assert not rc.active
    assert sent and sent[-1][0] == P.MSG_LEAVE and 0.45 < sent[-1][1]["ratio"] < 0.55
    assert ("key", False, 0, 0x1E, 0) in w.log and ("btn", 0, False) in w.log


def test_remote_release_masks_start_menu():
    w, sent = FakeWin(), []
    rc = RemoteControl(w, lambda m, o: sent.append((m, o)))
    rc.on_enter({"from": "center", "ratio": 0.5, "exit": "left"})
    rc.on_key(True, P.KEYFLAG_EXTENDED, 0x5B, 0)   # Win pressed
    w.log.clear()
    rc.on_exit()
    assert w.log[0] == ("key", True, P.KEYFLAG_VK, 0, MASK_VK)
    assert ("key", False, P.KEYFLAG_EXTENDED, 0x5B, 0) in w.log
    assert not rc.active


def test_frozen_location_falls_back_to_delta_fields():
    r, plat, sink, clock = make_router()
    r.enter_center()
    sink.msgs.clear()
    for _ in range(4):
        r.handle(InputEvent("move", x=720, y=450, dx=3, dy=1))
    assert sink.msgs == []
    r.handle(InputEvent("move", x=720, y=450, dx=3, dy=1))
    assert sink.msgs == [(P.MSG_MOVE, P.pack_move(3.0, 1.0))]
