"""A Windows window drawn by the graphics card (SDL2), trying hardware decoding, asynchronous (pipelined) H.264
encoding and NV12 capture on the Mac."""
import os
import queue
import sys
import threading
import time

import numpy as np
import pytest
from conftest import DictCfg

from mirrorway import h264 as H
from mirrorway.mac import capture as C
from mirrorway.mac.server import MirrorwayServer
from mirrorway.win.client import Resolver, VideoClient
from test_e2e import free_port, wait
from test_router_remote import FakeMac, mac_cfg
from test_changed_regions import bgra
from test_h264 import RawCapture, annexb_to_avcc_parts, install_fake_pyobjc, make_avcc_config, FakeSample

pytestmark = pytest.mark.skipif(not (H.decoder_available() and H.encoder_available()),
                                reason="needs PyAV (h264 + libx264)")


def test_target_size_is_always_even():
    assert C._target_size(1800, 1169, 1920) == (1800, 1168)
    assert C._target_size(3600, 2338, 1920) == (1920, 1246)
    assert C._target_size(1281, 801, 1280) == (1280, 800)
    w, h = C._target_size(1001, 333, 0)
    assert w % 2 == 0 and h % 2 == 0


# ---- asynchronous VideoToolbox (fake) ---------------------------------------------------
def install_async_vt(monkeypatch, error_after=None):
    """Like real VideoToolbox: output arrives on a separate thread, ~5 ms later, in order."""
    calls = install_fake_pyobjc(monkeypatch)
    vt = sys.modules["VideoToolbox"]
    calls["submits"] = 0
    calls["completes_called"] = 0

    def encode_handler(session, pb, pts, dur, props, flags_out, handler):
        if not hasattr(session, "q"):
            session.q = queue.Queue()

            def worker():
                while True:
                    item = session.q.get()
                    if item is None:
                        return
                    time.sleep(0.005)
                    h, sb, err = item
                    h(err, 0, sb)
            threading.Thread(target=worker, daemon=True).start()
        calls["submits"] += 1
        force = bool(props and props.get("ForceKeyFrame"))
        data, key = session.enc.encode(pb, force_key=force)
        sps, pps, body = annexb_to_avcc_parts(data)
        if sps:
            session.avcc = make_avcc_config(sps, pps)
        err = -12902 if (error_after is not None and calls["submits"] > error_after) else 0
        session.q.put((handler, FakeSample(body, key, session.avcc), err))
        return (0, 0)

    def complete(session, pts):
        calls["completes_called"] += 1
        q = getattr(session, "q", None)
        while q is not None and not q.empty():
            time.sleep(0.002)
        time.sleep(0.01)
        return 0

    vt.VTCompressionSessionEncodeFrameWithOutputHandler = encode_handler
    vt.VTCompressionSessionCompleteFrames = complete
    import mirrorway.mac.vtenc as V
    monkeypatch.setattr(V, "pixel_buffer_size", lambda pb: (pb.shape[1], pb.shape[0]))
    return calls


class FmtCapture(RawCapture):
    """A fake ScreenCaptureKit that reports the requested pixel format (bgra/nv12)."""

    def __init__(self, pixel_format="bgra", stop_after=None):
        super().__init__(stop_after=stop_after)
        self.pixel_format = pixel_format


def _client(port, got, on_video=True):
    ccfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN")
    kw = {}
    if on_video:
        kw["on_video"] = lambda frame, meta: got.append((frame, meta))
    return VideoClient(ccfg, Resolver(ccfg, None), on_frame=lambda img, meta=None: got.append((img, meta)),
                       on_status=lambda s: None, target_size=lambda: (0, 0), **kw)


def test_async_hardware_pipeline_low_latency(monkeypatch):
    calls = install_async_vt(monkeypatch)
    caps = []

    def factory(i, preset):
        caps.append(preset.get("pixel_format"))
        return FmtCapture(preset.get("pixel_format", "bgra"))

    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=factory, beacon=False)
    srv.start()
    got = []
    vc = _client(port, got)
    vc.start()
    try:
        wait(lambda: len(got) >= 8, timeout=10, msg="asynchronous frames")
        frame, meta = got[-1]
        assert hasattr(frame, "planes") and frame.format.name == "yuv420p"   # YUV goes to the GPU window
        assert meta["decoder"] == "software" and meta["bytes"] > 0
        assert caps[0] == "nv12"                                           # the hardware path asks for NV12
        assert "VideoToolbox" in srv.status()["video_encoder"] and not srv.vt_failed
        # output arrived without waiting for CompleteFrames; the encode time is the encoder's own time
        assert meta["enc_ms"] < 60
        assert calls["completes_called"] <= 2
    finally:
        vc.stop()
        srv.stop()


def test_last_frame_is_flushed_when_screen_goes_idle(monkeypatch):
    """An encoder that accumulates frames: when the screen stops, the last frame is still delivered."""
    install_fake_pyobjc(monkeypatch)            # output only on CompleteFrames
    import mirrorway.mac.vtenc as V
    monkeypatch.setattr(V, "pixel_buffer_size", lambda pb: (pb.shape[1], pb.shape[0]))
    cap = FmtCapture("nv12", stop_after=3)
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: cap, beacon=False)
    srv.start()
    got = []
    vc = _client(port, got)
    vc.start()
    try:
        wait(lambda: len(got) >= 3, timeout=5, msg="last frame delivered")
    finally:
        vc.stop()
        srv.stop()


def test_hardware_error_in_nv12_restarts_with_software(monkeypatch):
    install_async_vt(monkeypatch, error_after=5)
    caps = []

    def factory(i, preset):
        caps.append(preset.get("pixel_format"))
        return FmtCapture(preset.get("pixel_format", "bgra"))

    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=factory, beacon=False)
    srv.start()
    got = []
    vc = _client(port, got)
    vc.start()
    try:
        wait(lambda: srv.vt_blocked() and len(caps) >= 2, timeout=10, msg="re-setup")
        n = len(got)
        wait(lambda: len(got) > n + 3 and "x264" in srv.status()["video_encoder"], timeout=10,
             msg="continuing with the software encoder")
        assert caps[:2] == ["nv12", "bgra"]
    finally:
        vc.stop()
        srv.stop()


def test_keyframe_request_is_repeated_if_lost():
    sent = []

    class Ch:
        def send(self, t, p=b""):
            sent.append(t)

    ccfg = DictCfg(host="127.0.0.1", port=1, pair_code="X")
    vc = VideoClient(ccfg, Resolver(ccfg, None), on_frame=lambda *a: None, on_status=lambda s: None,
                     target_size=lambda: (0, 0))
    vc._h264 = H.H264Decoder()
    vc._h264_wait_key = True
    vc._key_req_t = time.monotonic() - 3
    from mirrorway import protocol as P
    vc._handle_h264(Ch(), P.pack_frame2(64, 64, 1, 1, b"\x00\x00\x00\x01\x41\x00", 0))
    assert sent == [P.MSG_KEYFRAME_REQ]
    vc._handle_h264(Ch(), P.pack_frame2(64, 64, 2, 1, b"\x00\x00\x00\x01\x41\x00", 0))
    assert sent == [P.MSG_KEYFRAME_REQ]          # not requested again within 2 s


def test_hw_decoder_falls_back_to_software():
    enc = H.AVEncoder(320, 192, 30, 2000)
    d = H.H264Decoder(hw="d3d11va")              # not available on Linux: must fall back to software
    frames = d.decode(enc.encode(bgra(192, 320), True)[0])
    assert len(frames) == 1 and d.kind == "software"
    d.fail_hw("test")
    assert d.kind == "software"


# ---- SDL window -------------------------------------------------------------------
def _sdl_ok():
    try:
        from mirrorway.win import sdlview
        return sdlview.available()
    except Exception:
        return False


@pytest.fixture
def sdl_dummy(monkeypatch):
    if not _sdl_ok():
        pytest.skip("no PySDL2")
    monkeypatch.setenv("SDL_VIDEODRIVER", os.environ.get("MIRRORWAY_TEST_SDL_DRIVER", "dummy"))
    monkeypatch.setenv("SDL_RENDER_DRIVER", "software")


def test_sdl_viewer_renders_yuv_with_bt709_colors(sdl_dummy):
    from mirrorway.win.sdlview import SDLViewer
    closed = []
    v = SDLViewer("test", on_close=lambda: closed.append(1), size_ratio=0.5)
    try:
        w, h = 320, 192
        img = np.zeros((h, w, 4), np.uint8)
        img[...] = (30, 160, 240, 255)                         # BGRA -> orange (240,160,30)
        img[:, w // 2:] = (60, 200, 40, 255)                   # green (40,200,60)
        enc, dec = H.AVEncoder(w, h, 30, 4000), H.H264Decoder()
        frame = dec.decode(enc.encode(img, True)[0])[0]
        for f, kind in ((frame, "IYUV"), (frame.reformat(format="nv12"), "NV12")):
            v.capture_next = True
            v.captured = None
            v.submit_video(f, {"enc_ms": 1, "decode_ms": 1, "bytes": 100, "decoder": "software"})
            wait(lambda: v.captured is not None, timeout=3, msg="drawing")
            ow, oh, data = v.captured
            out = np.frombuffer(data, np.uint8).reshape(oh, ow, 4)
            # the frame is centred in the window; colours in the middle of the left and right halves
            ys = oh // 2
            row = out[ys]
            lit = np.where(row[:, :3].sum(axis=1) > 30)[0]
            x0, x1 = lit[0], lit[-1]
            left = row[x0 + (x1 - x0) // 4, :3].astype(int)
            right = row[x0 + 3 * (x1 - x0) // 4, :3].astype(int)
            assert np.abs(left - [240, 160, 30]).max() <= 10, (kind, left)
            assert np.abs(right - [40, 200, 60]).max() <= 10, (kind, right)
        assert v._tex_key[0] == "yuv"
        # A one-dimensional frame: drawn with the RGBA fallback
        odd = H.AVEncoder(322, 194, 30, 2000)
        f2 = dec.decode(odd.encode(np.ascontiguousarray(np.zeros((194, 322, 4), np.uint8)), True)[0])
        v.submit_video(f2[0].reformat(width=321, height=193), {})
        wait(lambda: v._tex_key and v._tex_key[0] == "rgba", timeout=3, msg="RGBA fallback")
        # JPEG path: full frame + patches
        from PIL import Image
        v.submit(Image.new("RGB", (200, 100), (0, 0, 255)), {"bytes": 1})
        wait(lambda: not v.needs_full((200, 100)), timeout=3)
        v.submit_patches((200, 100), [(10, 10, Image.new("RGB", (20, 20), (255, 0, 0)))], {"bytes": 1})
        wait(lambda: v.patch_batches == 1, timeout=3)
        assert v.needs_full((300, 100))
        for _ in range(12):                      # the measurement window is 1 s: keep frames flowing
            v.submit(Image.new("RGB", (200, 100), (0, 0, 255)), {"bytes": 1})
            time.sleep(0.1)
        st = v.stats()
        assert st["renderer"] and st["fps"] > 0
    finally:
        v.close()
    assert closed == [1] and v.closed
    time.sleep(0.2)
    assert not v._thread.is_alive()
