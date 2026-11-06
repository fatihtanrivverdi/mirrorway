"""Adaptive picture quality (bitrate / frame rate follow the connection)."""
import socket
import time

import numpy as np
import pytest
from conftest import SLOW_CI_PLATFORM, SLOW_CI_REASON, DictCfg

from mirrorway import adaptive as A
from mirrorway import h264 as H
from mirrorway.mac.server import MirrorwayServer
from mirrorway.win.client import Resolver, VideoClient
from test_e2e import free_port, wait
from test_router_remote import FakeClock, FakeMac, mac_cfg


# ---- controller ------------------------------------------------------------------
def ctl(**kw):
    clock = FakeClock()
    c = A.RateController(kw.get("start", 14000), kw.get("min", 1500), kw.get("max", 30000), kw.get("fps", 60),
                         clock=clock)
    return c, clock


def test_decrease_on_queue_then_fps_at_minimum():
    c, clock = ctl()
    assert c.update(queue_ms=200, blocked=0.0, rtt_ms=3, sent_kbps=14000)
    assert c.kbps == 9800 and "send queue" in c.last_reason
    for _ in range(8):
        clock.t += 1
        c.update(queue_ms=0, blocked=0.9, rtt_ms=3, sent_kbps=1000)
    assert c.kbps == 1500                       # lower limit
    assert c.fps < 60                           # then the frame rate drops
    fps_low = c.fps
    clock.t += 1
    c.update(queue_ms=500, blocked=0.9, rtt_ms=3, sent_kbps=500)
    assert c.fps < fps_low and c.fps >= 15


def test_recovery_restores_fps_first_then_bitrate_with_hold():
    c, clock = ctl(start=4000, min=800, max=8000, fps=30)
    c.update(queue_ms=500, blocked=0, rtt_ms=3, sent_kbps=4000)   # 2800
    assert c.kbps == 2800
    c.fps = 20                                                    # (as if lowered to the bottom)
    clock.t += 1
    assert not c.update(queue_ms=1, blocked=0.01, rtt_ms=3, sent_kbps=2700)   # 3 s not over yet
    clock.t += 3
    assert c.update(queue_ms=1, blocked=0.01, rtt_ms=3, sent_kbps=2700) and c.fps == 30
    clock.t += 3.1
    assert not c.update(queue_ms=1, blocked=0.01, rtt_ms=3, sent_kbps=2700)   # 10 s have not passed since the drop
    clock.t += 4
    assert c.update(queue_ms=1, blocked=0.01, rtt_ms=3, sent_kbps=2700) and c.kbps == 3220
    # if the budget is not used (the screen is still), it is not raised
    clock.t += 4
    assert not c.update(queue_ms=1, blocked=0.01, rtt_ms=3, sent_kbps=500)
    # it does not exceed the ceiling
    for _ in range(30):
        clock.t += 4
        c.update(queue_ms=1, blocked=0.01, rtt_ms=3, sent_kbps=c.kbps)
    assert c.kbps == 8000 and c.increases > 3


def test_rtt_spike_needs_two_samples():
    c, clock = ctl()
    c.update(None, 0.0, 3.0, 0)                 # minimum 3 ms
    clock.t += 1
    assert not c.update(None, 0.0, 120.0, 0)    # a single spike: ignore
    clock.t += 1
    assert not c.update(None, 0.0, 4.0, 0)
    clock.t += 1
    c.update(None, 0.0, 120.0, 0)
    clock.t += 1
    assert c.update(None, 0.0, 130.0, 0) and c.kbps == 9800 and "round trip" in c.last_reason


def test_unsent_bytes_linux_socket():
    a, b = socket.socketpair()
    try:
        a.sendall(b"x" * 1000)
        n = A.unsent_bytes(a)
        assert n is None or n >= 0
    finally:
        a.close()
        b.close()


# ---- encoder bitrate ------------------------------------------------------------
@pytest.mark.skipif(not H.encoder_available(), reason="no libx264")
def test_x264_bitrate_change_takes_effect():
    rng = np.random.default_rng(0)
    enc = H.AVEncoder(320, 192, 30, 4000)

    def avg(n=25):
        return sum(len(enc.encode(rng.integers(0, 255, (192, 320, 4), dtype=np.uint8))[0]) for _ in range(n)) / n

    high = avg()
    enc.set_bitrate(500)
    low = avg()
    assert low < high * 0.5


# ---- end to end: slow connection ------------------------------------------------------
class NoiseCapture:
    """Frames that cannot be compressed (noise): the encoder uses the whole bitrate."""
    name = "noise"
    self_paced = True

    def __init__(self):
        self.rng = np.random.default_rng(1)

    def grab(self, timeout=1.0):
        time.sleep(1 / 30)
        return self.rng.integers(0, 255, (192, 320, 4), dtype=np.uint8)

    def stop(self):
        pass


@pytest.mark.skipif(not (H.encoder_available() and H.decoder_available()), reason="needs PyAV")
@pytest.mark.skipif(SLOW_CI_PLATFORM, reason=SLOW_CI_REASON)
def test_slow_link_lowers_bitrate_and_recovers(monkeypatch):
    monkeypatch.setattr(A.RateController, "STABLE_S", 0.5)
    monkeypatch.setattr(A.RateController, "HOLD_AFTER_DROP_S", 1.0)
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, quality="low",
                  clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: NoiseCapture(), beacon=False)
    srv.vt_failed = True
    srv.start()
    ccfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN")
    got = []
    vc = VideoClient(ccfg, Resolver(ccfg, None), on_frame=lambda *a: None, on_status=lambda s: None,
                     target_size=lambda: (0, 0), on_video=lambda f, m: got.append(m))
    vc.start()
    link = {"bps": 1_000_000}                  # a 1 Mbit/s "connection"
    try:
        wait(lambda: srv.video is not None and got, timeout=8, msg="stream")
        v = srv.video
        assert v.adaptive and v.target_kbps == 4000
        orig_send = v.ch.send

        def slow_send(mtype, payload=b""):
            if link["bps"]:
                time.sleep(len(payload) * 8 / link["bps"])
            return orig_send(mtype, payload)

        v.ch.send = slow_send
        wait(lambda: v.target_kbps <= 1500, timeout=12, msg="the bitrate must drop")
        line = srv.stats_line()
        assert "target" in line and "(adaptive)" in line
        low, low_fps = v.target_kbps, v.target_fps
        link["bps"] = 0                                  # the connection recovered
        # recovery: first the frame rate (if it was lowered), then the bitrate if the encoder uses its budget
        wait(lambda: v.target_kbps > low or v.target_fps > low_fps, timeout=20, msg="the quality must rise again")
        assert srv.status()["video_target_kbps"] == v.target_kbps
    finally:
        vc.stop()
        srv.stop()


@pytest.mark.skipif(not (H.encoder_available() and H.decoder_available()), reason="needs PyAV")
def test_adaptive_can_be_disabled():
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, quality="low",
                  clipboard=False, adaptive_quality=False)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: NoiseCapture(), beacon=False)
    srv.vt_failed = True
    srv.start()
    ccfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN")
    got = []
    vc = VideoClient(ccfg, Resolver(ccfg, None), on_frame=lambda *a: None, on_status=lambda s: None,
                     target_size=lambda: (0, 0), on_video=lambda f, m: got.append(m))
    vc.start()
    try:
        wait(lambda: srv.video is not None and got, timeout=8)
        assert not srv.video.adaptive and "(adaptive)" not in srv.stats_line()
    finally:
        vc.stop()
        srv.stop()
