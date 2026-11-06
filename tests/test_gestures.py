"""Trackpad gestures: while Windows is controlled, multi-finger gestures on the Mac become their Windows counterparts."""
import json
import time

from conftest import DictCfg

from mirrorway import gestures as G
from mirrorway import protocol as P
from mirrorway.mac.router import PASS, SUPPRESS, InputEvent, Router
from mirrorway.protocol import KEYFLAG_EXTENDED, KEYFLAG_VK
from mirrorway.win.remote import RemoteControl


# ---- gesture recognition ---------------------------------------------------------------------------
def _swipe(tr, n, path):
    out = []
    for x, y in path:
        a = tr.touches(n, x, y)
        if a:
            out.append(a)
    return out


def test_three_finger_swipes_map_like_macos():
    tr = G.GestureTracker()
    assert _swipe(tr, 3, [(0.6, 0.5), (0.55, 0.5), (0.45, 0.51), (0.30, 0.52), (0.2, 0.5)]) == [
        {"action": "desktop_right", "fingers": 3}]                     # left = the desktop on the right, once
    tr.touches(0, 0, 0)
    assert _swipe(tr, 4, [(0.3, 0.5), (0.5, 0.5)]) == [{"action": "desktop_left", "fingers": 4}]
    tr.touches(0, 0, 0)
    assert _swipe(tr, 3, [(0.5, 0.3), (0.5, 0.45)])[0]["action"] == "task_view"   # up (y grows upwards)
    tr.touches(0, 0, 0)
    assert _swipe(tr, 3, [(0.5, 0.6), (0.52, 0.45)])[0]["action"] == "show_desktop"


def test_small_or_diagonal_moves_and_two_fingers_do_nothing():
    tr = G.GestureTracker()
    assert _swipe(tr, 3, [(0.5, 0.5), (0.56, 0.53)]) == []           # small
    tr.touches(0, 0, 0)
    assert _swipe(tr, 3, [(0.5, 0.5), (0.65, 0.62)]) == []           # diagonal: no dominant direction
    tr.touches(0, 0, 0)
    assert _swipe(tr, 2, [(0.5, 0.5), (0.1, 0.5)]) == []             # two fingers = scrolling, not a gesture


def test_finger_count_change_restarts_and_lift_allows_next_swipe():
    tr = G.GestureTracker()
    tr.touches(3, 0.5, 0.5)
    assert tr.touches(4, 0.30, 0.5) is None                         # 4th finger: the average jumped, start over
    assert tr.touches(4, 0.25, 0.5) is None
    assert tr.touches(4, 0.10, 0.5)["action"] == "desktop_right"
    assert tr.touches(4, 0.0, 0.5) is None                          # not a second time in the same gesture
    tr.touches(2, 0, 0)                                              # fingers lifting
    tr.touches(3, 0.2, 0.5)
    assert tr.touches(3, 0.4, 0.5)["action"] == "desktop_left"


def test_pinch_becomes_zoom_steps():
    tr = G.GestureTracker()
    got = [tr.magnify(0.03) for _ in range(5)]                      # total 0.15 = 2 notches
    steps = sum(a["steps"] for a in got if a)
    assert steps == 2
    assert tr.magnify(-0.2)["steps"] == -2
    tr.magnify(0.05, phase=G.PHASE_ENDED)                            # end: the remainder is reset
    assert tr.magnify(0.05) is None
    assert G.describe({"action": "zoom", "steps": -3}) == "zoom out (3)"
    assert G.describe({"action": "task_view", "fingers": 3}) == "Task View (3 fingers)"


def test_key_sequences():
    seq = G.key_sequence("desktop_right")
    assert seq == [(True, 0x5B, True), (True, 0x11, False), (True, 0x27, True),
                   (False, 0x27, True), (False, 0x11, False), (False, 0x5B, True)]
    assert G.key_sequence("unknown") == []


# ---- Mac router ---------------------------------------------------------------------------
class Sink:
    def __init__(self, features=("gestures",)):
        self.msgs = []
        self.features = features

    def connected(self):
        return True

    def supports(self, f):
        return f in self.features

    def send(self, m, p=b""):
        self.msgs.append((m, p))

    def send_json(self, m, obj):
        self.msgs.append((m, obj))


def _router(sink=None, **cfg):
    from test_router_remote import FakeClock, FakeMac, mac_cfg
    sink = sink or Sink()
    r = Router(FakeMac(), sink, mac_cfg(**cfg), clock=FakeClock())
    return r, sink


def _g(**kw):
    return InputEvent(kind="gesture", **kw)


def test_router_passes_gestures_to_mac_when_local():
    r, sink = _router()
    assert r.handle(_g(gtype=G.NS_GESTURE, fingers=3, gx=0.5, gy=0.5)) == PASS
    assert r.handle(_g(gtype=G.NS_MAGNIFY, magnification=0.5)) == PASS
    assert not sink.msgs


def test_router_suppresses_and_forwards_when_remote():
    r, sink = _router()
    r.enter_center()
    sink.msgs.clear()
    assert r.handle(_g(gtype=G.NS_BEGIN_GESTURE)) == SUPPRESS
    for x in (0.6, 0.5, 0.4):
        assert r.handle(_g(gtype=G.NS_GESTURE, fingers=3, gx=x, gy=0.5)) == SUPPRESS
    assert r.handle(_g(gtype=G.NS_MAGNIFY, magnification=0.16)) == SUPPRESS
    assert r.handle(_g(gtype=G.NS_ROTATE)) == SUPPRESS                 # an unrecognised one does not reach the Mac either
    sent = [o for m, o in sink.msgs if m == P.MSG_GESTURE]
    assert sent == [{"action": "desktop_right", "fingers": 3}, {"action": "zoom", "steps": 2}]


def test_router_gestures_can_be_turned_off_and_old_windows_gets_nothing():
    r, sink = _router(gestures=False)
    r.enter_center()
    assert r.handle(_g(gtype=G.NS_GESTURE, fingers=3, gx=0.5, gy=0.5)) == PASS
    old = Sink(features=())
    r2, _ = _router(sink=old)
    r2.enter_center()
    old.msgs.clear()
    for x in (0.6, 0.3):
        assert r2.handle(_g(gtype=G.NS_GESTURE, fingers=3, gx=x, gy=0.5)) == SUPPRESS
    assert not [m for m, _ in old.msgs if m == P.MSG_GESTURE]


def test_new_remote_session_resets_tracker():
    r, sink = _router()
    r.enter_center()
    r.handle(_g(gtype=G.NS_GESTURE, fingers=3, gx=0.6, gy=0.5))
    r.handle(_g(gtype=G.NS_GESTURE, fingers=3, gx=0.3, gy=0.5))        # fired, fingers still down
    r.force_local()
    r.enter_center()
    sink.msgs.clear()
    r.handle(_g(gtype=G.NS_GESTURE, fingers=3, gx=0.3, gy=0.5))
    r.handle(_g(gtype=G.NS_GESTURE, fingers=3, gx=0.6, gy=0.5))
    assert [o["action"] for m, o in sink.msgs if m == P.MSG_GESTURE] == ["desktop_left"]


# ---- Windows ------------------------------------------------------------------------------------
class KeyLog:
    def __init__(self):
        self.log = []

    def get_pos(self):
        return (10, 10)

    def monitors(self):
        from mirrorway.geometry import Rect
        return [Rect(0, 0, 1920, 1080)]

    def move_to(self, x, y):
        pass

    def key(self, down, flags, scan, vk):
        self.log.append(("key", down, flags, vk))

    def wheel(self, v, h):
        self.log.append(("wheel", v, h))

    def button(self, b, d):
        pass


def test_windows_applies_gestures_only_while_active():
    b = KeyLog()
    rc = RemoteControl(b, lambda m, o: None)
    rc.on_gesture({"action": "task_view"})
    assert b.log == []                                               # nothing while Windows is not controlled
    rc.on_enter({"from": "left", "ratio": 0.5})
    rc.on_gesture({"action": "task_view"})
    assert b.log == [("key", True, KEYFLAG_VK | KEYFLAG_EXTENDED, 0x5B), ("key", True, KEYFLAG_VK, 0x09),
                     ("key", False, KEYFLAG_VK, 0x09), ("key", False, KEYFLAG_VK | KEYFLAG_EXTENDED, 0x5B)]
    b.log.clear()
    rc.on_gesture({"action": "zoom", "steps": -2})
    assert b.log == [("key", True, KEYFLAG_VK, 0x11), ("wheel", -240, 0), ("key", False, KEYFLAG_VK, 0x11)]
    b.log.clear()
    rc.on_gesture({"action": "zoom", "steps": 999})
    assert b.log[1] == ("wheel", 2400, 0)                           # clamped
    b.log.clear()
    rc.on_gesture({"action": "rm -rf"})
    assert b.log == []


def test_end_to_end_gesture_reaches_windows():
    from mirrorway.geometry import Rect
    from mirrorway.mac.server import MirrorwayServer
    from mirrorway.win.client import ControlClient, Resolver
    from test_e2e import free_port, wait
    from test_router_remote import FakeMac, mac_cfg

    port = free_port()
    srv = MirrorwayServer(mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN"), FakeMac(), beacon=False)
    srv.start()
    wcfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN", clipboard=False)
    b = KeyLog()
    holder = {}
    rc = RemoteControl(b, lambda m, o: holder["c"].send_json(m, o))
    c = ControlClient(wcfg, Resolver(wcfg, None), rc, monitors_fn=lambda: [Rect(0, 0, 1920, 1080)],
                      netinfo_fn=lambda: None)
    holder["c"] = c
    c.start()
    try:
        wait(lambda: srv.connected() and c.auth_mode == "key" and srv.supports(G.FEATURE), timeout=20,
             msg="connection")
        srv.router.enter_center()
        wait(lambda: rc.active, msg="remote mode")
        for x in (0.7, 0.6, 0.4):
            srv.router.handle(InputEvent("gesture", gtype=G.NS_GESTURE, fingers=4, gx=x, gy=0.5))
        wait(lambda: ("key", False, KEYFLAG_VK | KEYFLAG_EXTENDED, 0x5B) in b.log, msg="Win+Ctrl+→")
        assert ("key", True, KEYFLAG_VK | KEYFLAG_EXTENDED, 0x27) in b.log
        time.sleep(0.1)
        assert json.dumps(b.log)                                     # key events only
    finally:
        c.stop()
        srv.stop()


# ---- macOS's own Dock gesture (Spaces / Mission Control) -----------------------------------------
def _dock(tr, dtype, values, end=G.HID_ENDED, t0=0.0):
    acts = []
    for i, v in enumerate(values):
        ph = G.HID_BEGAN if i == 0 else G.HID_CHANGED
        a = tr.dock(dtype, v, ph, t0 + i * 0.01)
        if a:
            acts.append(a)
    a = tr.dock(dtype, values[-1], end, t0 + len(values) * 0.01)
    if a:
        acts.append(a)
    return acts


def test_dock_swipe_directions_and_cancel():
    tr = G.GestureTracker()
    assert _dock(tr, 1, [-0.05, -0.2, -0.5]) == [{"action": "desktop_right"}]   # negative: fingers to the left
    assert tr.last_dock["type"] == 1 and tr.last_dock["n"] == 3
    assert _dock(tr, 1, [0.05, 0.3, 0.6]) == [{"action": "desktop_left"}]
    assert _dock(tr, 2, [-0.1, -0.4, -0.6]) == [{"action": "task_view"}]
    assert _dock(tr, 2, [0.1, 0.4, 0.6]) == [{"action": "show_desktop"}]
    assert _dock(tr, 3, [0.1, 0.4, 0.6]) == []                                  # pinch: no counterpart
    assert _dock(tr, 1, [-0.1, -0.4, -0.6], end=G.HID_CANCELLED) == []          # cancelled
    assert _dock(tr, 1, [-0.01, -0.02]) == []                                   # too short


def test_dock_swipe_without_phase_uses_threshold_once():
    tr = G.GestureTracker()
    acts = [tr.dock(1, v, 0, i * 0.02) for i, v in enumerate([-0.1, -0.2, -0.35, -0.5, -0.7])]
    assert [a for a in acts if a] == [{"action": "desktop_right"}]
    assert tr.dock(1, 0.1, 0, 2.0) is None                                      # a new gesture (after a pause)
    assert tr.dock(1, 0.5, 0, 2.01) == {"action": "desktop_left"}


def test_router_uses_dock_swipe_and_does_not_zoom():
    r, sink = _router()
    r.enter_center()
    sink.msgs.clear()
    # seen on a real Mac: NSEvent type 30 (magnify) but HID 23 — must not be taken for zooming
    for i, v in enumerate((-0.1, -0.3, -0.6)):
        r.handle(_g(gtype=G.NS_MAGNIFY, hid=23, magnification=0.5, hid_motion=1, hid_progress=v,
                    hid_phase=G.HID_BEGAN if i == 0 else G.HID_CHANGED))
    assert r.handle(_g(gtype=G.NS_MAGNIFY, hid=23, hid_motion=1, hid_progress=-0.6,
                       hid_phase=G.HID_ENDED)) == SUPPRESS
    assert [o for m, o in sink.msgs if m == P.MSG_GESTURE] == [{"action": "desktop_right"}]
    # if the same scroll is also recognised from the touches, it is not sent a second time
    for x in (0.6, 0.3):
        r.handle(_g(gtype=G.NS_GESTURE, fingers=3, gx=x, gy=0.5))
    assert len([m for m, _ in sink.msgs if m == P.MSG_GESTURE]) == 1
    # if the CGEvent fields are filled, they are used
    r.clock.t += 5
    for i, v in enumerate((0.1, 0.3, 0.6)):
        r.handle(_g(gtype=G.NS_MAGNIFY, hid=23, dock_type=2, dock_progress=v,
                    dock_phase=G.HID_BEGAN if i == 0 else G.HID_CHANGED))
    r.handle(_g(gtype=G.NS_MAGNIFY, hid=23, dock_type=2, dock_progress=0.6, dock_phase=G.HID_ENDED))
    assert [o for m, o in sink.msgs if m == P.MSG_GESTURE][-1] == {"action": "show_desktop"}
