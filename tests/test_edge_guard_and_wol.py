"""Edge bounce protection (EdgeGuard) and Wake-on-LAN."""
import json

from conftest import DictCfg

from mirrorway import protocol as P
from mirrorway import wol
from mirrorway.geometry import EdgeGuard, Rect
from mirrorway.mac.router import InputEvent
from mirrorway.mac.server import MirrorwayServer, wol_warnings
from mirrorway.win.client import ControlClient, Resolver
from mirrorway.win.netinfo import parse_netinfo
from mirrorway.win.remote import RemoteControl
from test_e2e import free_port, wait
from test_router_remote import FakeClock, FakeMac, FakeWin, mac_cfg, make_router

SCREEN = [Rect(0, 0, 1920, 1080)]


# ---- EdgeGuard -----------------------------------------------------------------
def test_edge_guard_disarmed_after_switch_until_time_or_distance():
    clock = FakeClock()
    g = EdgeGuard(clock)
    g.reset(armed=False)
    # Pushing against the edge: no switch while the guard is off, however hard you push
    for _ in range(10):
        assert g.check(SCREEN, 0, 500, -5, 0, "left", corner_guard=0) is None
    # It arms once the time is up; then a switch once the push threshold is passed
    clock.t += EdgeGuard.ARM_TIME + 0.01
    assert g.check(SCREEN, 0, 500, -1, 0, "left", corner_guard=0) is None      # 1 px < PUSH_PX
    assert g.check(SCREEN, 0, 500, -1, 0, "left", corner_guard=0) is None
    r = g.check(SCREEN, 0, 500, -1, 0, "left", corner_guard=0)
    assert r is not None and abs(r - 500 / 1079) < 1e-6


def test_edge_guard_arms_by_distance_and_resets_push():
    clock = FakeClock()
    g = EdgeGuard(clock)
    g.reset(armed=False)
    assert g.check(SCREEN, 10, 500, -1, 0, "left") is None
    assert not g.armed
    assert g.check(SCREEN, 30, 500, -1, 0, "left") is None                   # moved 24 px away
    assert g.armed
    assert g.check(SCREEN, 0, 500, -2, 0, "left", corner_guard=0) is None     # 2 px
    assert g.check(SCREEN, 200, 500, -2, 0, "left", corner_guard=0) is None   # left the edge -> reset
    assert g.push == 0.0
    assert g.check(SCREEN, 0, 500, -2, 0, "left", corner_guard=0) is None
    assert g.check(SCREEN, 0, 500, -2, 0, "left", corner_guard=0) is not None


def test_mac_router_no_immediate_bounce_back_to_windows():
    r, plat, sink, clock = make_router()
    r.enter_center()
    r.leave_from_client(0.5)
    sink.msgs.clear()
    # Right after returning, the finger keeps sliding at the right edge
    for i in range(20):
        clock.t += 0.02
        r.handle(InputEvent("move", x=1439, y=450, dx=3))
    assert not r.remote
    # After moving 24 pt away and coming back, switching is possible again
    r.handle(InputEvent("move", x=1400, y=450, dx=-39))
    r.handle(InputEvent("move", x=1439, y=450, dx=3))
    assert r.remote


def test_windows_no_immediate_bounce_back_to_mac():
    w, sent, clock = FakeWin(), [], FakeClock()
    rc = RemoteControl(w, lambda m, o: sent.append((m, o)), clock=clock)
    rc.on_enter({"from": "left", "ratio": 0.5, "exit": "left"})
    for _ in range(30):                      # sliding left for 0.6 s after entering
        clock.t += 0.02
        rc.on_move(-2, 0)
    assert rc.active and not sent
    clock.t += 0.5                           # ARM_TIME is up
    rc.on_move(-2, 0)
    rc.on_move(-2, 0)
    assert not rc.active and sent[-1][0] == P.MSG_LEAVE


# ---- Wake-on-LAN -----------------------------------------------------------------
def test_normalize_mac_and_magic_packet():
    assert wol.normalize_mac("AA-BB-CC-DD-EE-0F") == "aa:bb:cc:dd:ee:0f"
    assert wol.normalize_mac("0:e0:4c:68:1:2") == "00:e0:4c:68:01:02"          # macOS arp shorthand
    assert wol.normalize_mac("00e04c680102") == "00:e0:4c:68:01:02"
    assert wol.normalize_mac("00e0.4c68.0102") == "00:e0:4c:68:01:02"
    for bad in ("", "zz:bb:cc:dd:ee:ff", "00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "1:2:3"):
        assert wol.normalize_mac(bad) is None
    pkt = wol.magic_packet("00-E0-4C-68-01-02")
    assert len(pkt) == 102 and pkt[:6] == b"\xff" * 6
    assert pkt[6:] == bytes.fromhex("00e04c680102") * 16


def test_parse_arp():
    out = ("? (192.168.1.1) at 4c:ed:fb:1:2:3 on en0 ifscope [ethernet]\n"
           "? (192.168.1.107) at 0:e0:4c:68:1:2 on en0 ifscope [ethernet]\n"
           "? (192.168.1.120) at (incomplete) on en0 ifscope [ethernet]\n")
    assert wol.parse_arp(out, "192.168.1.107") == "00:e0:4c:68:01:02"
    assert wol.parse_arp(out, "192.168.1.120") is None
    assert wol.parse_arp(out, "192.168.1.5") is None
    assert wol.subnet_broadcast("192.168.1.107") == "192.168.1.255"
    assert wol.subnet_broadcast("10.1.2.3", 16) == "10.1.255.255"


def test_send_wol_uses_every_target_and_port():
    sent = []

    class S:
        def __init__(self, ifname):
            self.ifname = ifname

        def sendto(self, data, addr):
            sent.append((self.ifname, addr, data))

        def close(self):
            pass

    targets = [(None, "255.255.255.255"), ("en0", "255.255.255.255"), (None, "192.168.1.255")]
    n = wol.send_wol("00:e0:4c:68:01:02", repeat=2, targets=targets, socket_factory=S)
    assert n == len(sent) == 2 * 3 * 2
    assert {a for _, a, _ in sent} == {(t, p) for _, t in targets for p in (9, 7)}
    assert all(d == wol.magic_packet("00:e0:4c:68:01:02") for *_, d in sent)


def test_wol_targets_include_subnet_and_last_ip(monkeypatch):
    import mirrorway.discovery as D
    monkeypatch.setattr(D, "_broadcast_targets", lambda: [(None, "255.255.255.255"), ("en0", "255.255.255.255")])
    t = wol.wol_targets("192.168.1.107")
    assert (None, "192.168.1.255") in t and (None, "192.168.1.107") in t and ("en0", "255.255.255.255") in t
    assert len(t) == len(set(t))


ADAPTERS = [
    {"name": "Wi-Fi", "desc": "Intel(R) Wi-Fi 6 AX201", "media": "Native 802.11", "mac": "aa:aa:aa:aa:aa:01",
     "ips": ["192.168.1.150"]},
    {"name": "Ethernet", "desc": "Realtek PCIe GbE Family Controller", "media": "802.3",
     "mac": "00:e0:4c:68:01:02", "ips": ["192.168.1.107"], "wake": "Enabled", "armed": True},
]


def test_pick_adapter_and_wired():
    assert wol.pick_adapter(ADAPTERS, "192.168.1.150")["name"] == "Wi-Fi"     # the IP the connection came from
    assert wol.pick_adapter(ADAPTERS, "10.0.0.1")["name"] == "Ethernet"       # otherwise the wired one
    assert wol.pick_adapter([], "1.2.3.4") is None
    assert wol.is_wired(ADAPTERS[1]) and not wol.is_wired(ADAPTERS[0])


def test_parse_netinfo_variants():
    one = json.dumps({"adapters": {"name": "Ethernet", "desc": "Realtek", "media": "802.3",
                                   "mac": "00-E0-4C-68-01-02", "ips": "192.168.1.107", "prefix": 24,
                                   "wake": "Enabled", "adv": "Enabled", "armed": True},
                      "fast_startup": 1})
    info = parse_netinfo("﻿" + one)
    a = info["adapters"][0]
    assert a["mac"] == "00:e0:4c:68:01:02" and a["ips"] == ["192.168.1.107"] and a["prefix"] == 24
    assert a["armed"] is True and info["fast_startup"] is True
    # PowerShell 5.1 array wrapper and a record without a MAC
    wrapped = json.dumps({"adapters": {"value": [{"name": "X", "mac": ""},
                                                 {"name": "E", "mac": "001122334455", "ips": ["a", "b"]}],
                                       "Count": 2}, "fast_startup": 0})
    info = parse_netinfo(wrapped)
    assert [x["name"] for x in info["adapters"]] == ["E"] and info["adapters"][0]["ips"] == ["a", "b"]
    assert info["fast_startup"] is False
    assert parse_netinfo("") == {"adapters": [], "fast_startup": None}
    assert parse_netinfo('{"adapters":[],"fast_startup":null}')["fast_startup"] is None


def test_wol_warnings():
    good = {"mac": "x", "wired": True, "wake": "Enabled", "adv": "Enabled", "armed": True, "fast_startup": False}
    assert wol_warnings(good) == []
    bad = dict(good, wired=False, wake="Disabled", adv="Disabled", armed=False, fast_startup=True)
    assert len(wol_warnings(bad)) == 5
    assert wol_warnings({"mac": "x", "source": "arp"}) == []


def test_wake_session_resends_and_finishes():
    clock = FakeClock()
    sends = []
    ws = wol.WakeSession(lambda: sends.append(clock.t), clock=clock)
    ws.start()
    assert ws.state == "waking" and len(sends) == 1
    clock.t += 5
    assert ws.tick(False) == "waking" and len(sends) == 1
    clock.t += 5
    ws.tick(False)
    assert len(sends) == 2
    clock.t += 3
    assert ws.tick(True) == "woke" and abs(ws.elapsed - 13) < 1e-9
    ws2 = wol.WakeSession(lambda: None, clock=clock)
    ws2.start()
    clock.t += ws2.TIMEOUT
    assert ws2.tick(False) == "timeout"
    assert ws2.tick(True) == "timeout"        # finished, the state does not change


def _client(port, netinfo_fn):
    ccfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN", clipboard=False)
    return ControlClient(ccfg, Resolver(ccfg, None), RemoteControl(FakeWin(), lambda m, o: None),
                         netinfo_fn=netinfo_fn)


def test_server_learns_wol_target_from_windows(monkeypatch):
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=False, clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), beacon=False)
    srv.start()
    adapters = [dict(ADAPTERS[0]), dict(ADAPTERS[1], ips=["127.0.0.1"])]
    c = _client(port, lambda: {"adapters": adapters, "fast_startup": True})
    c.start()
    try:
        wait(lambda: srv.wol_target() is not None, msg="WoL target")
        t = srv.wol_target()
        assert t["mac"] == "00:e0:4c:68:01:02" and t["ip"] == "127.0.0.1" and t["wired"]
        assert t["armed"] and t["fast_startup"] is True and t["source"] == "windows"
        assert srv.status()["wol"]["mac"] == t["mac"]
        calls = []
        monkeypatch.setattr(wol, "send_wol", lambda mac, last_ip=None: calls.append((mac, last_ip)) or 6)
        assert srv.wake_windows() == 6 and calls == [("00:e0:4c:68:01:02", "127.0.0.1")]
    finally:
        c.stop()
        srv.stop()


def test_server_arp_fallback_when_windows_sends_nothing(monkeypatch):
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=False, clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), beacon=False)
    srv.ARP_DELAY = 0.2
    monkeypatch.setattr(wol, "arp_lookup", lambda ip: wol.normalize_mac("0:e0:4c:68:1:2"))
    srv.start()
    c = _client(port, lambda: None)
    c.start()
    try:
        wait(lambda: srv.wol_target() is not None, msg="WoL target from ARP")
        t = srv.wol_target()
        assert t["mac"] == "00:e0:4c:68:01:02" and t["source"] == "arp"
    finally:
        c.stop()
        srv.stop()
    assert MirrorwayServer(mac_cfg(port=1), FakeMac(), beacon=False).wake_windows() == 0   # no target
