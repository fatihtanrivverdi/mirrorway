"""Second screen: one of the Windows monitors shows a virtual display of the Mac."""
import io
import json
import time

import pytest
from conftest import DictCfg

from mirrorway import extend as X
from mirrorway import protocol as P
from mirrorway.geometry import Rect
from mirrorway.mac.router import InputEvent, Router
from mirrorway.win.remote import RemoteControl

A = [0, 0, 1920, 1080]           # Windows main monitor
B = [-1920, 0, 1920, 1080]       # the monitor on its left: will be the Mac's second screen
MONS = [A, B]
MAIN = Rect(0, 0, 1800, 1168)    # MacBook display (points)


# ---- pure calculations ------------------------------------------------------------------------------
def test_choose_monitor_and_labels():
    assert X.choose_monitor(MONS) == 1                          # automatic: not the main one
    assert X.choose_monitor([A]) == 0                           # a single monitor
    assert X.choose_monitor([]) is None
    assert X.choose_monitor(MONS, B) == 1 and X.choose_monitor(MONS, A) == 0
    assert X.choose_monitor(MONS, [5000, 0, 1920, 1080]) == 0  # moved: the first monitor of the same size
    assert X.choose_monitor(MONS, [0, 0, 2560, 1440]) is None
    assert X.monitor_label(MONS, 0) == "Monitor 1 — 1920×1080 (main)"
    assert X.monitor_label(MONS, 1) == "Monitor 2 — 1920×1080 (left)"
    assert X.monitor_label([A, [1920, 0, 2560, 1440]], 1) == "Monitor 2 — 2560×1440 (right)"


def test_place_beside_main_display():
    assert X.place_beside(MAIN, 1920, 1080, "bottom") == (-60, 1168)
    assert X.place_beside(MAIN, 1920, 1080, "top") == (-60, -1080)
    assert X.place_beside(MAIN, 1920, 1080, "right") == (1800, 44)
    assert X.place_beside(MAIN, 1920, 1080, "left") == (-1920, 44)


def test_neighbors_and_entry_points():
    assert X.neighbor_sides(MONS, 1) == {"right": [0]}
    assert X.neighbor_sides(MONS, 0) == {"left": [1]}
    assert X.neighbor_sides([A], 0) == {}
    # leaving VD at the middle of its right edge: on Windows, A's left edge at the same height
    x, y = X.win_entry(MONS, 1, "right", 0.5)
    assert x == 2 and abs(y - 539.5) < 1e-6
    assert X.win_entry(MONS, 1, "left", 0.5) is None           # no monitor on its left
    # even if A is shorter than M, the point is clamped into A
    x, y = X.win_entry([[0, 0, 1920, 600], B], 1, "right", 0.9)
    assert x == 2 and y == 599
    # a monitor above
    x, y = X.win_entry([A, [0, -1080, 1920, 1080]], 1, "bottom", 0.25)
    assert y == 2 and abs(x - 0.25 * 1919) < 1e-6


def test_fractions_and_points():
    m = Rect(*B)
    assert X.m_fraction(m, -1, 540) == (1.0, pytest.approx(540 / 1079))
    assert X.m_fraction(m, -5000, 99999) == (0.0, 1.0)
    vd = Rect(-60, 1168, 1920, 1080)
    x, y = X.vd_point(vd, 1.0, 0.5, inset=4)
    assert x == vd.right - 1 - 4 and abs(y - (1168 + 0.5 * 1079)) < 1e-6
    assert X.crosses_into(m, 0, 540, "left") == (-1, 540)
    assert X.crosses_into(m, 0, 540, "right") is None
    assert X.toward([Rect(*A)], m) == ["left"]


def test_pick_display_for_viewer():
    assert X.pick_display([A, B], B) == 1
    assert X.pick_display([[0, 0, 1920, 1080], [1920, 0, 2560, 1440]], [5000, 0, 2560, 1440]) == 1  # by size
    assert X.pick_display([A, B], [-1000, 100, 100, 100]) == 1                                       # by centre
    assert X.pick_display([A], [9000, 9000, 800, 600]) is None


# ---- Mac router ---------------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Plat:
    def __init__(self, rects):
        self.rects = rects
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
        return (100.0, 100.0)


class Sink:
    def __init__(self):
        self.msgs = []

    def connected(self):
        return True

    def send(self, m, p=b""):
        self.msgs.append((m, p))

    def send_json(self, m, obj):
        self.msgs.append((m, obj))

    def last(self, mtype):
        return [o for m, o in self.msgs if m == mtype][-1]


class FakeExtend:
    def __init__(self, vd):
        self.vd = vd

    def route(self):
        return {"vd": self.vd, "sides": ["right"]}

    def win_point(self, side, ratio):
        return X.win_entry(MONS, 1, side, ratio)

    def m_rect(self):
        return list(map(float, B))

    def vd_rect(self):
        return self.vd


def _router(side="bottom"):
    vd = Rect(-60, 1168, 1920, 1080)
    plat, sink, clock = Plat([MAIN, vd]), Sink(), Clock()
    cfg = DictCfg(side=side, speed=1.0, scroll_speed=1.0, invert_scroll=False, swap_cmd_ctrl=True,
                  left_option="alt", keyboard_layout="auto", edge_switch=True)
    r = Router(plat, sink, cfg, clock=clock)
    r.extend = FakeExtend(vd)
    return r, plat, sink, clock, vd


def test_router_enters_windows_from_vd_side_facing_neighbor():
    r, plat, sink, clock, vd = _router()
    y = vd.top + 540
    for _ in range(3):                                     # you need to push against the edge (3 px)
        r.handle(InputEvent("move", x=vd.right - 1, y=y, dx=2))
    assert r.remote
    msg = sink.last(P.MSG_ENTER)
    assert msg["from"] == "left" and msg["extend"] == [-1920.0, 0.0, 1920.0, 1080.0]
    assert msg["pos"][0] == 2 and abs(msg["pos"][1] - 540 * 1079 / 1079) < 1.5


def test_router_inner_edge_between_main_and_vd_stays_on_mac():
    r, plat, sink, clock, vd = _router()
    for _ in range(5):
        r.handle(InputEvent("move", x=900, y=MAIN.bottom - 1, dy=3))   # bottom of the main display = inner edge to VD
    assert not r.remote


def test_router_vd_edges_without_windows_neighbor_are_walls():
    r, plat, sink, clock, vd = _router()
    for _ in range(6):
        r.handle(InputEvent("move", x=900, y=vd.bottom - 1, dy=2))     # no Windows monitor below VD
    assert not r.remote


def test_router_single_monitor_extend_uses_regular_edge_and_carries_extend():
    r, plat, sink, clock, vd = _router()
    r.extend.route = lambda: {"vd": vd, "sides": []}                   # Windows with one monitor
    for _ in range(3):
        r.handle(InputEvent("move", x=900, y=vd.bottom - 1, dy=2))
    assert r.remote
    msg = sink.last(P.MSG_ENTER)
    assert msg["from"] == "top" and "pos" not in msg and msg["extend"] == [-1920.0, 0.0, 1920.0, 1080.0]


def test_router_regular_return_lands_on_mac_main_display():
    """Returning from Windows's usual edge: to the Mac's own display, even if the virtual display covers that edge."""
    vd = Rect(-60, -1080, 1920, 1080)                                   # Windows is above the Mac: VD on top
    plat, sink, clock = Plat([MAIN, vd]), Sink(), Clock()
    cfg = DictCfg(side="top", speed=1.0, scroll_speed=1.0, invert_scroll=False, swap_cmd_ctrl=True,
                  left_option="alt", keyboard_layout="auto", edge_switch=True)
    r = Router(plat, sink, cfg, clock=clock)
    r.extend = FakeExtend(vd)
    r.enter_center()
    clock.t += 1
    r.leave_from_client(0.5)
    x, y = plat.warps[-1]
    assert MAIN.contains(x, y) and y == MAIN.top + 4


def test_router_returns_to_vd_point_when_windows_enters_m():
    r, plat, sink, clock, vd = _router()
    r.enter_center()
    assert r.remote
    clock.t += 1
    r.leave_from_client(0.5, vd=(1.0, 0.5))
    assert not r.remote and not plat.hidden
    x, y = plat.warps[-1]
    assert x == vd.right - 1 - 4 and abs(y - (vd.top + 0.5 * (vd.h - 1))) < 1e-6
    # pushing straight back does not switch (guard); moving away from the edge and back does
    r.handle(InputEvent("move", x=vd.right - 1, y=y, dx=3))
    r.handle(InputEvent("move", x=vd.right - 1, y=y, dx=3))
    assert not r.remote


# ---- Windows remote control ------------------------------------------------------------------------
class Win2:
    """Windows with two monitors: the pointer stays within the union of the desktop (like real Windows)."""

    def __init__(self, mons=(A, B)):
        self.mons = [Rect(*m) for m in mons]
        self.pos = [500, 500]
        self.log = []

    def get_pos(self):
        return tuple(self.pos)

    def monitors(self):
        return self.mons

    def move_to(self, x, y):
        from mirrorway.geometry import clamp_to_rects
        x, y = clamp_to_rects(self.mons, int(round(x)), int(round(y)))
        self.pos = [int(x), int(y)]
        self.log.append(("move", self.pos[0], self.pos[1]))

    def button(self, b, down):
        pass

    def wheel(self, v, h):
        pass

    def key(self, down, flags, scan, vk):
        pass


def test_windows_enters_at_given_point_and_leaves_into_m():
    w, sent, clock = Win2(), [], Clock()
    rc = RemoteControl(w, lambda m, o: sent.append((m, o)), clock=clock)
    rc.on_enter({"from": "left", "ratio": 0.5, "exit": "top", "pos": [2, 539.5], "extend": B})
    assert rc.active and w.pos == [2, 540]
    clock.t += 1                                         # the entry guard has passed
    rc.on_move(-50, 0)                                   # a big step: does not enter M, stops at the edge
    assert w.pos[0] == 0 and rc.active
    for _ in range(3):
        rc.on_move(-2, 0)
    assert not rc.active
    m, o = sent[-1]
    assert m == P.MSG_LEAVE and o["vd"][0] == 1.0 and abs(o["vd"][1] - 540 / 1079) < 1e-3


def test_windows_never_enters_m_from_entry_point_and_regular_exit_works():
    w, sent, clock = Win2(), [], Clock()
    rc = RemoteControl(w, lambda m, o: sent.append((m, o)), clock=clock)
    rc.on_enter({"from": "top", "ratio": 0.5, "exit": "top", "extend": B})
    assert w.pos[0] >= 0                                 # the middle of the union would be in M; placed on A
    clock.t += 1
    rc.on_move(0, -2000)
    for _ in range(3):
        rc.on_move(0, -2)
    assert not rc.active and sent[-1][0] == P.MSG_LEAVE and "vd" not in sent[-1][1]


def test_windows_single_monitor_extend_keeps_monitor():
    w, sent, clock = Win2(mons=(A,)), [], Clock()
    rc = RemoteControl(w, lambda m, o: sent.append((m, o)), clock=clock)
    rc.on_enter({"from": "top", "ratio": 0.5, "exit": "top", "extend": A})
    assert rc.active and rc._usable() == [Rect(*A)]
    clock.t += 1
    rc.on_move(10, 10)
    assert w.pos == [970, 13]


def test_windows_without_extend_unchanged():
    w, sent, clock = Win2(), [], Clock()
    rc = RemoteControl(w, lambda m, o: sent.append((m, o)), clock=clock)
    rc.on_enter({"from": "top", "ratio": 0.5, "exit": "top"})
    assert rc.extend_rect is None
    clock.t += 1
    rc.on_move(-3000, 0)                                  # moves freely to the left monitor
    assert w.pos[0] < 0


# ---- Mac: second screen management -------------------------------------------------------------------------
class FakeVD:
    instances = []

    def __init__(self, w, h, origin, fail=None):
        self.width, self.height, self.origin, self.fail = w, h, origin, fail
        self.info = {}
        self._alive = False
        self.stopped = False
        FakeVD.instances.append(self)

    @property
    def display_id(self):
        return int(self.info.get("id") or 0)

    def start(self):
        if self.fail:
            raise RuntimeError(self.fail)
        self._alive = True
        x, y = self.origin
        self.info = {"ok": True, "id": 77 + len(FakeVD.instances), "bounds": [x, y, self.width, self.height]}
        return self.info

    def alive(self):
        return self._alive

    def stop(self):
        self._alive, self.stopped = False, True


class FakeServerForExt:
    def __init__(self, monitors=MONS, features=("extend",)):
        self.cfg = DictCfg(extend_monitor=None, win_monitors=[])
        self.control = type("C", (), {"hello": {"monitors": monitors, "features": list(features)}})()
        self.video = None
        self.platform = type("P", (), {"main_display": lambda s: MAIN,
                                       "display_rect_id": lambda s, i: Rect(-60, 1168, 1920, 1080)})()
        self.router = type("R", (), {"side": "bottom"})()
        self.restarts = 0
        self.viewer_requests = 0

    def supports(self, f):
        return f in self.control.hello["features"]

    def restart_video(self):
        self.restarts += 1

    def request_viewer(self):
        self.viewer_requests += 1


def _mgr(**kw):
    from mirrorway.mac.extend_mgr import ExtendManager
    FakeVD.instances = []
    srv = FakeServerForExt(**kw)
    return ExtendManager(srv, vd_factory=lambda w, h, o: FakeVD(w, h, o)), srv


def test_manager_off_by_default_and_menu_choices():
    mgr, srv = _mgr()
    assert mgr.prepare() is None and not FakeVD.instances
    assert [c[0] for c in mgr.choices()] == ["Off", "Monitor 1 — 1920×1080 (main)",
                                             "Monitor 2 — 1920×1080 (left)"]
    mgr.remember_monitors()
    assert srv.cfg.get("win_monitors") == MONS


def test_manager_creates_reuses_and_releases_vd():
    mgr, srv = _mgr()
    srv.cfg.set("extend_monitor", B)
    info = mgr.prepare()
    assert info["w"] == 1920 and info["mrect"] == [-1920.0, 0.0, 1920.0, 1080.0] and info["id"] == 78
    vd = FakeVD.instances[0]
    assert vd.origin == (-60, 1168)                      # on the Mac, below the main display ("bottom")
    assert mgr.prepare()["id"] == 78 and len(FakeVD.instances) == 1      # the open one is reused
    route = mgr.route()
    assert route["sides"] == ["right"] and route["vd"] == Rect(-60, 1168, 1920, 1080)
    assert mgr.m_rect() == [-1920.0, 0.0, 1920.0, 1080.0]
    assert mgr.win_point("right", 0.0) == (2, 0)
    st = mgr.status()
    assert st["active"] and st["size"] == [1920, 1080]
    srv.cfg.set("extend_monitor", None)
    mgr.changed()
    assert vd.stopped and not mgr.active() and srv.restarts == 1 and mgr.route() is None


def test_manager_grace_period_after_viewer_closes():
    mgr, srv = _mgr()
    mgr.GRACE_S = 0.05
    srv.cfg.set("extend_monitor", "auto")
    mgr.prepare()
    vd = FakeVD.instances[0]
    mgr.session_ended()
    mgr.prepare()                                        # reconnected (e.g. an update): stays
    time.sleep(0.15)
    assert not vd.stopped
    mgr.session_ended()
    time.sleep(0.15)
    assert vd.stopped


def test_manager_refuses_old_windows_and_reports_failure():
    mgr, srv = _mgr(features=())
    srv.cfg.set("extend_monitor", B)
    assert mgr.prepare() is None and "needs an update" in mgr.last_error
    from mirrorway.mac.extend_mgr import ExtendManager
    srv2 = FakeServerForExt()
    srv2.cfg.set("extend_monitor", B)
    m2 = ExtendManager(srv2, vd_factory=lambda w, h, o: FakeVD(w, h, o, fail="desteklenmiyor"))
    assert m2.prepare() is None and "desteklenmiyor" in m2.last_error and not m2.active()


def test_manager_recreates_when_monitor_size_changes():
    mgr, srv = _mgr()
    srv.cfg.set("extend_monitor", "auto")
    mgr.prepare()
    srv.control.hello["monitors"] = [A, [-2560, 0, 2560, 1440]]
    info = mgr.prepare()
    assert info["w"] == 2560 and FakeVD.instances[0].stopped and len(FakeVD.instances) == 2


# ---- virtual display helper (main process side) ---------------------------------------------------------
class FakeProc:
    def __init__(self, out=b"", err=b"", exit_code=None, block=False):
        self.stdout = io.BytesIO(out) if not block else _Blocking()
        self.stderr = io.BytesIO(err)
        self.stdin = io.BytesIO()
        self._code = exit_code
        self.closed_stdin = False
        self.args = None

    def poll(self):
        return self._code

    def wait(self, timeout=None):
        self._code = 0 if self._code is None else self._code
        return self._code

    def terminate(self):
        self._code = -15

    def kill(self):
        self._code = -9


class _Blocking:
    def readline(self):
        time.sleep(5)
        return b""


def test_virtual_display_process_protocol():
    from mirrorway.mac.vdisplay import VirtualDisplay, VirtualDisplayError, product_id
    procs = []

    def popen(args, **kw):
        p = FakeProc(out=json.dumps({"ok": True, "id": 5, "bounds": [-60, 1168, 1920, 1080]}).encode() + b"\n")
        p.args, p.kw = args, kw
        procs.append(p)
        return p
    vd = VirtualDisplay(1920, 1080, origin=(-60, 1168), popen=popen, python="py")
    info = vd.start()
    assert info["id"] == 5 and vd.display_id == 5 and vd.alive()
    assert procs[0].args[:3] == ["py", "-m", "mirrorway.mac.vdisplay"] and "--x" in procs[0].args
    assert "PYTHONPATH" in procs[0].kw["env"]
    vd.stop()
    assert procs[0].stdin.closed and not vd.alive()

    bad = VirtualDisplay(1920, 1080, popen=lambda a, **k: FakeProc(
        out=b'{"ok": false, "error": "This macOS version has no CGVirtualDisplay"}\n'))
    with pytest.raises(VirtualDisplayError, match="no CGVirtualDisplay"):
        bad.start()
    dead = VirtualDisplay(1920, 1080, popen=lambda a, **k: FakeProc(err=b"Traceback\nImportError: objc\n",
                                                                     exit_code=1))
    with pytest.raises(VirtualDisplayError, match="ImportError: objc"):
        dead.start()
    hung = VirtualDisplay(1920, 1080, popen=lambda a, **k: FakeProc(block=True))
    with pytest.raises(VirtualDisplayError, match="timed out"):
        hung.start(timeout=0.1)
    assert product_id(1920, 1080) != product_id(2560, 1440) and 0x100 <= product_id(7680, 4320) <= 0xFFFF


def test_capture_by_display_id_uses_only_screencapturekit(monkeypatch):
    from mirrorway.mac import capture as C
    calls = []

    class SCK:
        name = "ScreenCaptureKit"
        supports_raw = True
        pixel_format = "nv12"

        def start(self, idx, max_w, fps, pixel_format="bgra", display_id=None):
            calls.append(("sck", display_id, max_w, pixel_format))

        def stop(self):
            pass

    class Other(SCK):
        name = "other"

        def start(self, *a, **k):
            calls.append(("other",))
    monkeypatch.setattr(C, "SCKBackend", SCK)
    monkeypatch.setattr(C, "BACKENDS", [Other, SCK])
    C.open_best(0, 1920, 60, pixel_format="nv12", display_id=99)
    assert calls == [("sck", 99, 1920, "nv12")]


# ---- end to end: Mac server + Windows clients -----------------------------------------------------
def test_end_to_end_extend_mode():
    from PIL import Image

    from mirrorway.mac.server import MirrorwayServer
    from mirrorway.win.client import ControlClient, Resolver, VideoClient
    from test_e2e import free_port, wait
    from test_router_remote import FakeMac, mac_cfg

    class Cap:
        name = "fake"

        def grab(self, timeout=1.0):
            time.sleep(0.03)
            return Image.new("RGB", (640, 360), (10, 20, 30))

        def stop(self):
            pass

    presets = []

    def capture_factory(idx, preset):
        presets.append(dict(preset))
        return Cap()

    FakeVD.instances = []
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, quality="low", side="bottom",
                  extend_monitor=None)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=capture_factory, beacon=False,
                      vd_factory=lambda w, h, o: FakeVD(w, h, o))
    srv.start()
    wcfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN", clipboard=False)
    views, infos = [], []
    win = Win2()
    holder = {}
    rc = RemoteControl(win, lambda m, o: holder["c"].send_json(m, o))
    c = ControlClient(wcfg, Resolver(wcfg, None), rc, monitors_fn=lambda: [Rect(*A), Rect(*B)],
                      netinfo_fn=lambda: None, on_view=views.append)
    holder["c"] = c
    c.start()
    vc = None
    try:
        wait(lambda: srv.connected() and c.auth_mode == "key" and srv.supports(X.FEATURE), timeout=20,
             msg="connection", state=lambda: (f"mac connected={srv.connected()} win={c.state}/{c.auth_mode} "
                                           f"features={(srv.control.hello.get('features') if srv.control else None)} "
                                           f"error={srv.last_error!r} devices={len(srv.devices())}"))
        vc = VideoClient(wcfg, Resolver(wcfg, None), on_frame=lambda img, meta=None: None,
                         on_status=lambda s: None, target_size=lambda: (0, 0), on_info=infos.append)
        vc.start()
        wait(lambda: infos and infos[-1].get("backend") == "fake", msg="mirror stream")
        assert "extend" not in infos[-1] and "display_id" not in presets[-1]

        cfg.set("extend_monitor", B)                    # from the menu: Monitor 2
        srv.extend.changed()
        wait(lambda: views, msg="request to open a window on Windows")
        assert views[-1] == {"open": True}
        wait(lambda: infos and (infos[-1].get("extend") or {}).get("rect") == [-1920.0, 0.0, 1920.0, 1080.0],
             timeout=10, msg="second screen stream")
        assert presets[-1]["display_id"] == FakeVD.instances[-1].display_id
        assert presets[-1]["max_w"] >= 1920
        assert srv.status()["extend"]["active"]

        # on the Mac the pointer goes to Windows from VD's right edge; pushing left (into M) on Windows returns to VD
        srv.platform.rects = [Rect(0, 0, 1440, 900), Rect(-240, 900, 1920, 1080)]
        srv.extend._rect_cache = (-1e9, None)
        srv.platform.display_rect_id = lambda i: Rect(-240, 900, 1920, 1080)
        for _ in range(3):
            srv.router.handle(InputEvent("move", x=1679, y=1440, dx=2))
        wait(lambda: rc.active, msg="VD'den Windows'a")
        assert win.pos[0] == 2 and rc.extend_rect == Rect(*B)
        time.sleep(0.9)
        srv.router.handle(InputEvent("move", x=500, y=400))       # pointer parked in the centre; left on Windows
        wait(lambda: win.pos[0] == 0, msg="left edge")
        for i in range(4):
            srv.router.handle(InputEvent("move", x=495 - i * 5, y=400))
        wait(lambda: not srv.router.remote, msg="back to the Mac on entering M")
        assert srv.platform.warps[-1][0] == 1679 - 4

        cfg.set("extend_monitor", None)                 # off: back to the mirror stream, VD goes away
        srv.extend.changed()
        wait(lambda: infos and infos[-1].get("backend") == "fake" and "extend" not in infos[-1],
             timeout=10, msg="back to the mirror stream")
        assert FakeVD.instances[-1].stopped
    finally:
        if vc is not None:
            vc.stop()
        c.stop()
        srv.stop()


def test_manager_recovers_when_helper_dies():
    mgr, srv = _mgr()
    srv.cfg.set("extend_monitor", B)
    mgr.prepare()
    FakeVD.instances[0]._alive = False                  # the helper process crashed
    st = mgr.status()
    assert not st["active"] and "closed unexpectedly" in st["error"] and mgr.vd is None
    deadline = time.time() + 2
    while srv.restarts == 0 and time.time() < deadline:
        time.sleep(0.01)
    assert srv.restarts == 1
    assert mgr.prepare()["id"] and len(FakeVD.instances) == 2 and mgr.last_error == ""
