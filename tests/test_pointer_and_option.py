"""Automatic pointer speed, Unicode characters with Option, partial drawing on Windows."""
import time

import numpy as np
from conftest import DictCfg

from mirrorway import protocol as P
from mirrorway.geometry import Rect
from mirrorway.keymap import FLAG_COMMAND, FLAG_OPTION, FLAG_SHIFT
from mirrorway.mac.router import PASS, SUPPRESS, InputEvent, Router, printable
from mirrorway.mac.server import MirrorwayServer
from mirrorway.win.client import ControlClient, Resolver, VideoClient
from mirrorway.win.remote import RemoteControl
from test_e2e import free_port, wait
from test_router_remote import FakeClock, FakeMac, FakeSink, FakeWin, mac_cfg
from test_changed_regions import ArrayCapture

OPT_L = FLAG_OPTION | 0x20       # left Option down (device bit)


class TextSink(FakeSink):
    def supports(self, feature):
        return feature == "text"


def router(sink_cls=TextSink, **kw):
    plat, sink = FakeMac(), sink_cls()
    r = Router(plat, sink, mac_cfg(**kw), clock=FakeClock())
    r.enter_center()
    sink.msgs.clear()
    return r, sink


def test_printable():
    assert printable("@") and printable("€") and printable("ğ") and printable(" ")
    assert not printable("") and not printable("\t") and not printable("\r") and not printable("")


def test_auto_speed():
    r, sink = router(speed=1.5)
    r.set_auto_scale(1920 / 1440)
    r.handle(InputEvent("move", x=723, y=450))
    dx, dy = P.unpack_move(sink.msgs[-1][1])
    assert abs(dx - 3 * 1.5 * 1920 / 1440) < 1e-3
    r2, sink2 = router(speed=1.0, auto_speed=False)
    r2.set_auto_scale(2.0)
    r2.handle(InputEvent("move", x=723, y=450))
    assert P.unpack_move(sink2.msgs[-1][1]) == (3.0, 0.0)
    r.set_auto_scale(50)
    assert r.auto_scale == 4.0                      # clamped


def test_option_character_is_sent_as_text():
    r, sink = router()
    assert r.handle(InputEvent("flags", keycode=58, flags=OPT_L)) == PASS       # Option bas
    assert sink.msgs == []                                                       # no Alt sent
    assert r.handle(InputEvent("key_down", keycode=12, flags=OPT_L, text="@")) == SUPPRESS
    assert r.handle(InputEvent("key_down", keycode=12, flags=OPT_L, text="@")) == SUPPRESS  # repeat
    assert r.handle(InputEvent("key_up", keycode=12, flags=OPT_L)) == SUPPRESS
    assert r.handle(InputEvent("flags", keycode=58, flags=0)) == PASS            # release Option
    assert sink.msgs == [(P.MSG_TEXT, "@".encode()), (P.MSG_TEXT, "@".encode())]


def test_option_with_tab_acts_as_alt():
    r, sink = router()
    r.handle(InputEvent("flags", keycode=58, flags=OPT_L))
    r.handle(InputEvent("key_down", keycode=48, flags=OPT_L, text="\t"))         # Option+Tab
    r.handle(InputEvent("key_up", keycode=48, flags=OPT_L))
    r.handle(InputEvent("key_down", keycode=48, flags=OPT_L, text="\t"))         # Tab once more
    r.handle(InputEvent("flags", keycode=58, flags=0))
    keys = [P.unpack_key(p) for m, p in sink.msgs if m == P.MSG_KEY]
    assert keys[0] == (True, 0, 0x38, 0)                                         # Alt bas
    assert keys[1] == (True, 0, 0x0F, 0) and keys[2] == (False, 0, 0x0F, 0)     # Tab
    assert keys[-1] == (False, 0, 0x38, 0)                                       # release Alt


def test_option_with_cmd_click_and_shift():
    r, sink = router()
    r.handle(InputEvent("flags", keycode=58, flags=OPT_L))
    r.handle(InputEvent("key_down", keycode=7, flags=OPT_L | FLAG_COMMAND | 0x08, text="≈"))  # Cmd+Opt+X
    keys = [P.unpack_key(p) for m, p in sink.msgs if m == P.MSG_KEY]
    assert keys[0] == (True, 0, 0x38, 0) and keys[1] == (True, 0, 0x2D, 0)
    r2, sink2 = router()
    r2.handle(InputEvent("flags", keycode=58, flags=OPT_L))
    r2.handle(InputEvent("down", button=0))                                      # Alt+click
    assert sink2.types() == [P.MSG_KEY, P.MSG_BUTTON]
    r3, sink3 = router()
    r3.handle(InputEvent("flags", keycode=56, flags=FLAG_SHIFT | 0x02))          # Shift goes to Windows
    r3.handle(InputEvent("flags", keycode=58, flags=OPT_L | FLAG_SHIFT | 0x02))
    r3.handle(InputEvent("key_down", keycode=14, flags=OPT_L | FLAG_SHIFT, text="´"))
    assert sink3.msgs[-1] == (P.MSG_TEXT, "´".encode())


def test_option_without_client_support_keeps_old_behaviour():
    r, sink = router(sink_cls=FakeSink)
    r.handle(InputEvent("flags", keycode=58, flags=OPT_L))
    assert sink.msgs and P.unpack_key(sink.msgs[0][1]) == (True, 0, 0x38, 0)
    r2, sink2 = router(option_unicode=False)
    r2.handle(InputEvent("flags", keycode=58, flags=OPT_L))
    assert P.unpack_key(sink2.msgs[0][1]) == (True, 0, 0x38, 0)


def test_remote_on_text():
    typed = []
    w = FakeWin()
    w.type_text = typed.append
    rc = RemoteControl(w, lambda m, o: None)
    rc.on_text("@")
    assert typed == []                    # no typing while inactive
    rc.on_enter({"from": "center", "ratio": 0.5, "exit": "left"})
    rc.on_text("€")
    assert typed == ["€"]


def test_text_and_auto_scale_end_to_end():
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=False, clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), beacon=False)
    srv.start()
    w = FakeWin()
    typed = []
    w.type_text = typed.append
    ccfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN", clipboard=False)
    holder = {}
    c = ControlClient(ccfg, Resolver(ccfg, None), RemoteControl(w, lambda m, o: holder["c"].send_json(m, o)),
                      monitors_fn=lambda: [Rect(0, 0, 1920, 1080), Rect(-1920, 0, 1920, 1080)])
    holder["c"] = c
    c.start()
    try:
        wait(lambda: srv.connected())
        assert abs(srv.router.auto_scale - 1920 / 1440) < 1e-6
        assert srv.supports("text")
        srv.router.enter_center()
        wait(lambda: c.remote.active)
        srv.router.handle(InputEvent("flags", keycode=58, flags=OPT_L))
        srv.router.handle(InputEvent("key_down", keycode=12, flags=OPT_L, text="@"))
        wait(lambda: typed == ["@"], msg="@ typed")
    finally:
        c.stop()
        srv.stop()


def test_video_patches_scaled_and_consistent():
    cap = ArrayCapture()
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, video_codec="jpeg",
                  quality="high", clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: cap, beacon=False)
    srv.start()
    ccfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN")
    state = {"full": None, "size": None, "patches": 0}

    def on_frame(img, meta):
        state["full"] = np.array(img).copy()
        state["size"] = img.size

    def on_patches(size, patches, meta):
        assert size == state["size"]
        for x, y, img in patches:
            a = np.asarray(img)
            state["full"][y:y + a.shape[0], x:x + a.shape[1]] = a
        state["patches"] += 1

    vc = VideoClient(ccfg, Resolver(ccfg, None), on_frame=on_frame, on_status=lambda s: None,
                     target_size=lambda: (480, 300), on_patches=on_patches,
                     needs_full=lambda size: state["size"] != tuple(size))
    vc.start()
    try:
        wait(lambda: state["patches"] >= 15, msg="patches")
        time.sleep(0.3)
        assert state["size"] == (480, 300)
        # the picture updated with patches must be close to the last frame scaled down directly
        from PIL import Image
        ref = np.asarray(Image.fromarray(np.ascontiguousarray(cap.last[..., [2, 1, 0]])).resize((480, 300),
                                                                                              Image.BILINEAR))
        assert np.abs(state["full"].astype(int) - ref.astype(int)).mean() < 6
    finally:
        vc.stop()
        srv.stop()
