"""CI (GitHub Windows runner): tries the platform-dependent parts of Mirrorway on real Windows.

  - encrypting/decrypting the device key with DPAPI
  - PyAV H.264 encoding + decoding (D3D11VA is tried, otherwise it falls back to software)
  - window, renderer and YUV texture with SDL2 (pysdl2-dll)
  - network adapter info (PowerShell) — only reported
  - importing the Windows app modules
Run: python tests/ci_smoke_windows.py
"""
import os
import sys
import tempfile

if not os.environ.get("CI"):
    sys.exit("This script is for CI only (no CI environment variable).")

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


class Cfg(dict):
    def set(self, k, v, save=True):
        self[k] = v


def dpapi():
    from mirrorway import devkeys as D
    cfg, store = Cfg(), D.DPAPIStore()
    ident = D.Identity.generate()
    D.save_identity(cfg, ident, store=store)
    assert str(cfg.get("device_key", "")).startswith("dpapi:"), cfg
    again = D.load_identity(cfg, store=store, create=False)
    assert again is not None and again.public == ident.public
    return "encrypt/decrypt"


def h264():
    import numpy as np
    from PIL import Image
    from mirrorway import h264 as H
    w, h = 640, 400
    enc = H.AVEncoder(w, h, 30, 4000)
    dec = H.H264Decoder(hw="d3d11va")
    got = 0
    for i in range(12):
        a = np.zeros((h, w, 3), np.uint8)
        y0 = (i * 30) % (h - 40)
        a[y0:y0 + 40] = 255
        out = enc.encode(Image.fromarray(a), force_key=(i == 0))
        if out is None:
            continue
        data, _key = out
        try:
            frames = dec.decode(data)
        except Exception as exc:  # no hardware decoder: fall back to software like the app does
            dec.fail_hw(exc)
            frames = []
        for fr in frames:
            rgba = dec.to_rgba(fr, (w, h))
            assert rgba.shape[:2] == (h, w), rgba.shape
            got += 1
    enc.close()
    assert got >= 6, f"decoded frames {got}"
    return f"{got} frames decoded, decoder: {dec.kind}"


def sdl():
    import ctypes
    os.environ.setdefault("SDL_RENDER_DRIVER", "software")
    import sdl2
    assert sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) == 0, sdl2.SDL_GetError()
    try:
        win = sdl2.SDL_CreateWindow(b"mirrorway-ci", 0, 0, 320, 200, sdl2.SDL_WINDOW_HIDDEN)
        assert win, sdl2.SDL_GetError()
        ren = sdl2.SDL_CreateRenderer(win, -1, 0)
        assert ren, sdl2.SDL_GetError()
        info = sdl2.SDL_RendererInfo()
        sdl2.SDL_GetRendererInfo(ren, ctypes.byref(info))
        tex = sdl2.SDL_CreateTexture(ren, sdl2.SDL_PIXELFORMAT_IYUV, sdl2.SDL_TEXTUREACCESS_STREAMING, 320, 200)
        assert tex, sdl2.SDL_GetError()
        y = (ctypes.c_ubyte * (320 * 200))()
        uv = (ctypes.c_ubyte * (160 * 100))(*([128] * (160 * 100)))
        assert sdl2.SDL_UpdateYUVTexture(tex, None, y, 320, uv, 160, uv, 160) == 0, sdl2.SDL_GetError()
        sdl2.SDL_RenderCopy(ren, tex, None, None)
        sdl2.SDL_RenderPresent(ren)
        name = info.name.decode()
        sdl2.SDL_DestroyTexture(tex)
        sdl2.SDL_DestroyRenderer(ren)
        sdl2.SDL_DestroyWindow(win)
    finally:
        sdl2.SDL_Quit()
    ver = sdl2.SDL_version()
    sdl2.SDL_GetVersion(ctypes.byref(ver))
    return f"SDL {ver.major}.{ver.minor}.{ver.patch}, renderer {name}"


def clipboard():
    """The real Win32 clipboard: write text, HTML, RTF and an image, read them back."""
    import io
    from PIL import Image
    from mirrorway import richclip as RC
    from mirrorway.win.clipboard import WinRichClipboard
    b = io.BytesIO()
    Image.new("RGBA", (32, 20), (200, 30, 60, 255)).save(b, "PNG")
    fmts = {RC.TEXT: "Mirrorway clipboard — ğüşiöç".encode(), RC.HTML: "<b>bold</b> text".encode(),
            RC.RTF: b"{\\rtf1\\ansi {\\b kalin}}", RC.PNG: b.getvalue()}
    cb = WinRichClipboard()
    before = cb.change_count()
    cb.set_formats(fmts)
    assert cb.change_count() != before, "the sequence number did not change"
    got = cb.get_formats()
    assert got.get(RC.TEXT) == fmts[RC.TEXT], got.get(RC.TEXT)
    assert got.get(RC.HTML) == fmts[RC.HTML], got.get(RC.HTML)
    assert got.get(RC.RTF) == fmts[RC.RTF], got.get(RC.RTF)
    assert Image.open(io.BytesIO(got[RC.PNG])).size == (32, 20)
    return ", ".join(sorted(got))


def file_clipboard():
    """File copy and paste: write CF_HDROP (+ Preferred DropEffect) to the real Windows clipboard, read it back."""
    import tempfile
    from mirrorway import richclip as RC
    from mirrorway.win.clipboard import WinRichClipboard
    d = tempfile.mkdtemp(prefix="mirrorway-ci-files-")
    paths = [os.path.join(d, "größe.csv"), os.path.join(d, "Dossier é")]
    open(paths[0], "wb").write(b"a,b\n")
    os.mkdir(paths[1])
    cb = WinRichClipboard()
    cb.set_formats({RC.FILES: "\n".join(paths).encode("utf-8")})
    got = cb.get_formats().get(RC.FILES, b"").decode("utf-8").split("\n")
    assert [os.path.normcase(p) for p in got] == [os.path.normcase(p) for p in paths], got
    return f"{len(got)} items"


def sdl_extend():
    """Second screen: the video window goes full screen on the chosen monitor and returns to normal when closed."""
    import time
    from mirrorway.win import winapi
    from mirrorway.win.sdlview import SDLViewer

    def until(cond, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        return False
    mons = winapi.monitors()
    r = mons[-1]
    v = SDLViewer("mirrorway-ci-extend", on_close=lambda: None)
    try:
        v.set_extend([r.x, r.y, r.w, r.h])
        assert until(lambda: v.extend_rect is not None and v.fullscreen), "did not go full screen"
        geo = v._window_geometry()
        v.set_hidden(True)
        assert until(lambda: v.hidden), "did not minimise"
        v.set_hidden(False)
        assert until(lambda: not v.hidden and v.fullscreen), "did not come back"
        v.set_extend(None)
        assert until(lambda: v.extend_rect is None and not v.fullscreen), "did not return to normal"
    finally:
        v.close()
    return f"{len(mons)} monitors, window {geo}"


def sdl_audio():
    """Audio transfer: an SDL2 audio device opens, 16-bit PCM enters the queue and drains by playing.
    The CI machine may have no sound card: the default driver is tried first, then SDL's "dummy" driver."""
    import time
    import sdl2
    from mirrorway import audio as A
    from mirrorway.win.audio import SDLAudioPlayer
    notes = []
    try:
        pl = SDLAudioPlayer()
    except Exception as exc:  # noqa: BLE001
        notes.append(f"no default device ({str(exc)[:80]})")
        sdl2.SDL_SetHint(b"SDL_AUDIODRIVER", b"dummy")
        pl = SDLAudioPlayer()
    try:
        policy = A.QueuePolicy()
        pcm = b"\0" * int(20 * A.bytes_per_ms())
        acts = []
        for _ in range(5):
            act = policy.on_packet(pl.queued(), len(pcm))
            pl.queue(pcm)
            if act == "start":
                pl.play(True)
            acts.append(act)
        full = pl.queued()
        deadline = time.time() + 3
        while pl.queued() >= full and time.time() < deadline:
            time.sleep(0.02)
        drained = pl.queued() < full
        assert "start" in acts, acts
        assert drained, f"the queue did not drain ({full} bytes)"
        pl.clear()
        assert pl.queued() == 0
    finally:
        pl.close()
    return f"driver {pl.driver}, queue {full} bytes -> drained by playing" + (f"; {notes[0]}" if notes else "")


def app_imports():
    import importlib
    for m in ['mirrorway.win.app', 'mirrorway.win.client', 'mirrorway.win.remote', 'mirrorway.win.sdlview', 'mirrorway.win.updater', 'pystray']:
        importlib.import_module(m)
    return None


def hotkeys():
    """RegisterHotKey on a thread of its own: Control + Alt + S registers (or is reported as taken) and the loop
    stops cleanly."""
    from mirrorway.win.hotkeys import Hotkeys
    hk = Hotkeys({"screen": "s"}, lambda a: None)
    hk.start()
    assert hk.ready.wait(5), "the shortcut thread did not start"
    t = hk._thread
    hk.stop()
    t.join(5)
    assert not t.is_alive(), "the shortcut thread did not stop"
    return f"registered {hk.registered}, taken {hk.failed}"


def tray_panel():
    """The tray panel (Tk) in light and dark theme; printed as PNG (base64) for a look from the CI log."""
    import base64
    import io
    import time
    import tkinter as tk

    from PIL import ImageGrab
    from mirrorway import quick
    from mirrorway.win import panel as P
    from mirrorway.win import winapi
    winapi.set_dpi_aware()
    root = tk.Tk()
    root.withdraw()
    out = []
    try:
        for theme in ("light", "dark"):
            os.environ["MIRRORWAY_THEME"] = theme
            clicks = []
            p = P.TrayPanel(root, clicks.append)
            p.model = quick.win_panel(state="connected", status_text="", cfg=Cfg(clipboard=True),
                                      mac_name="MacBook-Pro", rtt_ms=3.0, audio_supported=True, audio_on=True)
            p.show()
            for _ in range(15):
                root.update()
                time.sleep(0.05)
            assert p.is_shown(), "the panel did not show"
            x, y = p.top.winfo_rootx(), p.top.winfo_rooty()
            img = ImageGrab.grab(bbox=(x, y, x + p.px(P.W), y + p.height))
            b = io.BytesIO()
            img.save(b, "PNG")
            print(f"----- WINPANEL PNG {theme} -----")
            print(base64.b64encode(b.getvalue()).decode("ascii"))
            print(f"----- END WINPANEL PNG {theme} -----")
            p.click("tile:files")
            assert clicks == ["tile:files"], clicks
            out.append(f"{theme} {img.size[0]}×{img.size[1]} (scale {p.scale:g}, icons {p.icon_family})")
            p.top.destroy()
    finally:
        os.environ.pop("MIRRORWAY_THEME", None)
        root.destroy()
    return ", ".join(out)


check("DPAPI", dpapi)
check("Shortcuts (RegisterHotKey)", hotkeys)
check("Tray panel (Tk)", tray_panel)
check("H.264 encoding + decoding", h264)
check("SDL2 window and YUV texture", sdl)
check("Rich clipboard (Win32)", clipboard)
check("File clipboard (CF_HDROP)", file_clipboard)
check("Second screen: video window full screen", sdl_extend)
check("Audio transfer: SDL2 audio queue", sdl_audio)
check("Windows app modules", app_imports)
try:
    from mirrorway.win import netinfo
    info = netinfo.collect(timeout=60)
    adapters = (info or {}).get("adapters") or []
    print(f"[INFO] Network adapters: {len(adapters)} — " +
          ", ".join(f"{a.get('name')} ({a.get('media') or a.get('kind') or '?'})" for a in adapters[:4]))
except Exception as exc:  # noqa: BLE001
    print(f"[WARN] Could not get network info: {exc}")

print("RESULT: " + ("PASSED" if not failures else "FAILED: " + ", ".join(failures)))
sys.exit(1 if failures else 0)
