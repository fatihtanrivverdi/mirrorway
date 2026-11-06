"""Smoke test of the Windows interface (Tk + tray + video window) with fake Windows APIs.
Run (Linux, Xvfb):  xvfb-run python tests/gui_smoke_windows.py [sdl|tk] [h264|jpeg]
"""
import os
import socket
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]
os.environ["MIRRORWAY_HOME"] = tempfile.mkdtemp(prefix="mirrorway-gui-")

import stubs  # noqa: E402

u = stubs.install_windows()

# --- fake pystray / pyperclip ---------------------------------------------
ps = types.ModuleType("pystray")


class MenuItem:
    def __init__(self, text, action, checked=None, radio=False, default=False, visible=True, enabled=True):
        self.text_src, self.action, self.checked, self.default, self.enabled = text, action, checked, default, enabled

    @property
    def text(self):
        return self.text_src(self) if callable(self.text_src) else self.text_src


class Menu:
    SEPARATOR = MenuItem("- - - -", None)

    def __init__(self, *items):
        self.items = items


class Icon:
    def __init__(self, name, icon, title, menu):
        self.name, self.icon, self.title, self.menu = name, icon, title, menu
        self.updates = 0

    def run_detached(self):
        pass

    def update_menu(self):
        self.updates += 1

    def stop(self):
        pass

    def click(self, label):
        for it in self.menu.items:
            if it is not Menu.SEPARATOR and it.text.startswith(label):
                return it.action(self, it)
        raise KeyError(label)


ps.MenuItem, ps.Menu, ps.Icon = MenuItem, Menu, Icon
sys.modules["pystray"] = ps
pc = types.ModuleType("pyperclip")
_clip = {"t": ""}
pc.copy = lambda t: _clip.__setitem__("t", t)
pc.paste = lambda: _clip["t"]
sys.modules["pyperclip"] = pc

from PIL import Image  # noqa: E402

from conftest import DictCfg  # noqa: E402
from mirrorway.config import WIN_DEFAULTS, Config  # noqa: E402
from mirrorway.mac.server import MirrorwayServer  # noqa: E402
from mirrorway.win.app import WinApp  # noqa: E402
from test_router_remote import FakeMac  # noqa: E402


class Cap:
    name = "fake"
    n = 0

    def grab(self, timeout=1.0):
        time.sleep(0.04)
        self.n += 1
        return Image.new("RGB", (2880, 1800), ((self.n * 30) % 256, 90, 200))

    def stop(self):
        pass


s = socket.socket()
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
s.close()
AUTH = sys.argv[3] if len(sys.argv) > 3 else "pair"          # pair: 6-digit code; legacy: the old long code
mcfg = DictCfg(port=port, pair_code="ABCD-EFGH-JKMN" if AUTH == "legacy" else "", side="right", speed=1.0, scroll_speed=1.0,
               swap_cmd_ctrl=True, left_option="alt", keyboard_layout="auto", edge_switch=True,
               screen_share=True, display_index=0, quality="low", clipboard=True)
from test_changed_regions import ArrayCapture  # noqa: E402

server = MirrorwayServer(mcfg, FakeMac(), capture_factory=lambda i, p: ArrayCapture(), beacon=False)
server.start()

VIEWER = sys.argv[1] if len(sys.argv) > 1 else "sdl"
CODEC = sys.argv[2] if len(sys.argv) > 2 else "h264"
mcfg.set("video_codec", CODEC)
cfg = Config("windows", WIN_DEFAULTS)
cfg.set("port", port)
cfg.set("viewer", VIEWER)
app = WinApp(cfg)
results = {}
errors = []


def check(name, cond):
    results[name] = bool(cond)
    if not cond:
        errors.append(name)


def when(cond, then, timeout_ms=15000, every_ms=100):
    """Move to the next step once the condition holds (or the time is up): fixed waits may not be enough on
    slow machines (CI)."""
    import time as _t
    deadline = _t.monotonic() + timeout_ms / 1000.0

    def poll():
        try:
            app._drain()
            ok = cond()
        except Exception:
            ok = False
        if ok or _t.monotonic() >= deadline:
            then()
        else:
            app.root.after(every_ms, poll)
    app.root.after(every_ms, poll)


def _viewer_ready():
    v = app.viewer
    if v is None:
        return False
    if hasattr(v, "submit_video"):       # let the stream switch to its real texture type after the first frames
        return v.frames_drawn >= 10 and v._tex_key is not None
    return bool(v._has_frame)


def step1():
    check("settings window opened while not paired", app.settings_win is not None)
    check("pairing help is shown", "6-digit" in app.settings_win.help_var.get())
    app.settings_win.host_var.set("127.0.0.1")
    if AUTH == "legacy":
        app.settings_win.code_var.set("abcd-efgh-jkmn")
    else:
        code = server.start_pairing()
        app.settings_win.code_var.set(code[:3] + " " + code[3:])
    app.settings_win.save()
    # a client connecting with the old code registers its key and reconnects with it: wait for that too
    when(lambda: app.control.state == "connected" and server.connected() and app.control.auth_mode == "key"
         and (AUTH != "legacy" or app.creds.paired()), step2)


def step2():
    app._drain()
    check("connected", app.control.state == "connected" and server.connected())
    if AUTH == "legacy":
        check("connected with the old code and registered the key", app.creds.paired() and server.devices())
    else:
        check("connected with the device key", app.control.auth_mode == "key" and len(server.devices()) == 1)
        check("paired Mac in the settings window", app.settings_win is not None
              and "Paired Mac" in app.settings_win.pair_var.get())
    check("tray menu updated", app.icon.updates > 0)
    check("status text", "Connected" in app.status_text)
    default = [it for it in app.icon.menu.items if getattr(it, "default", False)]
    check("left click opens the panel", len(default) == 1 and default[0].text == "Open Mirrorway")
    app.icon.click("Open Mirrorway")
    when(lambda: app.panel is not None and app.panel.is_shown(), step2b, timeout_ms=5000)


def step2b():
    p = app.panel
    check("panel opened", p is not None and p.is_shown())
    m = p.model
    check("panel: connected", m is not None and m.header.state == "connected" and "Connected to" in m.header.subtitle)
    check("panel: drawn", len(p.canvas.find_all()) > 20)
    clip = bool(cfg.get("clipboard"))
    p.click("tile:clipboard")
    check("panel: clipboard tile", bool(cfg.get("clipboard")) == (not clip) and p.model.tile("clipboard").on == (not clip))
    p.click("tile:clipboard")
    p.click("tile:files")
    check("panel: files tile", cfg.get("file_clipboard") is False and not p.model.tile("files").on)
    p.click("tile:files")
    check("panel: the Mac accepts actions", app.control.can_ask_mac() and p.model.tile("audio").enabled)
    before = bool(mcfg.get("audio"))
    p.click("tile:audio")
    when(lambda: bool(mcfg.get("audio")) != before, step2c, timeout_ms=5000)


def step2c():
    p = app.panel
    check("panel: audio tile changes the Mac's setting", bool(mcfg.get("audio")) is True)
    mcfg.set("audio", False)
    server.send_audio_state()
    p.hide()
    check("panel hides", not p.is_shown())
    p.click("primary")                         # Show the Mac's screen
    when(_viewer_ready, step3)


def step3():
    v = app.viewer
    check("viewer opened", v is not None)
    sdl = hasattr(v, "submit_video")
    check(f"viewer type ({VIEWER})", sdl == (VIEWER == "sdl"))
    if sdl:
        check("frame drawn (GPU)", v.frames_drawn > 0)
        kind = v._tex_key[0] if v._tex_key else None
        check("texture type", kind == ("yuv" if CODEC == "h264" else "rgb"))
        v.capture_next = True
    else:
        check("frame received", v._has_frame)
        if v._photo is not None:
            w, h = v._photo.width(), v._photo.height()
            tw, th = v.target_size()
            check("frame fits the window", w <= tw + 1 and h <= th + 1 and abs(w / h - 1.6) < 0.02)
    print("    viewer:", v.stats())
    u.sent.clear()
    server.router.enter_center()

    def ready4():
        v = app.viewer
        st = v.stats() if v else {}
        sdl = hasattr(v, "submit_video")
        cap_ok = (not sdl) or v.captured is not None
        title = v.title if sdl else v.top.title()        # the title refreshes once a second
        return (cap_ok and any(e[0] == "mouse" for e in u.sent) and st.get("fps", 0) > 0 and app.remote.active
                and "fps" in title)
    when(ready4, step4)


def step4():
    v = app.viewer
    sdl = hasattr(v, "submit_video")
    check("ENTER moved the pointer (SendInput)", any(e[0] == "mouse" for e in u.sent))
    st = v.stats() if v else {}
    check("viewer measurements (fps, MB/s)", st.get("fps", 0) > 0 and st.get("kbps", 0) > 0)
    if sdl:
        check("measurements in the title", "fps" in v.title)
        cap = v.captured
        ok = False
        if cap:
            import numpy as np
            w, h, data = cap
            img = np.frombuffer(data, np.uint8).reshape(h, w, 4)
            # fake screen: dark lines on a light grey background (235); black outside the frame
            center = img[h // 2 - 40:h // 2 + 40, w // 2 - 40:w // 2 + 40, :3].astype(int)
            ok = center.max() > 200 and img[..., 3].min() > 0
        check("GPU drawing looks right", ok)
        if CODEC == "jpeg":
            check("partial drawing used", v.patch_batches > 10)
    else:
        check("measurements in the title", "fps" in v.top.title())
        check("partial drawing used", v.patch_batches > 10)
        check("partial drawing fast (< 8 ms)", st.get("display_ms", 99) < 8)
    check("tray icon active", app.remote.active)
    # Measurement box: off in a window, on at the top right in full screen
    check("measurement box off in a window", not v.overlay_visible())
    if sdl:
        v.toggle_fullscreen()
    else:
        v.set_fullscreen(True)
    v4 = v
    when(lambda: v4.overlay_visible() and (not hasattr(v4, "submit_video") or v4.overlay_draws > 0), step4b,
         timeout_ms=10000)


def step4b():
    v = app.viewer
    sdl = hasattr(v, "submit_video")
    check("measurement box on in full screen", v.overlay_visible())
    if sdl:
        check("measurement box drawn", v.overlay_draws > 0)
        v.toggle_fullscreen()
    else:
        app.root.update_idletasks()
        check("measurement box visible (Tk)", v.overlay.winfo_ismapped() and "fps" in v.overlay.cget("text"))
        v.set_fullscreen(False)
    app.icon.click("Settings")
    when(lambda: app.settings_win is not None, step5, timeout_ms=5000)


def step5():
    app._drain()
    check("settings could be reopened", app.settings_win is not None)
    w = app.settings_win
    built = []
    for page in [pg.id for pg in w.pages]:
        w.show_page(page)
        built.append(bool(w._updaters))
    check("settings: every page builds", len(built) == 6 and all(built))
    w.show_page("clipboard")
    app.apply_setting("file_clipboard", False)
    check("settings: a switch applies", cfg.get("file_clipboard") is False)
    app.apply_setting("file_clipboard", True)
    w.show_page("connection")
    check("settings: connected status", "Connected to" in w.pages[0].groups[0].items[0].title)
    import pathlib
    import mirrorway.diag as D
    D.default_output_dir = lambda: pathlib.Path(os.environ["MIRRORWAY_HOME"])
    p = app.make_diag()
    check("diagnostics bundle created", p is not None and p.exists() and p.stat().st_size > 200)
    app.settings_win.close()
    check("the Mac can close the screen window", server.request_viewer(False))     # Screen tile / Control+Option+S
    when(lambda: app.viewer is None, step6, timeout_ms=8000)


def step6():
    check("screen window closed by the Mac", app.viewer is None)
    check("video connection closed", app.video is None)
    app.hotkey_action("screen")                # Control + Alt + S
    check("shortcut opens the screen window", app.viewer is not None and not app.viewer.closed)
    app.hotkey_action("screen")
    check("shortcut closes it again", app.viewer is None)
    import mirrorway.win.app as appmod
    launched = []
    appmod.subprocess.Popen = lambda cmd, **kw: launched.append((cmd, kw))
    app.root.after(300, lambda: (app.restart_for_update("9.9.9"),
                                 check("restart command after an update",
                                       launched and launched[0][0][1:3] == ["-m", "mirrorway.win"])))


app.root.after(800, step1)
app.root.after(90000, lambda: (errors.append("timed out"), app.quit()))
app.run()
server.stop()
for k, v in results.items():
    print(("[OK]  " if v else "[FAIL]") + " " + k)
if errors:                      # the end of the app log, for diagnosis
    import glob
    for lp in sorted(glob.glob(os.path.join(os.environ["MIRRORWAY_HOME"], "*.log"))):
        lines = open(lp, encoding="utf-8", errors="replace").read().splitlines()
        print(f"---- {os.path.basename(lp)} (last {min(40, len(lines))} lines)")
        for ln in lines[-40:]:
            print("    " + ln)
print("RESULT:", "PASSED" if not errors else f"FAILED {errors}")
sys.exit(1 if errors else 0)
