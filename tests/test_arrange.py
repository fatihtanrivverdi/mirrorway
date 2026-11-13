"""3.5: the screen arrangement — Windows' monitors placed next to the Mac, the pointer crossing straight, walls where
no screen faces the other, and the Settings picture."""
import json

from conftest import DictCfg
from test_router_remote import FakeClock, FakeMac, FakeSink, FakeWin, mac_cfg

from mirrorway import arrange as AR
from mirrorway import protocol as P
from mirrorway.geometry import Rect
from mirrorway.mac.router import InputEvent, Router
from mirrorway.win.remote import RemoteControl

MAC = [Rect(0, 0, 1800, 1169)]                                           # Fatih's MacBook (points)
WIN = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080)]                # two monitors side by side (pixels)


def test_edge_extent_and_scale():
    assert AR.edge_extent(MAC, "bottom") == (0, 1800)
    assert AR.edge_extent(WIN, "top") == (0, 3840)                      # both monitors form the top edge
    assert AR.edge_extent(WIN, "left") == (0, 1080)                     # only the left one forms the left edge
    tall = [Rect(0, 0, 1920, 1080), Rect(1920, -200, 1080, 1920)]
    assert AR.edge_extent(tall, "top") == (1920, 1080)                  # the higher monitor alone
    assert AR.edge_scale(WIN, [1.5, 1.5], "top") == 1.5
    assert AR.edge_scale(WIN, [1.0, 2.0], "top") == 1.5
    assert AR.edge_scale(WIN, [], "top") == 1.0 and AR.edge_scale(WIN, [9.0, 9.0], "top") == 1.0


def test_crossing_is_straight_and_round_trips():
    g = AR.Geometry(MAC, WIN, [1.0, 1.0], "bottom")
    lay = AR.Layout("bottom", -960.0)                  # the Mac sits over the middle: Windows x 960 .. 2760
    q = g.mac_to_win(lay, 0.0)
    assert abs(q * 3839 - 960) < 1e-6                  # Mac x=0 lands on Windows x=960
    q = g.mac_to_win(lay, 1.0)
    assert abs(q * 3839 - (1799 + 960)) < 1e-6
    for r in (0.0, 0.1, 0.33, 0.5, 0.9, 1.0):
        assert abs(g.win_to_mac(lay, g.mac_to_win(lay, r)) - r) < 1e-9   # back through the same spot
    lo, hi = g.win_span(lay)
    assert abs(lo * 3839 - 960) < 1e-6 and abs(hi * 3839 - 2759) < 1e-6
    assert g.win_to_mac(lay, 0.0) == 0.0 and g.win_to_mac(lay, 1.0) == 1.0   # outside the span: clamped


def test_walls_where_no_screen_faces_the_other():
    g = AR.Geometry(MAC, WIN, [1.0, 1.0], "bottom")
    lay = AR.Layout("bottom", 500.0)                   # Windows starts 500 pt in: the Mac's left 500 pt are a wall
    assert g.mac_to_win(lay, 0.0) is None
    assert g.mac_to_win(lay, 499 / 1799) is None
    assert g.mac_to_win(lay, 501 / 1799) is not None
    lo, hi = g.win_span(lay)
    assert lo == 0.0 and abs(hi * 3839 - 1299) < 1e-6  # only Windows x 0 .. 1299 lead back
    assert g.win_span(AR.Layout("bottom", 5000.0)) is None


def test_display_scale_makes_windows_smaller():
    g = AR.Geometry(MAC, WIN, [1.5, 1.5], "bottom")     # 3840 px at 150 % = 2560 pt
    assert abs(g.wl_pt - 2560) < 1e-9
    lay = AR.Layout("bottom", 0.0)
    q = g.mac_to_win(lay, 900 / 1799)                   # 900 pt into Windows = 1350 px
    assert abs(q * 3839 - 1350) < 1.0
    group = g.win_group(lay)
    assert group[0] == Rect(0, 1169, 1280, 720) and group[1] == Rect(1280, 1169, 1280, 720)


def test_group_placement_on_each_side_and_centred_default():
    for side, first in (("bottom", Rect(-1020, 1169, 1920, 1080)), ("top", Rect(-1020, -1080, 1920, 1080)),
                        ("right", Rect(1800, 44.5, 1920, 1080)), ("left", Rect(-3840, 44.5, 1920, 1080))):
        g = AR.Geometry(MAC, WIN, None, side)
        grp = g.win_group(None)                         # no arrangement: centred on that side
        assert grp[0] == first, (side, grp[0])


def test_snap_picks_the_nearest_side_and_keeps_an_overlap():
    lay = AR.snap(MAC, WIN, [1.0, 1.0], Rect(-1000, 1300, 3840, 1080))
    assert lay.side == "bottom" and lay.offset == -1000
    lay = AR.snap(MAC, WIN, [1.0, 1.0], Rect(2000, 100, 3840, 1080))
    assert lay.side == "right" and lay.offset == 100   # the right edge: only the left monitor faces the Mac
    lay = AR.snap(MAC, WIN, [1.0, 1.0], Rect(-3850, 1300, 3840, 1080))
    assert lay.side == "bottom" and lay.offset == AR.MIN_OVERLAP - 3840   # pulled back to share 48 pt
    lay = AR.snap(MAC, WIN, [1.0, 1.0], Rect(1790, 1300, 3840, 1080))
    assert lay.side == "bottom" and lay.offset == 1800 - AR.MIN_OVERLAP
    assert AR.from_cfg(lay.to_cfg(), "bottom") == AR.Layout("bottom", lay.offset)
    assert AR.from_cfg(lay.to_cfg(), "right") is None  # another side chosen elsewhere: the whole edge again
    assert AR.from_cfg(None, "bottom") is None and AR.from_cfg({"side": "bottom", "offset": "x"}, "bottom") is None


def test_picture_drag_and_shared_edge():
    model = {"mac": [[0, 0, 1800, 1169]], "win": [[0, 0, 1920, 1080], [1920, 0, 1920, 1080]],
             "scales": [1.0, 1.0], "side": "bottom", "offset": None}
    p = AR.Picture(model, 664, 276)
    k, x0, y0 = p.fit
    assert 0 < k < 0.2
    mac_v = p.to_view(p.mac[0])
    assert mac_v.x >= 0 and mac_v.right <= 664 and mac_v.y >= 0
    assert all(0 <= p.to_view(r).x and p.to_view(r).right <= 664 for r in p.win)
    x1, y1, x2, y2 = p.shared_edge()                    # the whole Mac edge crosses (no arrangement)
    assert y1 == y2 and abs(x1 - mac_v.x) < 1e-6 and abs(x2 - mac_v.right) < 1e-6
    w0 = p.to_view(p.win[0])
    assert p.hit(w0.x + 5, w0.y + 5) and not p.hit(mac_v.x + 5, mac_v.y + 5)
    p.begin()
    p.move(-200 * k, 0)                                 # drag 200 pt to the left
    assert p.win_now()[0].x == p.win[0].x - 200 and p.shared_edge() is None
    lay = p.end()
    assert lay.side == "bottom" and abs(lay.offset - (-1020 - 200)) < 1e-6
    assert p.layout == lay and p.drag is None
    x1, y1, x2, y2 = p.shared_edge()                    # still the whole Mac edge: Windows is wider
    assert abs(x2 - x1 - 1800 * k) < 1e-6


def _push_down(r, x):
    r.handle(InputEvent("move", x=x, y=899, dy=3))


def _enter_msg(sink):
    return next(json.loads(p) for m, p in reversed(sink.msgs) if m == P.MSG_ENTER)


def test_router_crosses_straight_with_walls_and_tells_windows_the_span():
    plat, sink, clock = FakeMac(), FakeSink(), FakeClock()
    cfg = mac_cfg(side="bottom", layout={"side": "bottom", "offset": 300.0})
    r = Router(plat, sink, cfg, clock=clock)
    r.set_win_monitors([Rect(0, 0, 1920, 1080)], [1.0])
    _push_down(r, 100)                                  # Mac x=100: no Windows below (it starts at 300): a wall
    assert not r.remote
    _push_down(r, 700)
    assert r.remote
    info = _enter_msg(sink)
    assert info["from"] == "top"
    assert abs(info["ratio"] * 1919 - 400) < 1e-6      # straight down: Windows x 400
    assert info["span"] == [0.0, round((1439 - 300) / 1919, 5)]
    # Windows comes back at x=1000 -> the Mac's x=1300
    r.leave_from_client(1000 / 1919)
    assert abs(plat.warps[-1][0] - 1300) < 1e-6
    # another side chosen in the menu: the arrangement no longer applies (the whole edge in proportion)
    cfg.set("side", "right")
    clock.t += 5
    r.handle(InputEvent("move", x=1439, y=450, dx=3))
    assert r.remote and "span" not in _enter_msg(sink)


def test_router_without_windows_monitors_uses_the_whole_edge():
    plat, sink, clock = FakeMac(), FakeSink(), FakeClock()
    r = Router(plat, sink, mac_cfg(side="bottom", layout={"side": "bottom", "offset": 300.0}), clock=clock)
    _push_down(r, 100)                                  # Windows' monitors not known yet: as before 3.5
    assert r.remote and "span" not in _enter_msg(sink)


def test_windows_walls_outside_the_span():
    w, sent, clock = FakeWin(), [], FakeClock()
    rc = RemoteControl(w, lambda m, o: sent.append((m, o)), clock=clock)
    rc.on_enter({"from": "top", "ratio": 0.5, "exit": "top", "span": [0.0, 0.3]})
    assert rc.exit_span == (0.0, 0.3)
    clock.t += 1
    w.pos = [1500, 0]                                   # x=1500 is past the span (0.3 * 1919 = 576): a wall
    rc.on_move(0, -3)
    rc.on_move(0, -3)
    assert rc.active and not sent
    w.pos = [300, 0]
    rc.on_move(0, -3)
    rc.on_move(0, -3)
    assert not rc.active and sent[-1][0] == P.MSG_LEAVE and abs(sent[-1][1]["ratio"] - 300 / 1919) < 1e-6
    rc.on_enter({"from": "top", "ratio": 0.5, "exit": "top"})       # an older Mac: no span, the whole edge
    assert rc.exit_span is None


def test_settings_page():
    from mirrorway import prefs
    cfg = DictCfg(side="bottom", layout=None)
    pages = {p.id: p for p in prefs.mac_pages(cfg, {}, {})}
    assert [p.id for p in prefs.mac_pages(cfg, {}, {})][:2] == ["general", "arrange"]
    assert pages["arrange"].item("arrange_wait")                      # no Windows monitors yet
    ctx = {"arrange": {"mac": [[0, 0, 1800, 1169]], "win": [[0, 0, 1920, 1080]], "scales": [1.25]}}
    page = {p.id: p for p in prefs.mac_pages(cfg, {}, ctx)}["arrange"]
    it = page.item("layout")
    assert it.kind == "arrange" and it.value["offset"] is None and it.value["side"] == "bottom"
    assert it.value["scales"] == [1.25] and not page.item("action:layout_reset").enabled
    assert page.item("arrange_state").title == "Whole edge"
    cfg.set("layout", {"side": "bottom", "offset": -100.0})
    page = {p.id: p for p in prefs.mac_pages(cfg, {}, ctx)}["arrange"]
    assert page.item("layout").value["offset"] == -100.0 and page.item("action:layout_reset").enabled
    assert page.item("arrange_state").title == "Real position"
