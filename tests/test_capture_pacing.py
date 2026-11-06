"""A self-paced capture adds no extra wait; per-minute measurement summary."""
import time

import numpy as np
import pytest
from conftest import SLOW_CI_PLATFORM, SLOW_CI_REASON, DictCfg

from mirrorway import h264 as H
from mirrorway.mac.server import MirrorwayServer
from mirrorway.win.client import Resolver, VideoClient
from test_e2e import free_port, wait
from test_router_remote import FakeMac, mac_cfg
from test_changed_regions import bgra

pytestmark = pytest.mark.skipif(not (H.decoder_available() and H.encoder_available()),
                                reason="needs PyAV (h264 + libx264)")


class FastCapture:
    """A capture that delivers a new frame every 8 ms (it sets its own pace, like ScreenCaptureKit)."""
    name = "fast"

    def __init__(self, self_paced):
        self.self_paced = self_paced
        self.base = bgra(96, 160)
        self.n = 0

    def grab(self, timeout=1.0):
        time.sleep(0.008)
        self.n += 1
        f = self.base.copy()
        f[10:20, (self.n * 4) % 140:(self.n * 4) % 140 + 10] = (0, 0, 255, 255)
        return f

    def stop(self):
        pass


def _run(self_paced, seconds=1.2):
    cap = FastCapture(self_paced)
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, quality="low",
                  clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: cap, beacon=False)
    srv.vt_failed = True
    srv.start()
    ccfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN")
    got = []
    vc = VideoClient(ccfg, Resolver(ccfg, None), on_frame=lambda *a: None, on_status=lambda s: None,
                     target_size=lambda: (0, 0), on_video=lambda f, m: got.append(time.monotonic()))
    vc.start()
    try:
        wait(lambda: got, timeout=5)
        t0, n0 = time.monotonic(), len(got)
        time.sleep(seconds)
        rate = (len(got) - n0) / (time.monotonic() - t0)
        return rate, srv
    finally:
        vc.stop()
        srv.stop()


@pytest.mark.skipif(SLOW_CI_PLATFORM, reason=SLOW_CI_REASON)
def test_self_paced_capture_is_not_throttled_again():
    paced, _ = _run(self_paced=False)         # the old behaviour: fixed at 30 fps (low)
    free, _ = _run(self_paced=True)
    assert paced < 34
    assert free > paced * 1.5, (paced, free)


def test_minute_summary_and_stats_line():
    from test_changed_regions import ArrayCapture
    cap = ArrayCapture()
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: cap, beacon=False)
    srv.start()
    ccfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN")
    stats = {"fps": 25.0, "kbps": 10.0, "decode_ms": 2.0, "display_ms": 1.0, "latency_ms": 12.0,
             "decoder": "D3D11VA", "renderer": "direct3d11"}
    vc = VideoClient(ccfg, Resolver(ccfg, None), on_frame=lambda *a: None, on_status=lambda s: None,
                     target_size=lambda: (0, 0), on_video=lambda f, m: None, stats_fn=lambda: dict(stats))
    vc.STATS_INTERVAL = 0.3
    vc.start()
    try:
        wait(lambda: srv.video is not None and len(srv.video.client_hist) >= 2, timeout=6, msg="Windows measurements")
        stats["latency_ms"] = 30.0
        n = len(srv.video.client_hist)
        wait(lambda: len(srv.video.client_hist) > n, timeout=3)
        wait(lambda: srv.video.minute["frames"] > 0, timeout=5, msg="2 s window")
        m = srv.video.minute_summary()
        assert m["frames"] > 0 and m["peak_fps"] >= m["fps"] * 0.5 and m["peak_kbps"] > 0
        c = m["client"]
        assert c["decoder"] == "D3D11VA" and c["peak_latency"] == 30.0 and 12.0 < c["latency_ms"] < 30.0
        line = srv.stats_line(reset=True)
        assert "avg" in line and "peak" in line and "Windows: avg 25 fps" in line
        assert "(D3D11VA)" in line and "(direct3d11)" in line
        assert srv.video.minute["frames"] == 0 and srv.video.client_hist == []      # reset
        assert "fps" in srv.stats_line()          # with an empty summary it falls back to current values
    finally:
        vc.stop()
        srv.stop()


def test_real_backends_are_self_paced():
    from mirrorway.mac import capture as C
    assert all(b.self_paced for b in C.BACKENDS)
    assert np is not None
