"""The switch indicator: a ring where the pointer appears after switching computers (mirrorway/ring.py, the hooks
in the Mac router and Windows' RemoteControl, the Settings switches)."""
from conftest import DictCfg

from mirrorway import prefs as P
from mirrorway import ring as R
from mirrorway.win.remote import RemoteControl
from test_router_remote import FakeClock, FakeWin, make_router


def test_ring_timing():
    assert R.at(0) == (R.R_START, R.W_START, 1.0)
    r, w, o = R.at(R.DURATION)
    assert (r, w, o) == (R.R_END, R.W_END, 0.0)
    assert R.at(-1) == R.at(0) and R.at(5) == R.at(R.DURATION)
    radii = [R.at(i / 100)[0] for i in range(51)]
    alphas = [R.at(i / 100)[2] for i in range(51)]
    assert radii == sorted(radii) and alphas == sorted(alphas, reverse=True)
    assert R.at(R.DURATION / 2)[0] > (R.R_START + R.R_END) / 2         # grows fast, then slows down
    assert R.frame_count() == 30 and R.box() % 2 == 1 and R.box(2.0) > 2 * R.box() - 3
    side, border = R.layer_shape(10, 4, halo=1.5)
    assert border == 7 and side == 2 * (10 + 3.5)


def test_ring_frames():
    img = R.render(0.15, scale=1.0)
    s = R.box()
    assert img.size == (s, s) and img.mode == "RGBA"
    c = s // 2
    assert img.getpixel((c, c))[3] == 0                                # the pointer stays visible in the middle
    radius = round(R.at(0.15)[0])
    px = img.getpixel((c + radius, c))
    assert px[3] > 200 and px[2] > px[1]                               # violet on the ring
    halo = img.getpixel((c + radius + round(R.at(0.15)[1] / 2 + R.HALO / 2), c))
    assert halo[3] > 0 and min(halo[:3]) > 150                         # the white edge outside it
    alpha = [R.render(t).getchannel("A").getextrema()[1] for t in (0.05, 0.25, 0.45)]
    assert alpha[0] > alpha[1] > alpha[2] > 0                          # fades
    assert R.render(R.DURATION).getchannel("A").getextrema()[1] == 0


def test_premultiplied_bgra():
    from PIL import Image
    img = Image.new("RGBA", (2, 1), (200, 100, 50, 128))
    img.putpixel((1, 0), (10, 20, 30, 0))
    data = R.premultiplied_bgra(img)
    assert data[:4] == bytes((round(50 * 128 / 255), round(100 * 128 / 255), round(200 * 128 / 255), 128))
    assert data[4:] == bytes((0, 0, 0, 0))


def test_windows_frame_cache_and_switch_off():
    from mirrorway.win import ring as W
    calls = []

    def render(t, scale, ss):
        calls.append(scale)
        return R.render(t, scale, 1)
    cache = W.FrameCache(render)
    size, frames = cache.get(1.0)
    assert size == R.box(1.0) and len(frames) == R.frame_count() and len(frames[0]) == size * size * 4
    assert cache.get(1.04) is cache.get(1.0)                            # 104% draws like 100%
    assert cache.get(1.5)[0] == R.box(1.5) and set(calls) == {1.0, 1.5}
    ring = W.RingIndicator(enabled=lambda: False)
    ring.show(100, 100)                                                 # off: no window, no thread
    assert ring._thread is None and ring.shown == 0


def test_windows_reports_where_the_pointer_arrived():
    w, clock = FakeWin(), FakeClock()
    rc = RemoteControl(w, lambda m, o: None, clock=clock)
    seen = []
    rc.enter_listeners.append(lambda x, y: seen.append((x, y)))
    rc.enter_listeners.insert(0, lambda x, y: 1 / 0)                    # a failing listener does not stop the others
    rc.on_enter({"from": "left", "ratio": 0.5, "exit": "left"})
    assert len(seen) == 1 and seen[0][0] < 10 and abs(seen[0][1] - 539.5) < 2
    rc.on_enter({"from": "center", "pos": [800, 300]})
    assert seen[-1] == (800, 300)


def test_mac_router_reports_where_the_pointer_came_back():
    r, plat, sink, clock = make_router()
    back = []
    r.on_return = lambda x, y: back.append((x, y))
    r.enter_center()
    r.leave_from_client(0.5)
    assert back == [plat.warps[-1]]
    r.on_return = lambda x, y: 1 / 0                                    # an error in the ring does not break the switch
    clock.t += 1
    r.enter_center()
    assert r.leave_to_mac() and not r.remote


def test_settings_switches():
    from test_settings_pages import cfg, status
    g = {p.id: p for p in P.mac_pages(cfg(), status())}["general"]
    assert g.item("switch_ring").value is True and g.item("switch_ring").kind == "switch"
    assert {p.id: p for p in P.mac_pages(cfg(switch_ring=False), status())}["general"].item("switch_ring").value is False
    wg = {p.id: p for p in P.win_pages(DictCfg(), {})}["general"]
    assert wg.item("switch_ring").value is True
    assert {p.id: p for p in P.win_pages(DictCfg(switch_ring=False), {})}["general"].item("switch_ring").value is False
