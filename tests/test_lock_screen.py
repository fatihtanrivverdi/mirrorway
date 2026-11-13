"""Lock screen support (3.3): the Windows service keeps a SYSTEM helper running; the helper connects to the Mac on its
own (ROLE_SECURE) and types on the secure desktop (lock screen, Ctrl+Alt+Del, UAC); the Mac sends the keyboard and
mouse there while Windows reports a secure desktop, and ⌃⌥⌫ sends Ctrl+Alt+Del."""
import json
import queue
import threading
import time

import pytest
from conftest import DictCfg

from mirrorway import prefs as P
from mirrorway import protocol as PR
from mirrorway.win import lockscreen as LS
from mirrorway.win.securedesk import is_secure
from mirrorway.win.service import NO_SESSION, Supervisor


# ---- the service's supervisor ------------------------------------------------------------------------------------
class FakeChild:
    def __init__(self, session):
        self.session, self.pid, self.dead, self.killed = session, 1000 + session, False, False

    def alive(self):
        return not self.dead and not self.killed

    def terminate(self):
        self.killed = True


def test_supervisor_keeps_the_helper_in_the_console_session():
    session = {"id": NO_SESSION}
    started, sas = [], []

    def launch(sid, on_line):
        if sid == 7:
            raise OSError("CreateProcessAsUser failed (5)")
        started.append(FakeChild(sid))
        started[-1].on_line = on_line
        return started[-1]
    sup = Supervisor(launch, lambda: session["id"], lambda: sas.append(1))
    sup.step()
    assert started == []                                    # nobody at the console yet
    session["id"] = 1
    sup.step()
    assert [c.session for c in started] == [1] and sup.child is started[0]
    sup.step()
    assert len(started) == 1                                # still running: left alone
    started[0].dead = True                                  # it ended: started again
    sup.step()
    assert len(started) == 2
    session["id"] = 2                                       # another user at the console: moves there
    sup.step()
    assert started[1].killed and started[2].session == 2
    sup.on_line("SAS")
    sup.on_line("some output")
    assert sas == [1]
    session["id"] = 7
    assert sup.step() == 2.0 ** 1 and sup.child is None     # cannot start: waits longer and longer
    assert sup.step() == 4.0
    sup.stop()
    sup.run()                                               # returns at once when stopped


# ---- the helper ----------------------------------------------------------------------------------------------------
class Pipe:
    def __init__(self, inbox, outbox):
        self.inbox, self.outbox, self.closed = inbox, outbox, False

    def send(self, mtype, payload=b""):
        if self.closed:
            raise ConnectionError("closed")
        self.outbox.put((mtype, bytes(payload)))

    def send_json(self, mtype, obj):
        self.send(mtype, json.dumps(obj).encode())

    def recv(self):
        item = self.inbox.get(timeout=5)
        if item is None:
            raise ConnectionError("closed")
        return item

    def close(self):
        if not self.closed:
            self.closed = True
            self.inbox.put(None)


def _helper(desktop, sas, follows):
    from mirrorway.win.secure_helper import SecureHelper
    from test_router_remote import FakeWin
    w = FakeWin()
    h = SecureHelper({"identity": None, "mac_pub": b""}, backend=w, desktop_fn=lambda: desktop["name"],
                     follow_fn=lambda: follows.append(desktop["name"]), sas_fn=lambda: sas.append(1),
                     address_fn=lambda: None)
    h.POLL_S = 0.02
    return h, w


def test_helper_reports_the_desktop_and_types_only_what_the_mac_sends():
    desktop, sas, follows = {"name": "Default"}, [], []
    h, w = _helper(desktop, sas, follows)
    to_helper, from_helper = queue.Queue(), queue.Queue()
    ch = Pipe(to_helper, from_helper)
    def run():
        try:
            h.session(ch)
        except ConnectionError:
            pass                                                # the test closes the connection at the end
    t = threading.Thread(target=run)
    t.start()

    def next_state():
        while True:
            m, p = from_helper.get(timeout=5)
            if m == PR.MSG_SECURE_STATE:
                return json.loads(p)
    assert next_state() == {"desktop": "Default", "secure": False}
    desktop["name"] = "Winlogon"
    assert next_state() == {"desktop": "Winlogon", "secure": True}
    to_helper.put((PR.MSG_ENTER, json.dumps({"from": "center", "ratio": 0.5, "exit": "top"}).encode()))
    to_helper.put((PR.MSG_KEY, PR.pack_key(True, 0, 0x1E, 0)))
    to_helper.put((PR.MSG_KEY, PR.pack_key(False, 0, 0x1E, 0)))
    to_helper.put((PR.MSG_ECHO, PR.pack_echo(1.0, 2.0)))
    to_helper.put((PR.MSG_SAS, b""))
    while True:
        m, p = from_helper.get(timeout=5)
        if m == PR.MSG_ECHO_REPLY:
            break
    deadline = time.monotonic() + 5
    while not sas and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sas == [1] and h.remote.active
    assert ("key", True, 0, 0x1E, 0) in w.log and ("key", False, 0, 0x1E, 0) in w.log
    assert follows and follows[0] == "Winlogon"                 # attached to the input desktop before typing
    ch.close()
    t.join(5)
    assert not t.is_alive() and h.ch is None
    assert is_secure(None) and is_secure("Screen-saver") and not is_secure("Default")


# ---- the Mac -------------------------------------------------------------------------------------------------------
class FakeSession:
    def __init__(self, secure=False):
        self.secure, self.closed, self.sent = secure, False, []

    def send(self, m, p=b""):
        self.sent.append((m, p))

    def send_json(self, m, obj):
        self.sent.append((m, json.dumps(obj).encode()))

    def types(self):
        return [m for m, _ in self.sent]


def _mac(tmp_path, **cfg):
    from mirrorway.mac.server import MirrorwayServer
    from test_router_remote import FakeMac, mac_cfg
    srv = MirrorwayServer(mac_cfg(**cfg), FakeMac(), beacon=False, files_dir=tmp_path)
    app, helper = FakeSession(), FakeSession(secure=True)
    srv.control, srv.secure = app, helper
    srv.supports = lambda f: True
    return srv, app, helper


def test_mac_sends_input_to_the_helper_while_windows_shows_a_secure_desktop(tmp_path):
    srv, app, helper = _mac(tmp_path)
    assert srv.router.enter_center()
    assert app.types()[-1] == PR.MSG_ENTER
    srv.secure_state(helper, {"desktop": "Winlogon", "secure": True})
    assert srv.secure_desktop and app.types()[-1] == PR.MSG_EXIT     # the app lets go
    assert helper.types()[-1] == PR.MSG_ENTER                        # the helper takes over where the pointer is
    srv.send(PR.MSG_KEY, PR.pack_key(True, 0, 0x1E, 0))
    srv.send(PR.MSG_CLIPDATA, b"x")                                  # not input: still the app
    assert helper.types()[-1] == PR.MSG_KEY and app.types()[-1] == PR.MSG_CLIPDATA
    assert srv.send_sas() and helper.types()[-1] == PR.MSG_SAS
    srv.secure_state(app, {"desktop": "Default", "secure": False})   # only the helper reports this
    assert srv.secure_desktop
    srv.secure_state(helper, {"desktop": "Default", "secure": False})
    assert not srv.secure_desktop and helper.types()[-1] == PR.MSG_EXIT and app.types()[-1] == PR.MSG_ENTER
    # the helper drops out during a secure desktop: input goes back to the app
    srv.secure_state(helper, {"desktop": "Winlogon", "secure": True})
    srv._on_secure_closed(helper)
    assert srv.secure is None and not srv.secure_desktop and app.types()[-1] == PR.MSG_ENTER
    assert not srv.send_sas()


def test_windows_lock_keeps_control_with_lock_screen_support(tmp_path):
    srv, app, helper = _mac(tmp_path)
    srv.router.enter_center()
    srv.windows_lock_changed({"locked": True})
    assert srv.router.remote                                         # the helper can type the password
    srv.secure = None
    srv.windows_lock_changed({"locked": True})
    assert not srv.router.remote                                     # without it: back to the Mac (3.2.3)
    srv2, app2, helper2 = _mac(tmp_path, lock_sync=True)
    srv2.lock_mac = lambda: None
    srv2.router.enter_center()
    srv2.windows_lock_changed({"locked": True})
    assert not srv2.router.remote                                    # the Mac locks too: back to the Mac


def test_ctrl_alt_del_shortcut():
    from mirrorway.mac.router import InputEvent
    from test_router_remote import make_router
    r, plat, sink, clock = make_router()
    got = []
    r.on_action = got.append
    r.handle(InputEvent("key_down", keycode=51, flags=0x40000 | 0x80000))     # ⌃⌥⌫
    assert got == ["sas"]


def test_end_to_end_helper_connects_with_the_device_key(tmp_path):
    """A real Mac server, a Windows app paired with its device key, and the helper with the same key."""
    from mirrorway.geometry import Rect
    from mirrorway.mac.server import MirrorwayServer
    from mirrorway.win.client import ControlClient, Credentials, Resolver
    from mirrorway.win.remote import RemoteControl
    from mirrorway.win.secure_helper import SecureHelper
    from test_e2e import free_port, wait
    from test_router_remote import FakeMac, FakeWin, mac_cfg

    port = free_port()
    srv = MirrorwayServer(mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN"), FakeMac(), beacon=False,
                          files_dir=tmp_path / "mac")
    srv.start()
    wcfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN")
    app_win = FakeWin()
    c = ControlClient(wcfg, Resolver(wcfg, None), RemoteControl(app_win, lambda m, o: None),
                      netinfo_fn=lambda: None, monitors_fn=lambda: [Rect(0, 0, 1920, 1080)])
    c.start()
    h = None
    try:
        wait(lambda: srv.connected() and c.auth_mode == "key", timeout=20, msg="app paired with its device key")
        assert wcfg.get("last_mac_host") == "127.0.0.1"
        creds = Credentials(wcfg)
        trust = {"identity": creds.identity(), "mac_pub": creds.mac_pub(), "mac_name": "Mac", "config": "",
                 "port": port}
        desktop, sas, follows = {"name": "Default"}, [], []
        helper_win = FakeWin()
        h = SecureHelper(trust, backend=helper_win, desktop_fn=lambda: desktop["name"],
                         follow_fn=lambda: follows.append(1), sas_fn=lambda: sas.append(1),
                         address_fn=lambda: ("127.0.0.1", port))
        h.POLL_S = 0.05
        threading.Thread(target=h.run, daemon=True).start()
        wait(lambda: srv.secure_connected() and srv.secure_desktop_name == "Default", timeout=10,
             msg="helper connected")
        srv.router.enter_center()
        desktop["name"] = "Winlogon"
        wait(lambda: srv.secure_desktop and h.remote.active, timeout=5, msg="secure desktop")
        srv.send(PR.MSG_KEY, PR.pack_key(True, 0, 0x1C, 0x0D))
        wait(lambda: ("key", True, 0, 0x1C, 0x0D) in helper_win.log, timeout=5, msg="typed on the lock screen")
        assert ("key", True, 0, 0x1C, 0x0D) not in app_win.log
        srv.send_sas()
        wait(lambda: sas == [1], timeout=5, msg="Ctrl+Alt+Del")
        desktop["name"] = "Default"
        wait(lambda: not srv.secure_desktop and not h.remote.active, timeout=5, msg="back on the desktop")
        # a helper with a key the Mac does not know is refused
        from mirrorway.devkeys import Identity
        bad = SecureHelper(dict(trust, identity=Identity.generate()), backend=FakeWin(), desktop_fn=lambda: "Default",
                           follow_fn=lambda: None, sas_fn=lambda: None, address_fn=lambda: None)
        with pytest.raises(Exception):
            bad._connect(("127.0.0.1", port))
    finally:
        if h is not None:
            h.stop()
        c.stop()
        srv.stop()


# ---- installation pieces (platform independent) ----------------------------------------------------------------------
def test_trust_file_and_mac_address(tmp_path, monkeypatch):
    from mirrorway.devkeys import Identity
    ident = Identity.generate()
    cfg = tmp_path / "win.json"
    cfg.write_text(json.dumps({"host": "", "last_mac_host": "192.168.1.117"}), "utf-8")
    t = LS.make_trust(ident.private_bytes(), b"m" * 32, "MacBook", str(cfg), 24850, plain=True)
    p = tmp_path / "trust.json"
    p.write_text(json.dumps(t), "utf-8")
    got = LS.read_trust(p)
    assert got["identity"].public == ident.public and got["mac_pub"] == b"m" * 32 and got["mac_name"] == "MacBook"
    assert LS.mac_address(got) == ("192.168.1.117", 24850)
    cfg.write_text(json.dumps({"host": "mac.local:25000"}), "utf-8")
    assert LS.mac_address(got) == ("mac.local", 25000)               # set by hand wins
    cfg.write_text("{}", "utf-8")
    assert LS.mac_address(got) is None
    p.write_text(json.dumps(dict(t, version=99)), "utf-8")
    with pytest.raises(ValueError):
        LS.read_trust(p)
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    assert str(LS.data_dir()).replace("\\", "/").endswith("Mirrorway/ServiceData")
    from mirrorway.win.svcinstall import service_command
    from pathlib import PureWindowsPath
    assert service_command(PureWindowsPath(r"C:\Program Files\Mirrorway\Service")) == \
        r'"C:\Program Files\Mirrorway\Service\python\python.exe" -I "C:\Program Files\Mirrorway\Service\mirrorway_service.py"'


def test_windows_settings_lock_screen_group():
    def group(ctx):
        page = {p.id: p for p in P.win_pages(DictCfg(), dict(ctx, paired=True))}["general"]
        return page.groups[1]
    g = group({})
    assert g.title == "Lock screen" and "Off" in g.items[0].detail
    assert [i.key for i in g.items] == ["lockscreen_state", "action:lockscreen_install"] and g.items[1].enabled
    g = group({"lockscreen": {"installed": True, "running": True, "version": "3.3.0"}})
    assert "On:" in g.items[0].detail and [i.key for i in g.items][-1] == "action:lockscreen_remove"
    g = group({"lockscreen": {"installed": True, "running": True, "version": "3.3.0", "outdated": True}})
    assert "update" in g.items[0].detail and g.items[1].title == "Update lock screen support"
    g = group({"lockscreen": {"busy": True}})
    assert "Installing" in g.items[0].detail and not g.items[1].enabled
    g = group({"lockscreen": {"error": "Windows is not paired with a Mac yet: pair it first"}})
    assert "Could not install" in g.items[0].detail


def test_sign_in_screen_after_a_restart_works_without_the_app(tmp_path):
    """After Windows restarts nobody is logged on yet: only the helper is connected; the Mac can still switch over
    and type the password on the sign-in screen."""
    srv, app, helper = _mac(tmp_path)
    srv.control = None
    assert not srv.input_ready() and not srv.router.enter_center()
    srv.secure_state(helper, {"desktop": "Winlogon", "secure": True})
    assert srv.input_ready() and srv.router.enter_center()
    assert helper.types()[-1] == PR.MSG_ENTER
    srv.send(PR.MSG_KEY, PR.pack_key(True, 0, 0x1E, 0))
    assert helper.types()[-1] == PR.MSG_KEY
