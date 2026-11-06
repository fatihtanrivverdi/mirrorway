"""End-to-end test over real TCP + encryption (the Mac/Windows APIs are fakes)."""
import json
import socket
import threading
import time

from conftest import DictCfg
from PIL import Image

from mirrorway.clipsync import ClipboardSync
from mirrorway.discovery import Listener
from mirrorway.config import DISCOVERY_PORT
from mirrorway.mac.router import InputEvent
from mirrorway.mac.server import MirrorwayServer
from mirrorway.win.client import ControlClient, Resolver, VideoClient
from mirrorway.win.remote import RemoteControl
from test_router_remote import FakeMac, FakeWin, mac_cfg


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(cond, timeout=5.0, msg="", state=None):
    """Wait until cond is true; otherwise fail (with the current state, if state is given)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return
        time.sleep(0.02)
    extra = ""
    if state is not None:
        try:
            extra = f" [{state()}]"
        except Exception as exc:  # noqa: BLE001
            extra = f" [could not get the state: {exc}]"
    raise AssertionError("timed out: " + msg + extra)


class FakeClip:
    def __init__(self):
        self.count = 0
        self.text = None
        self.lock = threading.Lock()

    def change_count(self):
        return self.count

    def get_text(self):
        return self.text

    def set_text(self, t):
        with self.lock:
            self.text = t
            self.count += 1

    user_copies = set_text


class FakeCapture:
    name = "fake"

    def __init__(self):
        self.n = 0

    def grab(self, timeout=1.0):
        time.sleep(0.03)
        self.n += 1
        return Image.new("RGB", (2880, 1800), ((self.n * 40) % 256, 80, 160))

    def stop(self):
        pass


def test_full_flow():
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0,
                  quality="low", clipboard=True)
    plat, mac_clip = FakeMac(), FakeClip()
    server = MirrorwayServer(cfg, plat, clipboard_backend=mac_clip,
                         capture_factory=lambda idx, preset: FakeCapture(), beacon=False)
    server.start()

    wcfg = DictCfg(host="127.0.0.1", port=port, pair_code="abcd efgh jkmn", clipboard=True)
    win, win_clip = FakeWin(), FakeClip()
    resolver = Resolver(wcfg, None)
    states = []
    holder = {}
    remote = RemoteControl(win, lambda m, o: holder["c"].send_json(m, o))
    control = ControlClient(
        wcfg, resolver, remote,
        clipsync_factory=lambda send: ClipboardSync(win_clip, send, lambda: True, interval=0.05),
        on_status=lambda s, t: states.append(s))
    holder["c"] = control
    control.start()
    try:
        wait(server.connected, msg="connection")
        # a client connecting with the old code registers its key and reconnects with it; start the stream after that
        wait(lambda: server.connected() and control.state == "connected" and control.auth_mode == "key",
             timeout=20, msg="connection with the key",
             state=lambda: (f"mac connected={server.connected()} win={control.state}/{control.auth_mode} "
                            f"network={server.netlock.state}/{server.netlock.allowed} error={server.last_error!r} "
                            f"states={states[-4:]}"))
        st = server.status()
        assert st["connected"] and st["client_ip"] == "127.0.0.1"

        # pointer at the right edge on the Mac -> switch to Windows
        server.router.handle(InputEvent("move", x=1439, y=450, dx=5))
        wait(lambda: remote.active, msg="ENTER")
        assert win.pos[0] == 3
        for i in range(10):
            server.router.handle(InputEvent("move", x=721 + i, y=450))
        wait(lambda: win.pos[0] == 13, msg="movement")
        server.router.handle(InputEvent("key_down", keycode=0))
        server.router.handle(InputEvent("key_up", keycode=0))
        wait(lambda: ("key", False, 0, 0x1E, 0) in win.log, msg="key")

        # pushing against the left edge on Windows returns to the Mac
        time.sleep(0.9)  # switch guard (returning without moving away from the edge is allowed after 0.8 s)
        server.router.handle(InputEvent("move", x=630, y=450))   # 100 px sola
        wait(lambda: win.pos[0] == 0, msg="left edge")
        server.router.handle(InputEvent("move", x=625, y=450))   # keep pushing against the edge
        wait(lambda: not server.router.remote, msg="LEAVE")
        assert not remote.active and not plat.hidden

        # Clipboard: Mac -> Windows -> Mac
        mac_clip.user_copies("Hello Windows — ğüşiöç")
        wait(lambda: win_clip.text == "Hello Windows — ğüşiöç", msg="clipboard mac->win")
        win_clip.user_copies("Selam Mac")
        wait(lambda: mac_clip.text == "Selam Mac", msg="clipboard win->mac")

        # Screen picture
        frames, vstates = [], []
        vc = VideoClient(wcfg, resolver, on_frame=lambda img, meta=None: frames.append(img),
                         on_status=vstates.append, target_size=lambda: (800, 600))
        vc.start()
        wait(lambda: len(frames) >= 3, msg="frames")
        assert frames[-1].size == (800, 500)     # fitted, keeping the aspect ratio
        assert vc.info.get("backend") == "fake"
        wait(lambda: server.status()["video"], msg="video status")
        vc.stop()

        # With screen sharing off, video is refused
        cfg.set("screen_share", False)
        vstates2 = []
        vc2 = VideoClient(wcfg, resolver, on_frame=lambda i: None, on_status=vstates2.append,
                          target_size=lambda: (800, 600))
        vc2.start()
        wait(lambda: any("turned off" in s for s in vstates2), msg="sharing-off message")
        vc2.stop()

        # A client connecting with a wrong code is refused
        bad_cfg = DictCfg(host="127.0.0.1", port=port, pair_code="ZZZZ-ZZZZ-ZZZZ", clipboard=False)
        bad = ControlClient(bad_cfg, Resolver(bad_cfg, None), RemoteControl(FakeWin(), lambda m, o: None))
        bad.start()
        wait(lambda: bad.state == "auth", msg="wrong code")
        bad.stop()
        assert server.connected()      # the real connection is unaffected

        # If the connection drops while in remote mode, control returns to the Mac
        server.router.enter_center()
        wait(lambda: remote.active, msg="remote mode")
        control.stop()
        wait(lambda: not server.connected(), msg="disconnect")
        # right after the connection is marked closed, the router returns to the Mac (a separate step)
        wait(lambda: not server.router.remote and not plat.hidden, msg="back to the Mac")
    finally:
        control.stop()
        server.stop()


def test_discovery_listener_parses_beacon():
    lst = Listener()
    lst.start()
    time.sleep(0.2)
    if lst.error:
        return  # the environment does not allow a UDP bind
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    msg = json.dumps({"app": "mirrorway", "v": 1, "name": "Office-MacBook", "port": 24850}).encode()
    for _ in range(5):
        s.sendto(msg, ("127.0.0.1", DISCOVERY_PORT))
        time.sleep(0.1)
        if lst.best():
            break
    assert lst.best() == ("127.0.0.1", "Office-MacBook", 24850)
    lst.stop()


def test_resolver_order():
    class Disc:
        def __init__(self, best):
            self._b = best

        def best(self):
            return self._b

    cfg = DictCfg(host="", port=24850, fallback_host="192.168.1.50")
    assert Resolver(cfg, Disc(None)).resolve() == ("192.168.1.50", 24850, "192.168.1.50")
    assert Resolver(cfg, Disc(("10.0.0.5", "Mac", 24850))).resolve() == ("10.0.0.5", 24850, "Mac")
    cfg.set("host", "10.0.0.9:25000")
    assert Resolver(cfg, Disc(("10.0.0.5", "Mac", 24850))).resolve() == ("10.0.0.9", 25000, "10.0.0.9")
    assert Resolver(DictCfg(host="", port=1), Disc(None)).resolve() is None


def test_config_reads_bom_json(tmp_path, monkeypatch):
    import mirrorway.config as C
    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path)
    (tmp_path / "windows.json").write_bytes(b'\xef\xbb\xbf{"pair_code": "ABCD-EFGH-JKMN"}')
    assert C.Config("windows", C.WIN_DEFAULTS).get("pair_code") == "ABCD-EFGH-JKMN"
