"""Measurements in the top right corner of the screen window (in full screen), shown/hidden with F9."""
import os
import time

import numpy as np
import pytest
from conftest import DictCfg

from mirrorway import h264 as H
from mirrorway.win.overlay import font_px_for, overlay_lines, render_overlay
from test_e2e import wait

STATS = {"fps": 58.2, "kbps": 1700.0, "enc_ms": 11.8, "decode_ms": 3.4, "display_ms": 1.3,
         "latency_ms": 18.4, "rtt_ms": 3.1, "decoder": "D3D11VA", "renderer": "direct3d"}


def test_overlay_lines_content():
    info = {"codec": "h264", "target_kbps": 22000, "target_fps": 60, "adaptive": True}
    lines = overlay_lines(STATS, info)
    assert lines[0] == "58 fps · 1.7 MB/s"
    assert lines[1] == "H.264 · target 22 Mbit/s · 60 fps (auto)"
    assert lines[2] == "latency ≈ 18 ms · network 3 ms"
    assert lines[3] == "Mac 11.8 · decode 3.4 (D3D11VA) · draw 1.3 ms"
    assert overlay_lines({"fps": 12}, {"codec": "jpeg"})[1].startswith("JPEG")
    assert len(overlay_lines({}, {})) == 2           # without latency that line is left out


def test_render_overlay_is_translucent_box():
    img = render_overlay(overlay_lines(STATS, {"codec": "h264", "target_kbps": 14000}), font_px_for(1080))
    assert img.mode == "RGBA" and 150 < img.width < 900 and 40 < img.height < 300
    a = np.asarray(img)
    corner = a[a.shape[0] // 2, 2]                     # edge: the translucent dark background
    assert 120 < corner[3] < 230 and corner[:3].max() < 60
    assert a[..., :3].max() > 200                      # there is light text
    assert font_px_for(1080) == 16 and font_px_for(2160) == 28 and font_px_for(400) == 12


@pytest.fixture
def sdl_dummy(monkeypatch):
    from mirrorway.win import sdlview
    if not sdlview.available():
        pytest.skip("no PySDL2")
    monkeypatch.setenv("SDL_VIDEODRIVER", os.environ.get("MIRRORWAY_TEST_SDL_DRIVER", "dummy"))
    monkeypatch.setenv("SDL_RENDER_DRIVER", "software")


def _grab(v):
    v.captured = None
    v.capture_next = True
    v.submit_video(v._test_frame, {"bytes": 10, "decoder": "software"})
    wait(lambda: v.captured is not None, timeout=3, msg="drawing")
    w, h, data = v.captured
    return np.frombuffer(data, np.uint8).reshape(h, w, 4)


@pytest.mark.skipif(not (H.decoder_available() and H.encoder_available()), reason="needs PyAV")
def test_sdl_overlay_top_right_and_f9_toggle(sdl_dummy):
    from mirrorway.win.sdlview import SDLViewer
    prefs = DictCfg()
    v = SDLViewer("test", on_close=lambda: None, size_ratio=0.6, prefs=prefs,
                  info_fn=lambda: {"codec": "h264", "target_kbps": 14000, "target_fps": 60, "adaptive": True})
    try:
        w, h = 320, 192
        img = np.zeros((h, w, 4), np.uint8)
        img[...] = (60, 200, 40, 255)                  # a bright green frame (RGB 40,200,60)
        enc, dec = H.AVEncoder(w, h, 30, 3000), H.H264Decoder()
        v._test_frame = dec.decode(enc.encode(img, True)[0])[0]
        for _ in range(15):                            # fill the measurement window (fps > 0)
            v.submit_video(v._test_frame, {"bytes": 1000, "enc_ms": 10, "decode_ms": 3})
            time.sleep(0.08)

        def changed(a, b, top_right=True):
            hh, ww = a.shape[:2]
            ys, xs = (slice(0, hh // 3), slice(ww // 2, ww)) if top_right else (slice(hh // 2, hh), slice(0, ww // 2))
            d = np.abs(a[ys, xs, :3].astype(int) - b[ys, xs, :3].astype(int)).sum(axis=2)
            return int((d > 60).sum())

        base = _grab(v)
        assert not v.fullscreen and not v.overlay_visible()      # default in windowed mode: no box
        v.toggle_overlay()                             # F9
        wait(lambda: v.overlay_visible(), timeout=2)
        assert prefs.get("overlay_window") is True
        out = _grab(v)
        assert changed(out, base) > 200 and v.overlay_draws > 0      # a box with text in the top right
        assert changed(out, base, top_right=False) == 0             # the bottom left did not change
        assert "target 14 Mbit/s" in v._overlay_text() and "(auto)" in v._overlay_text()
        v.toggle_overlay()
        wait(lambda: not v.overlay_visible(), timeout=2)
        assert changed(_grab(v), base) == 0
        v.toggle_fullscreen()
        wait(lambda: v.fullscreen, timeout=2)
        assert v.overlay_visible()                     # default in full screen: on
        assert "F9" in v.title or v.frames_drawn
    finally:
        v.close()


def test_client_merges_partial_video_info():
    from mirrorway.win.client import Resolver, VideoClient
    ccfg = DictCfg(host="127.0.0.1", port=1, pair_code="X")
    vc = VideoClient(ccfg, Resolver(ccfg, None), on_frame=lambda *a: None, on_status=lambda s: None,
                     target_size=lambda: (0, 0))
    import json
    from mirrorway import protocol as P
    msgs = [{"backend": "SCK", "codec": "h264", "encoder": "VT", "target_kbps": 14000, "adaptive": True},
            {"target_kbps": 9800, "target_fps": 60, "adaptive": True, "reason": "queue"}]

    class Ch:
        def __init__(self):
            self.q = [(P.MSG_VIDEO_INFO, json.dumps(m).encode()) for m in msgs]
            self.closed = False

        def recv(self):
            if self.q:
                return self.q.pop(0)
            vc._stop_evt.set()
            return (P.MSG_PING, b"")

        def send(self, *a):
            pass

        def send_json(self, *a):
            pass

        def close(self):
            self.closed = True

    import mirrorway.win.client as CL
    ch = Ch()
    orig = CL.client_handshake
    CL.client_handshake = lambda sock, code, role, hello: (ch, {})
    orig_conn = CL.socket.create_connection
    CL.socket.create_connection = lambda *a, **k: type("S", (), {"settimeout": lambda s, t: None,
                                                                   "setsockopt": lambda s, *a: None,
                                                                   "close": lambda s: None})()
    orig_tune = CL.tune_socket
    CL.tune_socket = lambda s: None
    try:
        vc.run()
    finally:
        CL.client_handshake, CL.socket.create_connection, CL.tune_socket = orig, orig_conn, orig_tune
    assert vc.info["encoder"] == "VT" and vc.info["target_kbps"] == 9800 and vc.info["target_fps"] == 60
