"""The hardware encoder is not abandoned for good after a temporary problem; a new video session does not open the
encoder before the old one has finished."""
from mirrorway.mac.server import MirrorwayServer
from test_e2e import free_port, wait
from test_router_remote import FakeMac, mac_cfg
from test_gpu_viewer_and_pipeline import FmtCapture, _client, install_async_vt


def test_vt_failure_backoff_escalates(monkeypatch):
    import mirrorway.mac.server as S
    t = {"now": 1000.0}
    monkeypatch.setattr(S.time, "monotonic", lambda: t["now"])
    srv = MirrorwayServer(mac_cfg(port=free_port()), FakeMac(), beacon=False)
    assert not srv.vt_blocked()
    srv.note_vt_failure()
    assert srv.vt_blocked() and srv.vt_temporarily_blocked() and not srv.vt_failed
    t["now"] += 61
    assert not srv.vt_blocked()
    srv.note_vt_failure()
    t["now"] += 61
    assert srv.vt_blocked()                                 # second failure: 10 min
    t["now"] += 600
    assert not srv.vt_blocked()
    srv.note_vt_ok()                                        # running without problems resets the counter
    srv.note_vt_failure()
    srv.note_vt_failure()
    srv.note_vt_failure()
    assert srv.vt_failed and srv.vt_blocked() and not srv.vt_temporarily_blocked()   # 3 in a row: permanent


def test_hardware_encoder_is_retried_after_transient_error(monkeypatch):
    calls = install_async_vt(monkeypatch, error_after=5)
    caps = []

    def factory(i, preset):
        caps.append(preset.get("pixel_format"))
        if len(caps) == 2:                                  # switched to software: the hardware failure was a one-off
            calls["submits"] = -10 ** 9
        return FmtCapture(preset.get("pixel_format", "bgra"))

    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=factory, beacon=False)
    srv.VT_BLOCK_S = (6.0, 600.0)          # longer than a reconnect: the software encoder shows up in between
    srv.start()
    got = []
    vc = _client(port, got)
    vc.start()
    try:
        wait(lambda: len(caps) >= 3, timeout=30, msg="hardware encoder retried")
        assert caps[:3] == ["nv12", "bgra", "nv12"]
        n = len(got)
        wait(lambda: len(got) > n + 3 and "VideoToolbox" in srv.status()["video_encoder"], timeout=10,
             msg="continuing with the hardware encoder")
        assert not srv.vt_failed
    finally:
        vc.stop()
        srv.stop()


def test_new_video_session_waits_for_old_one(monkeypatch):
    """When a new viewer connects, the old session closes and the new one does not start before it has finished."""
    import threading
    import time

    import mirrorway.mac.server as S
    order = []
    orig_run = S.VideoSession.run

    def run(self):
        order.append(("start", id(self)))
        try:
            orig_run(self)
        finally:
            time.sleep(0.3)                                 # as if closing the encoder takes a while
            order.append(("bitti", id(self)))

    monkeypatch.setattr(S.VideoSession, "run", run)
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, clipboard=False,
                  video_codec="jpeg")
    from test_changed_regions import ArrayCapture
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: ArrayCapture(), beacon=False)
    srv.start()
    got1, got2 = [], []
    vc1 = _client(port, got1, on_video=False)
    vc1.start()
    try:
        wait(lambda: got1, timeout=8, msg="first viewer")
        first = srv.video
        vc2 = _client(port, got2, on_video=False)
        vc2.start()
        wait(lambda: got2, timeout=8, msg="second viewer")
        i_end = order.index(("bitti", id(first)))
        i_start = [k for k, (ev, sid) in enumerate(order) if ev == "start" and sid != id(first)][0]
        assert i_end < i_start, order
        vc2.stop()
    finally:
        vc1.stop()
        srv.stop()
    assert threading.active_count() < 200
