"""Lock both computers together (3.2.3): the Mac's lock locks Windows and the other way round, without echoing a lock
back; a locked computer gives the keyboard and mouse back to the Mac."""
import time

from conftest import DictCfg

from mirrorway import protocol as P
from mirrorway.win import session as S


class Sent(list):
    def types(self):
        return [m for m, _ in self]


def _server(tmp_path, **cfg):
    from mirrorway.mac.server import MirrorwayServer
    from test_router_remote import FakeMac, mac_cfg
    srv = MirrorwayServer(mac_cfg(**cfg), FakeMac(), beacon=False, files_dir=tmp_path)
    sent = Sent()
    features = {P.LOCK_FEATURE}
    srv.connected = lambda: True
    srv.supports = lambda f: f in features
    srv.send_json = lambda mtype, obj: sent.append((mtype, obj))
    srv.send = lambda mtype, payload=b"": sent.append((mtype, payload))
    locks = []
    srv.lock_mac = lambda: locks.append(time.monotonic())
    return srv, sent, locks, features


def test_mac_lock_locks_windows_only_with_the_setting(tmp_path):
    srv, sent, locks, features = _server(tmp_path)
    assert srv.lock_sync() is False                                   # off by default
    srv.router.enter_center()
    assert srv.router.remote
    srv.mac_lock_changed(True)
    assert not srv.router.remote                                      # the keyboard and mouse come back anyway
    assert P.MSG_LOCK not in sent.types()
    srv.cfg.set("lock_sync", True)
    srv.mac_lock_changed(False)                                       # unlocking does nothing
    assert P.MSG_LOCK not in sent.types()
    srv.mac_lock_changed(True)
    assert sent.types().count(P.MSG_LOCK) == 1
    # Windows reports the lock the Mac asked for: not echoed back to the Mac
    srv.windows_lock_changed({"locked": True})
    assert locks == []
    # an older Windows: nothing is sent
    features.clear()
    srv._asked_windows_lock = -1e9
    srv.mac_lock_changed(True)
    assert sent.types().count(P.MSG_LOCK) == 1


def test_windows_lock_locks_the_mac(tmp_path):
    srv, sent, locks, _ = _server(tmp_path, lock_sync=True)
    srv.router.enter_center()
    srv.windows_lock_changed({"locked": True})
    assert not srv.router.remote and P.MSG_EXIT in sent.types()      # control back to the Mac
    assert len(locks) == 1
    # the Mac's own lock that follows is not sent back to Windows
    srv.mac_lock_changed(True)
    assert P.MSG_LOCK not in sent.types()
    srv.windows_lock_changed({"locked": False})
    srv.windows_lock_changed("nonsense")
    assert len(locks) == 1
    # setting off: Windows' lock still returns control, but the Mac stays unlocked
    srv.cfg.set("lock_sync", False)
    srv._locked_for_windows = -1e9
    srv.router.enter_center()
    srv.windows_lock_changed({"locked": True})
    assert not srv.router.remote and len(locks) == 1


def test_windows_session_events():
    assert S.event_of(S.WTS_SESSION_LOCK) is True and S.event_of(S.WTS_SESSION_UNLOCK) is False
    assert S.event_of(1) is None                                      # console connect and others are ignored
    seen = []
    w = S.SessionWatcher(seen.append)
    w._changed(True)
    w._changed(True)                                                  # repeated: reported once
    w._changed(False)
    assert seen == [True, False] and w.locked is False


def test_end_to_end_both_ways(tmp_path):
    from mirrorway.geometry import Rect
    from mirrorway.mac.server import MirrorwayServer
    from mirrorway.win.client import ControlClient, Resolver
    from mirrorway.win.remote import RemoteControl
    from test_e2e import free_port, wait
    from test_router_remote import FakeMac, FakeWin, mac_cfg

    port = free_port()
    srv = MirrorwayServer(mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", lock_sync=True), FakeMac(), beacon=False,
                          files_dir=tmp_path / "mac")
    mac_locks = []
    srv.lock_mac = lambda: mac_locks.append(1)
    srv.start()
    win_locks = []
    wcfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN")
    c = ControlClient(wcfg, Resolver(wcfg, None), RemoteControl(FakeWin(), lambda m, o: None),
                      netinfo_fn=lambda: None, monitors_fn=lambda: [Rect(0, 0, 1920, 1080)],
                      lock_fn=lambda: win_locks.append(1))
    c.start()
    try:
        wait(lambda: srv.connected() and c.auth_mode == "key" and srv.supports(P.LOCK_FEATURE)
             and P.LOCK_FEATURE in c.server_features(), timeout=20, msg="connection")
        srv.mac_lock_changed(True)                                    # the Mac locks -> Windows locks
        wait(lambda: win_locks == [1], timeout=5, msg="Windows locked")
        assert c.report_lock(True)                                    # Windows reports it: no lock back
        time.sleep(0.5)
        assert mac_locks == []
        srv._asked_windows_lock = -1e9                                # later, Windows locks by itself
        assert c.report_lock(True)
        wait(lambda: mac_locks == [1], timeout=5, msg="Mac locked")
    finally:
        c.stop()
        srv.stop()
    assert not c.report_lock(True)                                    # not connected: nothing to tell
