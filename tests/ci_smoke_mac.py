"""CI (GitHub macOS runner): tries the platform-dependent parts of Mirrorway on real macOS.

The parts that need Screen Recording and Accessibility permission (screen capture, input capture) cannot be
tried on CI; this checks that the pinned packages install and that these work:
  - VideoToolbox H.264 encoding with pyobjc + decoding with PyAV
  - writing/reading/deleting a device key in the Keychain
  - network identity (gateway, network signature) — only reported; not required, as it depends on the CI network
  - importing the menu bar app modules
Run: python tests/ci_smoke_mac.py
"""
import os
import sys
import tempfile

if not os.environ.get("CI"):
    sys.exit("This script is for CI only (no CI environment variable). Do not run it on a real Mac.")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("MIRRORWAY_HOME", tempfile.mkdtemp(prefix="mirrorway-ci-"))

failures = []


def check(name, fn):
    try:
        info = fn()
        print(f"[OK]   {name}" + (f": {info}" if info else ""))
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")


def videotoolbox():
    import Quartz
    from mirrorway import h264 as H
    from mirrorway.mac.vtenc import VTEncoder, pixel_buffer_size
    w, h = 640, 400
    enc = VTEncoder(w, h, 30, 4000)
    dec = H.H264Decoder()
    got, keys = 0, 0
    try:
        for i in range(12):
            st, pb = Quartz.CVPixelBufferCreate(None, w, h, Quartz.kCVPixelFormatType_32BGRA, None, None)
            assert st == 0 and pb is not None, f"CVPixelBufferCreate {st}"
            assert pixel_buffer_size(pb) == (w, h)
            Quartz.CVPixelBufferLockBaseAddress(pb, 0)
            try:
                stride = Quartz.CVPixelBufferGetBytesPerRow(pb)
                buf = Quartz.CVPixelBufferGetBaseAddress(pb).as_buffer(stride * h)
                y0 = (i * 30) % (h - 40)
                for y in range(y0, y0 + 40):              # a moving white band
                    buf[y * stride: y * stride + w * 4] = b"\xff" * (w * 4)
            finally:
                Quartz.CVPixelBufferUnlockBaseAddress(pb, 0)
            out = enc.encode(pb, force_key=(i == 0))
            if out is None:
                continue
            data, key = out
            keys += int(key)
            for fr in dec.decode(data):
                assert (fr.width, fr.height) == (w, h), (fr.width, fr.height)
                got += 1
        enc.flush()
    finally:
        enc.close()
    assert keys >= 1 and got >= 6, f"key frames {keys}, decoded {got}"
    return f"{enc.name}, {got} frames decoded"


def keychain():
    from mirrorway import devkeys as D

    class Cfg(dict):
        def set(self, k, v, save=True):
            self[k] = v

    store = D.KeychainStore(service="app.mirrorway.ci-test", legacy_service=None)   # never touches the real item
    cfg = Cfg()
    key = "ci_test_key"
    secret = os.urandom(32)
    store.save(cfg, key, secret)
    try:
        assert store.load(cfg, key) == secret, "the key read back differs"
        ident = D.Identity.generate()
        D.save_identity(cfg, ident, store=store)
        again = D.load_identity(cfg, store=store, create=False)
        assert again is not None and again.public == ident.public
        assert all("plain:" not in str(v) for v in cfg.values()), "a plain key in the settings"
    finally:
        store.delete(cfg, key)
        store.delete(cfg, "device_key")
    return "write/read/delete"


def clipboard():
    """The real NSPasteboard: write text, HTML, RTF and an image, read them back (TIFF must be added too)."""
    import io
    from PIL import Image
    from mirrorway import richclip as RC
    from mirrorway.mac.clipboard import MacClipboard
    b = io.BytesIO()
    Image.new("RGBA", (32, 20), (200, 30, 60, 255)).save(b, "PNG")
    fmts = {RC.TEXT: "Mirrorway clipboard — ğüşiöç".encode(), RC.HTML: "<b>bold</b> text".encode(),
            RC.RTF: b"{\\rtf1\\ansi {\\b bold}}", RC.PNG: b.getvalue()}
    cb = MacClipboard()
    before = cb.change_count()
    cb.set_formats(fmts)
    assert cb.change_count() != before
    got = cb.get_formats()
    assert got.get(RC.TEXT) == fmts[RC.TEXT] and got.get(RC.RTF) == fmts[RC.RTF]
    assert got.get(RC.HTML) == fmts[RC.HTML], got.get(RC.HTML)
    assert Image.open(io.BytesIO(got[RC.PNG])).size == (32, 20)
    types = [str(t) for t in cb.pb.types()]
    assert "public.tiff" in types, types
    return ", ".join(sorted(got))


def tls_roots():
    """Starts with a context without root certificates, like python.org's macOS Python, loads the macOS roots and
    really connects to GitHub (a precondition for in-app updates)."""
    import ssl
    import urllib.request
    from mirrorway.mac import selfupdate as U
    orig = ssl.create_default_context
    ssl.create_default_context = lambda *a, **k: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        U._ssl_ctx = None
        ctx = U.ssl_context()
    finally:
        ssl.create_default_context = orig
        U._ssl_ctx = None
    n = ctx.cert_store_stats().get("x509_ca", 0)
    assert n > 0, "no root certificates loaded"
    import urllib.error
    req = urllib.request.Request("https://api.github.com/zen", headers={"User-Agent": "Mirrorway-CI"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            status = r.status
    except urllib.error.HTTPError as exc:
        status = exc.code       # an HTTP reply means TLS verification passed (e.g. 403: rate limit)
    assert 200 <= status < 600
    return f"{n} root certificates, api.github.com verified (HTTP {status})"


def network():
    from mirrorway import discovery, netlock
    ifs = discovery.interfaces()
    detail = []
    nets = netlock.current_networks(detail=detail)
    kinds = ", ".join(f"{d['name']}={d.get('ip')} ({d['kind']})" for d in ifs)
    found = ", ".join(f"{n['ifname']} {n['gateway']} [{n['source']}]" for n in nets) or "none"
    return f"interfaces: {kinds}; network: {found}" + (f"; detail: {'; '.join(detail)}" if detail else "")


def app_imports():
    import importlib
    for m in ['mirrorway.mac.app', 'mirrorway.mac.capture', 'mirrorway.mac.clipboard', 'mirrorway.mac.server', 'mirrorway.mac.tap',
              'mirrorway.mac.vdisplay', 'mirrorway.mac.extend_mgr', 'rumps']:
        importlib.import_module(m)
    return None


def virtual_display():
    """Second screen: open a virtual display with the helper process, see it in the macOS list, check it goes away."""
    import time

    import Quartz
    from mirrorway.mac.vdisplay import VirtualDisplay

    def ids():
        err, lst, n = Quartz.CGGetActiveDisplayList(32, None, None)
        return [int(i) for i in (lst or [])[:n]]
    before = ids()
    main = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    origin = (int(main.origin.x + main.size.width), int(main.origin.y))
    vd = VirtualDisplay(1280, 720, origin=origin)
    info = vd.start()
    try:
        did = vd.display_id
        assert did in ids(), f"not in the list: {did} / {ids()}"
        b = Quartz.CGDisplayBounds(did)
        got = (int(b.origin.x), int(b.origin.y), int(b.size.width), int(b.size.height))
        sck = "not tried"
        try:
            from mirrorway.mac.capture import open_best
            cap = open_best(0, 1280, 30, display_id=did)
            img = cap.grab(timeout=3)
            cap.stop()
            sck = f"captured {img.size if img is not None else None}"
        except Exception as exc:  # noqa: BLE001  (CI may have no Screen Recording permission)
            sck = f"not captured ({str(exc).splitlines()[0][:120]})"
    finally:
        vd.stop()
    deadline = time.time() + 5
    while did in ids() and time.time() < deadline:
        time.sleep(0.1)
    gone = did not in ids()
    return (f"id {did}, bounds {got} (requested position {origin}), note: {info.get('note')}, "
            f"{len(before)} displays before; on close {'removed' if gone else 'NOT REMOVED'}; ScreenCaptureKit: {sck}")


def file_clipboard():
    """File copy and paste: write file URLs to the real NSPasteboard (like Finder ⌘C), read them back."""
    import tempfile
    from mirrorway import richclip as RC
    from mirrorway.mac.clipboard import MacClipboard
    d = os.path.realpath(tempfile.mkdtemp(prefix="mirrorway-ci-files-"))
    paths = [os.path.join(d, "größe.csv"), os.path.join(d, "Dossier é")]
    open(paths[0], "wb").write(b"a,b\n")
    os.mkdir(paths[1])
    c = MacClipboard()
    c.set_formats({RC.FILES: "\n".join(paths).encode("utf-8")})
    got = c.get_formats().get(RC.FILES, b"").decode("utf-8").split("\n")
    norm = [unicodedata_nfc(os.path.realpath(p).rstrip("/")) for p in got]
    assert norm == [unicodedata_nfc(p) for p in paths], got
    return f"{len(got)} items"


def audio_parts():
    """Audio transfer: the ScreenCaptureKit audio output class (pyobjc protocol) is set up, the Mac's volume is read."""
    from mirrorway.mac import audio as MA
    cls = MA._audio_output_class()
    obj = cls.alloc().init()
    assert obj.respondsToSelector_(b"stream:didOutputSampleBuffer:ofType:")
    return f"class {cls.__name__}, speakers muted: {MA.output_muted()}"


def audio_capture():
    """Real ScreenCaptureKit audio capture: capture while afplay plays a tone; then capture again with the Mac's
    output muted (Mirrorway mutes the Mac while audio is on Windows: is the captured audio affected?). Only reported."""
    import math
    import struct
    import subprocess
    import time
    import wave
    from mirrorway import audio as A
    import logging
    from mirrorway.mac.audio import SCKAudioCapture, output_muted, set_output_muted
    alog = logging.getLogger("mirrorway.audio")
    alog.setLevel(logging.INFO)
    alog.addHandler(logging.StreamHandler(sys.stdout))             # show the "Audio format: …" line
    path = os.path.join(tempfile.mkdtemp(prefix="mirrorway-ci-audio-"), "tone.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / 48000)))
                               for i in range(48000 * 8)))
    phase = {"name": "on"}
    got = {"on": [], "muted": []}
    cap = SCKAudioCapture()
    cap.start(lambda pcm, rate, ch: got[phase["name"]].append(pcm))
    was_muted = output_muted()
    player = subprocess.Popen(["/usr/bin/afplay", path])
    try:
        time.sleep(2.5)
        phase["name"] = "muted"
        muted = set_output_muted(True)
        time.sleep(0.3)
        got["muted"].clear()
        time.sleep(2.0)
    finally:
        set_output_muted(bool(was_muted))
        player.kill()
        cap.stop()

    def desc(name):
        bufs = got[name][-60:]
        total = sum(len(p) for p in got[name])
        return (f"{name}: {len(got[name])} buffers, {total / A.bytes_per_ms():.0f} ms, "
                f"level {A.describe_level(A.level_dbfs(b''.join(bufs)) if bufs else None)}")
    return (f"format {cap.format}, read errors {cap.errors}; speakers {desc('on')}; "
            f"muted ({'yes' if muted else 'NO'}) {desc('muted')}")


def unicodedata_nfc(s):
    import unicodedata
    return unicodedata.normalize("NFC", s)


def gesture_events():
    """Trackpad gesture events: does the tap's conversion work without crashing while Windows is controlled (synthetic events)."""
    import Quartz
    from mirrorway import gestures as G
    from mirrorway.mac.tap import EventTap

    class R:
        remote = True

    tap = EventTap(R())
    out = []
    for etype, hid in ((G.NS_GESTURE, 23), (G.NS_MAGNIFY, 8), (G.NS_BEGIN_GESTURE, 0), (G.NS_ROTATE, 5)):
        ev = Quartz.CGEventCreate(None)
        Quartz.CGEventSetType(ev, etype)
        Quartz.CGEventSetIntegerValueField(ev, 110, hid)
        got = tap._convert(etype, ev)
        assert got is not None and got.kind == "gesture", (etype, got)
        # the HID subtype field is kept only on gesture (29) and magnify (30) events
        assert got.hid == hid or etype not in (G.NS_GESTURE, G.NS_MAGNIFY), (etype, got.hid)
        out.append(f"{etype}->{got.gtype}/{got.hid}/{got.fingers}")
    # are the Dock gesture fields (the ones Mac Mouse Fix writes) read back
    ev = Quartz.CGEventCreate(None)
    Quartz.CGEventSetType(ev, G.NS_MAGNIFY)
    Quartz.CGEventSetIntegerValueField(ev, 110, G.HID_DOCK_SWIPE)
    Quartz.CGEventSetIntegerValueField(ev, 123, 1)
    Quartz.CGEventSetDoubleValueField(ev, 124, -0.5)
    Quartz.CGEventSetIntegerValueField(ev, 132, 4)
    got = tap._convert(G.NS_MAGNIFY, ev)
    out.append(f"dock fields type={got.dock_type} progress={got.dock_progress} phase={got.dock_phase}")
    out.append("IOHIDEvent functions " + ("present" if tap._hid_dock is not None and tap._hid_dock.ok else "missing"))
    R.remote = False
    ev = Quartz.CGEventCreate(None)
    Quartz.CGEventSetType(ev, G.NS_GESTURE)
    assert tap._convert(G.NS_GESTURE, ev) is None         # untouched while the Mac is controlled
    return ", ".join(out)


def quick_panel():
    """The menu bar panel: build it, fill it from sample states, draw it in light and dark appearance and print the
    images (base64 PNG between markers) so they can be looked at from the CI log."""
    import base64
    import io

    from AppKit import NSApplication
    from PIL import Image
    from mirrorway import quick
    from mirrorway.mac.panel import H, W, QuickPanel

    NSApplication.sharedApplication()
    clicks = []
    panel = QuickPanel(clicks.append)
    st = {"connected": True, "remote": False, "client_name": "Home-PC", "rtt_ms": 3.0, "video": True,
          "video_fps": 58.0, "video_codec": "h264", "network": {"state": "trusted", "allowed": True},
          "extend": {"desired": False}, "extend_supported": True, "wol": {"mac": "aa"}, "devices": [{"id": "x"}],
          "audio": {"enabled": True, "active": True, "supported": True}, "pairing": None, "files": None}
    cfg = {"side": "right", "screen_share": True, "clipboard": True, "file_clipboard": True}
    sizes = []
    for name, dark, state in (("light", False, st), ("dark", True, dict(st, remote=True, video=False))):
        panel.update(quick.mac_panel(state, cfg, version="ci"))
        png = panel.render_png(dark=dark)
        img = Image.open(io.BytesIO(png))
        assert img.size[0] >= W and img.size[1] >= H, img.size
        sizes.append(f"{name} {img.size[0]}×{img.size[1]}")
        print(f"----- PANEL PNG {name} -----")
        print(base64.b64encode(png).decode("ascii"))
        print(f"----- END PANEL PNG {name} -----")
    panel._clicked(panel._keys.index("tile:audio"))
    assert clicks == ["tile:audio"], clicks
    panel.update(quick.mac_panel({"connected": False, "network": {}}, cfg, permission=False))
    return ", ".join(sizes)


check("VideoToolbox H.264 encoding + decoding", videotoolbox)
check("Quick panel (AppKit)", quick_panel)
check("Keychain", keychain)
check("Menu bar app modules", app_imports)
check("TLS: GitHub with the macOS root certificates", tls_roots)
check("Rich clipboard (NSPasteboard)", clipboard)
check("Trackpad gesture events", gesture_events)
check("File clipboard (NSPasteboard file URLs)", file_clipboard)
check("Audio transfer parts", audio_parts)
try:
    print(f"[INFO] Audio capture (ScreenCaptureKit): {audio_capture()}")
except Exception as exc:  # noqa: BLE001  (CI may have no Screen Recording permission; tried on a real Mac)
    print(f"[WARN] Could not try audio capture: {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
try:
    import subprocess
    from mirrorway.mac.notify import OSASCRIPT, script
    r = subprocess.run([OSASCRIPT, "-e", script("Mirrorway CI", "test notification", "files")], capture_output=True,
                       text=True, timeout=20)
    print(f"[INFO] Notification (osascript): exit {r.returncode} {r.stderr.strip()[:120]}")
except Exception as exc:  # noqa: BLE001
    print(f"[WARN] Could not try the notification: {exc}")
try:
    print(f"[INFO] Virtual display (second screen): {virtual_display()}")
except Exception as exc:  # noqa: BLE001  (the CI virtual machine may not support it; tried on a real Mac)
    print(f"[WARN] Could not try the virtual display: {type(exc).__name__}: {exc}")
try:
    print(f"[INFO] Network identity: {network()}")
except Exception as exc:  # noqa: BLE001
    print(f"[WARN] Could not try the network identity: {exc}")

print("RESULT: " + ("PASSED" if not failures else "FAILED: " + ", ".join(failures)))
sys.exit(1 if failures else 0)
