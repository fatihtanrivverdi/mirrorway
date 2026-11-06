"""H.264 video stream (Mac: VideoToolbox / x264, Windows: decoding with PyAV)."""
import sys
import time
import types

import numpy as np
import pytest
from conftest import DictCfg

from mirrorway import h264 as H
from mirrorway import protocol as P
from mirrorway.mac.server import MirrorwayServer
from mirrorway.win.client import Resolver, VideoClient
from test_e2e import free_port, wait
from test_router_remote import FakeMac, mac_cfg
from test_changed_regions import ArrayCapture, bgra

pytestmark = pytest.mark.skipif(not (H.decoder_available() and H.encoder_available()),
                                reason="needs PyAV (h264 + libx264)")


def _rgb_of(arr_bgra):
    return arr_bgra[..., [2, 1, 0]].astype(int)


# ---- bitstream ------------------------------------------------------------------
def _x264_stream(n=3, w=320, h=192):
    enc = H.AVEncoder(w, h, 30, 2000)
    out = []
    for i in range(n):
        a = bgra(h, w, seed=i)
        out.append(enc.encode(a, force_key=(i == 0)))
    return out


def annexb_to_avcc_parts(data):
    """For tests: Annex B -> (SPS, PPS, other NALs with length prefixes)."""
    sps = pps = None
    body = bytearray()
    for nal in H.split_annexb(data):
        t = nal[0] & 0x1F
        if t == 7:
            sps = nal
        elif t == 8:
            pps = nal
        elif t in (9, 6):                     # AUD / SEI: not present in VideoToolbox output
            continue
        else:
            body += len(nal).to_bytes(4, "big") + nal
    return sps, pps, bytes(body)


def make_avcc_config(sps, pps):
    return (bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1]) + len(sps).to_bytes(2, "big") + sps
            + bytes([1]) + len(pps).to_bytes(2, "big") + pps)


def test_annexb_helpers_roundtrip():
    (data, key), (data2, key2), _ = _x264_stream()
    assert key and not key2
    types_ = H.nal_types(data)
    assert 7 in types_ and 8 in types_ and 5 in types_ and H.is_keyframe(data)
    assert not H.is_keyframe(data2)
    sps, pps, body = annexb_to_avcc_parts(data)
    s2, p2, nal_len = H.parse_avcc_config(make_avcc_config(sps, pps))
    assert s2 == [sps] and p2 == [pps] and nal_len == 4
    rebuilt = H.build_annexb([sps, pps]) + H.avcc_to_annexb(body)
    assert H.nal_types(rebuilt) == [7, 8] + [t for t in types_ if t not in (6, 7, 8, 9)]
    with pytest.raises(ValueError):
        H.avcc_to_annexb(b"\x00\x00\x00\x09abc")
    with pytest.raises(ValueError):
        H.parse_avcc_config(b"\x00\x01")


def test_encoder_zero_delay_and_decoder_colors():
    w, h = 320, 192
    enc, dec = H.AVEncoder(w, h, 30, 3000), H.H264Decoder()
    a = bgra(h, w)
    a[20:60, 20:120] = (30, 160, 240, 255)       # BGR -> orange
    for i in range(4):
        res = enc.encode(a, force_key=(i == 0))
        assert res is not None, "the encoder must not hold back the frame"
        frames = dec.decode(res[0])
        assert len(frames) == 1, "the decoder must not hold back the frame"
    rgba = dec.to_rgba(frames[0])
    assert rgba.shape == (h, w, 4) and rgba[0, 0, 3] == 255
    assert np.abs(rgba[40, 60, :3].astype(int) - [240, 160, 30]).max() <= 6
    small = dec.to_rgba(frames[0], (160, 96))
    assert small.shape == (96, 160, 4)
    # forced key frame
    res = enc.encode(a, force_key=True)
    assert res[1] and H.is_keyframe(res[0])


# ---- VideoToolbox encoder logic (with fake pyobjc modules) ---------------------
class FakeDict(dict):
    def objectForKey_(self, k):
        return self.get(k)


class FakeSample:
    def __init__(self, body, key, avcc):
        self.bb = body
        self.key = key
        self.fd = types.SimpleNamespace(avcc=avcc)


def install_fake_pyobjc(monkeypatch, fail_low_latency=False):
    """Fake modules that use x264 in place of VideoToolbox/CoreMedia/objc.
    The output is AVCC like real VideoToolbox, delivered only on CompleteFrames."""
    calls = {"props": {}, "registered": [], "complete": 0, "create_specs": []}
    vt = types.ModuleType("VideoToolbox")
    cm = types.ModuleType("CoreMedia")
    ob = types.ModuleType("objc")

    class Session:
        def __init__(self, w, h):
            self.enc = H.AVEncoder(w, h, 30, 3000)
            self.pending = []

    def create(alloc, w, h, codec, spec, src, calloc, cb, refcon, out):
        calls["create_specs"].append(dict(spec))
        assert codec == 0x61766331
        if fail_low_latency and "EnableLowLatencyRateControl" in spec:
            return (-12902, None)
        return (0, Session(w, h))

    def encode_handler(session, pb, pts, dur, props, flags_out, handler):
        force = bool(props and props.get("ForceKeyFrame"))
        data, key = session.enc.encode(pb, force_key=force)
        sps, pps, body = annexb_to_avcc_parts(data)
        avcc = make_avcc_config(sps, pps) if sps else session.__dict__.get("avcc")
        session.avcc = avcc
        session.pending.append((handler, FakeSample(body, key, avcc)))
        return (0, 0)

    def complete(session, pts):
        calls["complete"] += 1
        for handler, sb in session.pending:
            handler(0, 0, sb)
        session.pending.clear()
        return 0

    vt.VTCompressionSessionGetTypeID = lambda: 77
    vt.VTCompressionSessionCreate = create
    vt.VTSessionSetProperty = lambda s, k, v: calls["props"].__setitem__(k, v) or 0
    vt.VTCompressionSessionPrepareToEncodeFrames = lambda s: 0
    vt.VTSessionCopyProperty = lambda s, k, a, o: (0, True)
    vt.VTCompressionSessionEncodeFrameWithOutputHandler = encode_handler
    vt.VTCompressionSessionCompleteFrames = complete
    vt.VTCompressionSessionInvalidate = lambda s: None

    cm.kCMVideoCodecType_H264 = 0x61766331
    cm.CMTimeMake = lambda v, ts: (v, ts)
    cm.kCMTimeInvalid = (0, 0)
    cm.CMSampleBufferGetDataBuffer = lambda sb: sb.bb
    cm.CMBlockBufferGetDataLength = len
    cm.CMBlockBufferCopyDataBytes = lambda bb, off, n, out: (0, bytes(bb[off:off + n]))
    cm.CMSampleBufferGetSampleAttachmentsArray = lambda sb, create: [FakeDict({} if sb.key else {"NotSync": True})]
    cm.CMSampleBufferGetFormatDescription = lambda sb: sb.fd
    cm.CMFormatDescriptionGetExtension = lambda fd, k: FakeDict({"avcC": fd.avcc})
    ob.registerCFSignature = lambda *a: calls["registered"].append(a)

    monkeypatch.setitem(sys.modules, "VideoToolbox", vt)
    monkeypatch.setitem(sys.modules, "CoreMedia", cm)
    monkeypatch.setitem(sys.modules, "objc", ob)
    import mirrorway.mac.vtenc as V
    monkeypatch.setattr(V, "_REGISTERED", False)
    return calls


def test_vtencoder_produces_decodable_annexb(monkeypatch):
    calls = install_fake_pyobjc(monkeypatch)
    from mirrorway.mac.vtenc import VTEncoder
    w, h = 320, 192
    enc = VTEncoder(w, h, 60, 9000)
    assert calls["registered"] and calls["registered"][0][1] == b"^{OpaqueVTCompressionSession=}"
    assert calls["create_specs"][0]["EnableLowLatencyRateControl"] is True and enc.low_latency
    p = calls["props"]
    assert p["RealTime"] is True and p["AllowFrameReordering"] is False
    assert p["AverageBitRate"] == 9_000_000 and p["ExpectedFrameRate"] == 60
    assert p["YCbCrMatrix"] == "ITU_R_709_2" and p["ProfileLevel"] == "H264_ConstrainedHigh_AutoLevel"
    assert "hardware" in enc.name
    dec = H.H264Decoder()
    for i in range(3):
        data, key = enc.encode(bgra(h, w, seed=i), force_key=(i == 0))
        assert key == (i == 0)
        types_ = H.nal_types(data)
        assert (types_[:2] == [7, 8]) == (i == 0), types_     # SPS/PPS only on the key frame
        assert len(dec.decode(data)) == 1
    assert calls["complete"] == 3                              # completed right away for every frame
    enc.close()


def test_vtencoder_falls_back_without_low_latency(monkeypatch):
    calls = install_fake_pyobjc(monkeypatch, fail_low_latency=True)
    from mirrorway.mac.vtenc import VTEncoder
    enc = VTEncoder(320, 192, 30, 4000)
    assert not enc.low_latency and len(calls["create_specs"]) == 2
    assert "EnableLowLatencyRateControl" not in calls["create_specs"][1]
    assert enc.encode(bgra(192, 320), force_key=True)[1]


# ---- end to end ------------------------------------------------------------------------
class RawCapture(ArrayCapture):
    """A fake capture that can give raw frames, like ScreenCaptureKit (raw frame = numpy array)."""
    supports_raw = True

    def __init__(self, stop_after=None):
        super().__init__()
        self.raw_calls = 0
        self.stop_after = stop_after

    def grab(self, timeout=1.0, raw=False):
        if self.stop_after is not None and self.n >= self.stop_after:
            time.sleep(min(timeout, 0.05))
            return None                       # the screen is still
        if raw:
            self.raw_calls += 1
        return super().grab(timeout)


def _video_client(port, frames, patches=None, statuses=None, h264=True, monkeypatch=None):
    ccfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN")
    st = statuses if statuses is not None else []
    vc = VideoClient(ccfg, Resolver(ccfg, None), on_frame=lambda img, meta=None: frames.append((img, meta)),
                     on_status=st.append, target_size=lambda: (320, 200),
                     on_patches=(lambda size, p, meta: patches.append((size, p, meta))) if patches is not None else None,
                     needs_full=lambda size: not frames)
    if not h264 and monkeypatch is not None:
        monkeypatch.setattr(H, "decoder_available", lambda: False)
    return vc


def test_h264_stream_end_to_end_with_patches():
    cap = ArrayCapture()
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, quality="high",
                  clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: cap, beacon=False)
    srv.start()
    frames, patches = [], []
    vc = _video_client(port, frames, patches)
    vc.start()
    try:
        wait(lambda: frames and len(patches) >= 3, timeout=10, msg="H.264 frame + patches")
        img, meta = frames[0]
        assert img.mode == "RGBA" and img.size == (320, 200)
        assert meta["bytes"] > 0 and "decode_ms" in meta
        size, parts, _ = patches[-1]
        assert size == (320, 200) and all(p.mode == "RGBA" for _, _, p in parts)
        assert vc.info.get("codec") == "h264" and "x264" in vc.info.get("encoder", "")
        wait(lambda: srv.status()["video_fps"] > 0, timeout=5)
        st = srv.status()
        assert st["video_codec"] == "h264" and "x264" in st["video_encoder"]
        assert "H.264" in srv.stats_line()
        # Corrupt data: the decoder is reset, a key frame is requested, the stream recovers
        n_before, errs = len(frames) + len(patches), vc.decode_errors
        vc._handle_h264(vc.ch, P.pack_frame2(640, 400, 999, 1, b"\x00\x00\x00\x01\x65" + b"\xff" * 64, 0))
        assert vc.decode_errors == errs + 1     # the waiting flag may be cleared at once by an arriving key frame
        wait(lambda: not vc._h264_wait_key and len(frames) + len(patches) > n_before, timeout=5,
             msg="recovery after the key frame")
    finally:
        vc.stop()
        srv.stop()


def test_client_without_decoder_gets_jpeg(monkeypatch):
    cap = ArrayCapture()
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: cap, beacon=False)
    srv.start()
    frames = []
    vc = _video_client(port, frames, h264=False, monkeypatch=monkeypatch)
    vc.start()
    try:
        wait(lambda: frames, timeout=5)
        assert frames[0][0].mode == "RGB" and srv.status()["video_codec"] == "jpeg"
    finally:
        vc.stop()
        srv.stop()


def test_encoder_failure_falls_back_to_jpeg():
    cap = ArrayCapture()
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, clipboard=False)

    def broken(kind, w, h, fps, kbps):
        raise RuntimeError("no encoder")

    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: cap, beacon=False, h264_encoder_factory=broken)
    srv.start()
    frames = []
    vc = _video_client(port, frames)
    vc.start()
    try:
        wait(lambda: frames, timeout=5, msg="fallback to JPEG")
        assert srv.status()["video_codec"] == "jpeg" and vc.info.get("codec") == "jpeg"
    finally:
        vc.stop()
        srv.stop()


def test_hardware_path_and_fallback_to_software(monkeypatch):
    install_fake_pyobjc(monkeypatch)
    import mirrorway.mac.vtenc as V
    monkeypatch.setattr(V, "pixel_buffer_size", lambda pb: (pb.shape[1], pb.shape[0]))
    from mirrorway.mac import server as S

    # 1) Hardware path: raw frame -> (fake) VideoToolbox -> Windows decodes
    cap = RawCapture()
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: cap, beacon=False)
    srv.start()
    frames = []
    vc = _video_client(port, frames)
    vc.start()
    try:
        wait(lambda: len(frames) >= 1 and cap.raw_calls >= 3, timeout=8, msg="hardware path")
        assert "VideoToolbox" in srv.status()["video_encoder"] and not srv.vt_failed
    finally:
        vc.stop()
        srv.stop()

    # 2) If the hardware encoder cannot be opened, software takes over and hardware is not tried again
    cap2 = RawCapture()
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, clipboard=False)

    def factory(kind, w, h, fps, kbps):
        if kind == "raw":
            raise RuntimeError("VTCompressionSessionCreate failed")
        return S._default_h264_factory(kind, w, h, fps, kbps)

    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: cap2, beacon=False, h264_encoder_factory=factory)
    srv.start()
    frames = []
    vc = _video_client(port, frames)
    vc.start()
    try:
        wait(lambda: frames, timeout=8, msg="software fallback")
        assert srv.vt_blocked() and not srv.vt_failed and "x264" in srv.status()["video_encoder"]
    finally:
        vc.stop()
        srv.stop()


def test_idle_refresh_reencodes_last_frame():
    cap = RawCapture(stop_after=3)
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, clipboard=False)
    encoded = []

    class CountingEncoder:
        name = "counter"

        def __init__(self, w, h):
            self.width, self.height = w, h
            self.inner = H.AVEncoder(w, h, 30, 2000)

        def encode(self, img, force_key=False):
            encoded.append(time.monotonic())
            return self.inner.encode(img, force_key)

        def close(self):
            pass

    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: cap, beacon=False,
                      h264_encoder_factory=lambda kind, w, h, fps, kbps: CountingEncoder(w, h))
    srv.vt_failed = True                                   # array path
    srv.start()
    frames = []
    vc = _video_client(port, frames)
    vc.start()
    try:
        wait(lambda: len(encoded) >= 3 + 2, timeout=6, msg="sharpening after movement")
        time.sleep(0.5)
        assert len(encoded) == 5                           # 3 real frames + 2 refreshes, then it stops
        assert encoded[3] - encoded[2] >= 0.25
    finally:
        vc.stop()
        srv.stop()


def test_silent_hardware_encoder_falls_back(monkeypatch):
    """If the hardware encoder produces no output without reporting an error, the software encoder takes over."""
    import mirrorway.mac.vtenc as V
    monkeypatch.setattr(V, "pixel_buffer_size", lambda pb: (pb.shape[1], pb.shape[0]))
    from mirrorway.mac import server as S

    class Silent:
        name = "silent"

        def __init__(self, w, h):
            self.width, self.height = w, h

        def encode(self, img, force_key=False):
            return None

        def close(self):
            pass

    def factory(kind, w, h, fps, kbps):
        return Silent(w, h) if kind == "raw" else S._default_h264_factory(kind, w, h, fps, kbps)

    cap = RawCapture()
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0, quality="low",
                  clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: cap, beacon=False, h264_encoder_factory=factory)
    srv.start()
    frames = []
    vc = _video_client(port, frames)
    vc.start()
    try:
        wait(lambda: frames, timeout=10, msg="software after a silent encoder")
        assert srv.vt_blocked() and not srv.vt_failed and "x264" in srv.status()["video_encoder"]
    finally:
        vc.stop()
        srv.stop()
