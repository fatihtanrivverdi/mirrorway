"""Changed-region encoding, full-frame requests, forwarding Windows log records to the Mac."""
import logging
import socket
import time

import numpy as np
from conftest import DictCfg

from mirrorway import delta as D
from mirrorway import protocol as P
from mirrorway.mac.server import MirrorwayServer
from mirrorway.win.client import ControlClient, Resolver, VideoClient
from mirrorway.win.remote import RemoteControl
from test_e2e import free_port, wait
from test_router_remote import FakeMac, FakeWin, mac_cfg


def bgra(h=400, w=640, seed=1):
    rng = np.random.default_rng(seed)
    a = np.full((h, w, 4), 235, dtype=np.uint8)
    a[..., 3] = 255
    region = a[min(40, h // 4):h - min(40, h // 4):16, min(40, w // 4):w - min(40, w // 4)]
    region[...] = rng.integers(0, 80, size=region.shape, dtype=np.uint8)            # "lines of text"
    a[..., 3] = 255
    return a


def test_changed_rects_cases():
    a = bgra()
    assert D.changed_rects(None, a) is None
    assert D.changed_rects(a, a.copy()) == []
    b = a.copy()
    b[100:110, 70:90] = 0
    assert D.changed_rects(a, b) == [(64, 96, 32, 32)]
    c = a.copy()
    c[:, :] = 0
    assert D.changed_rects(a, c) is None                      # most of it changed -> full frame
    assert D.changed_rects(a, bgra(h=200)) is None            # the size changed
    # vertical merge: consecutive rows over the same column range become one rectangle
    d = a.copy()
    d[0:96, 0:64] = 1
    assert D.changed_rects(a, d) == [(0, 0, 64, 96)]
    # a half tile at the edge is clipped at the image boundary
    e = bgra(h=50, w=70)
    f = e.copy()
    f[45:50, 65:70] = 0
    assert D.changed_rects(e, f) == [(64, 32, 6, 18)]


def test_pack_unpack_and_canvas():
    a = bgra()
    b = a.copy()
    b[200:230, 300:350] = 10
    rects = D.changed_rects(a, b)
    parts = [(r, D.encode(b[r[1]:r[1] + r[3], r[0]:r[0] + r[2]], "BGRX", 95, 0)) for r in rects]
    w, h, seq, enc, flags, back = D.unpack_delta(D.pack_delta(640, 400, 9, 4.6, parts))
    assert (w, h, seq, enc, flags) == (640, 400, 9, 4, 0) and [p[0] for p in back] == rects
    c = D.Canvas()
    assert not c.apply(640, 400, back)                        # no canvas
    c.set_full(D.encode(a, "BGRX", 95, 0))
    assert not c.apply(320, 200, back)                        # size mismatch
    assert c.apply(640, 400, back)
    err = np.abs(c.arr.astype(int) - b[..., [2, 1, 0]].astype(int)).mean()
    assert err < 3


class ArrayCapture:
    """Like ScreenCaptureKit: returns a BGRA array, a small region changes in every frame."""
    name = "fake-sck"

    def __init__(self):
        self.base = bgra()
        self.n = 0

    def grab(self, timeout=1.0):
        time.sleep(0.03)
        self.n += 1
        f = self.base.copy()
        x = 40 + (self.n * 24) % 520
        f[300:316, x:x + 20] = (0, 0, 200, 255)             # "pointer/text"
        self.last = f
        return f

    def stop(self):
        pass


def make_server(capture):
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, video_codec="jpeg",
                  quality="high", clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: capture, beacon=False)
    srv.start()
    return srv, port


def test_delta_stream_end_to_end():
    cap = ArrayCapture()
    srv, port = make_server(cap)
    cfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN")
    frames = []
    vc = VideoClient(cfg, Resolver(cfg, None), on_frame=lambda img, meta: frames.append((img, meta)),
                     on_status=lambda s: None, target_size=lambda: (0, 0))
    vc.start()
    try:
        wait(lambda: len(frames) >= 25, msg="frames")
        time.sleep(2.2)
        st = srv.status()
        assert st["video_delta"] and st["video_area_pct"] < 10
        sizes = [m["bytes"] for _, m in frames]
        assert sizes[0] > 10 * max(sizes[5:])                # first a full frame, then small pieces
        img, _ = frames[-1]
        assert img.size == (640, 400)
        # the Windows canvas must be very close to the last frame on the Mac
        got = np.asarray(img).astype(int)
        ref = cap.last[..., [2, 1, 0]].astype(int)
        assert np.abs(got - ref).mean() < 4
        assert "changed area" in srv.stats_line()
    finally:
        vc.stop()
        srv.stop()


def test_keyframe_request_and_plain_client():
    cap = ArrayCapture()
    srv, port = make_server(cap)
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        ch, _ = P.client_handshake(sock, "ABCD-EFGH-JKMN", P.ROLE_VIDEO,
                                   {"name": "t", "features": ["frame2", "stats", "delta"]})
        sock.settimeout(5)
        kinds = []
        while len(kinds) < 6:
            m, _ = ch.recv()
            if m in (P.MSG_FRAME2, P.MSG_FRAME_DELTA):
                kinds.append(m)
        assert kinds[0] == P.MSG_FRAME2 and P.MSG_FRAME_DELTA in kinds[1:]
        ch.send(P.MSG_KEYFRAME_REQ)
        got_key = False
        for _ in range(10):
            m, _ = ch.recv()
            if m == P.MSG_FRAME2:
                got_key = True
                break
        assert got_key
        ch.close()
    finally:
        srv.stop()


def test_windows_logs_forwarded_to_mac(caplog):
    cap = ArrayCapture()
    srv, port = make_server(cap)
    cfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN", clipboard=False)
    holder = {}
    c = ControlClient(cfg, Resolver(cfg, None), RemoteControl(FakeWin(), lambda m, o: holder["c"].send_json(m, o)))
    holder["c"] = c
    caplog.set_level(logging.INFO, logger="mirrorway.server")
    c.enable_log_forwarding()
    c.start()
    try:
        # a client connecting with the old code registers its key and reconnects with it; send the warning after that
        wait(lambda: srv.connected() and c.auth_mode == "key" and c.state == "connected", msg="connection with the key")
        wait(lambda: any("Windows info" in r.getMessage() for r in caplog.records), msg="info")
        logging.getLogger("mirrorway.remote").warning("test warning %d", 42)
        logging.getLogger("mirrorway.server").warning("server record must not be forwarded")
        wait(lambda: any("Windows: [mirrorway.remote] test warning 42" in r.getMessage() for r in caplog.records),
             msg="forwarded warning")
        assert not any("Windows: [mirrorway.server]" in r.getMessage() for r in caplog.records)
    finally:
        logging.getLogger("mirrorway").removeHandler(c._log_handler)
        c.stop()
        srv.stop()
