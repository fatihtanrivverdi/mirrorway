"""Working only on the home network, diagnostics bundle, dependencies pinned by hash."""
import json
import os
import re
import socket
import zipfile

from conftest import DictCfg

from mirrorway import diag
from mirrorway import netlock as N
from mirrorway.mac.server import MirrorwayServer
from mirrorway.win.client import ControlClient, Resolver
from mirrorway.win.remote import RemoteControl
from test_e2e import free_port, wait
from test_router_remote import FakeMac, FakeWin, mac_cfg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = [{"id": "gw:aa:bb:cc:dd:ee:01", "gateway": "192.168.1.1", "ifname": "en0"}]
CAFE = [{"id": "gw:12:34:56:78:9a:bc", "gateway": "10.0.0.1", "ifname": "en0"}]


# ---- network identity -------------------------------------------------------------------
def test_parse_route_and_current_networks():
    out = ("   route to: default\ndestination: default\n       mask: default\n    gateway: 192.168.1.1\n"
           "  interface: en0\n")
    assert N.parse_route_gateway(out) == "192.168.1.1"
    assert N.parse_route_gateway("gateway: utun4") is None
    nets = N.current_networks(
        interfaces_fn=lambda: [{"name": "en0", "kind": "lan"}, {"name": "utun3", "kind": "vpn"},
                               {"name": "en7", "kind": "lan"}],
        gateway_fn=lambda ifn: {"en0": "192.168.1.1", "en7": "192.168.2.1"}.get(ifn),
        arp_fn=lambda ip: {"192.168.1.1": "aa:bb:cc:dd:ee:01"}.get(ip))
    assert [(n["id"], n["gateway"], n["ifname"]) for n in nets] == [("gw:aa:bb:cc:dd:ee:01", "192.168.1.1", "en0")]
    # VPN skipped; en7, whose hardware address could not be read, is left out while a strongly recognised network exists


def test_network_lock_decisions():
    cfg = DictCfg()
    current = {"nets": HOME}
    lock = N.NetworkLock(cfg, probe=lambda: current["nets"])
    lock.refresh()
    assert lock.state == "trusted" and lock.allowed                 # the first network was saved as the home network
    assert [n["id"] for n in lock.trusted()] == [HOME[0]["id"]]
    current["nets"] = CAFE
    assert lock.refresh() is True and lock.state == "untrusted" and not lock.allowed
    assert "Unknown network" in lock.describe()
    current["nets"] = []                                            # no network: the decision stands
    assert lock.refresh() is False and not lock.allowed and lock.state == "unknown"
    current["nets"] = CAFE
    lock.refresh()
    lock.trust_current()
    assert lock.allowed and len(lock.trusted()) == 2
    cfg.set("network_lock", False)
    current["nets"] = [{"id": "gw:ff:ff:ff:00:00:01", "gateway": "172.16.0.1", "ifname": "en0"}]
    lock.refresh()
    assert lock.state == "off" and lock.allowed
    cfg.set("network_lock", True)
    lock.forget_all()                                               # reset: only this network
    assert lock.allowed and [n["gateway"] for n in lock.trusted()] == ["172.16.0.1"]


def test_server_closes_on_untrusted_network_and_reopens():
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=False, clipboard=False)
    current = {"nets": HOME}
    srv = MirrorwayServer(cfg, FakeMac(), beacon=False, network_probe=lambda: current["nets"])
    srv.start()
    wcfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN", clipboard=False)
    c = ControlClient(wcfg, Resolver(wcfg, None), RemoteControl(FakeWin(), lambda m, o: None), netinfo_fn=lambda: None)
    c.start()
    try:
        wait(lambda: srv.connected() and srv.status()["network"]["state"] == "trusted", timeout=8)
        current["nets"] = CAFE
        srv.apply_network(srv.netlock.refresh())
        assert not srv.status()["network"]["allowed"]
        wait(lambda: not srv.connected(), timeout=3, msg="disconnected")
        s = socket.socket()
        s.settimeout(1)
        assert s.connect_ex(("127.0.0.1", port)) != 0                # not listening
        s.close()
        srv.trust_current_network()                                  # "Trust This Network"
        wait(lambda: srv.connected(), timeout=10, msg="reconnected")
        assert len(srv.netlock.trusted()) == 2
    finally:
        c.stop()
        srv.stop()


def test_server_starts_closed_on_untrusted_network():
    port = free_port()
    cfg = mac_cfg(port=port, screen_share=False, clipboard=False,
                  trusted_networks=[{"id": HOME[0]["id"], "gateway": "192.168.1.1"}])
    srv = MirrorwayServer(cfg, FakeMac(), beacon=False, network_probe=lambda: CAFE)
    srv.start()
    try:
        s = socket.socket()
        s.settimeout(1)
        assert s.connect_ex(("127.0.0.1", port)) != 0
        s.close()
        assert srv.status()["network"]["state"] == "untrusted"
    finally:
        srv.stop()


# ---- diagnostics bundle ------------------------------------------------------------------
def test_diag_bundle_redacts_secrets(tmp_path):
    log = tmp_path / "mirrorway-mac.log"
    log.write_text("line 1\nline 2\n", encoding="utf-8")
    (tmp_path / "mirrorway-mac.log.1").write_text("old\n", encoding="utf-8")
    settings = {"pair_code": "ABCD-EFGH-JKMN", "device_key": "plain:c2lyIQ==", "quality": "high",
                "devices": [{"name": "PC", "pub": "AAAA"}], "nested": {"device_key": "dpapi:xyz"}}
    out = diag.make_bundle(tmp_path / "bundle.zip", diag.log_files(tmp_path, "mirrorway-mac"), settings,
                           status={"connected": True, "secret": "plain:abc"}, extra={"note": 1})
    z = zipfile.ZipFile(out)
    names = set(z.namelist())
    assert {"logs/mirrorway-mac.log", "logs/mirrorway-mac.log.1", "settings.json", "diagnostics.json", "README.txt"} <= names
    blob = b"".join(z.read(n) for n in names)
    assert b"ABCD-EFGH-JKMN" not in blob and b"c2lyIQ==" not in blob and b"dpapi:xyz" not in blob and b"plain:abc" not in blob
    s = json.loads(z.read("settings.json"))
    assert s["quality"] == "high" and s["devices"][0]["pub"] == "AAAA"
    info = json.loads(z.read("diagnostics.json"))
    assert info["versions"]["mirrorway"] and info["status"]["connected"] is True and info["note"] == 1
    assert diag.bundle_name("mac").startswith("Mirrorway-diagnostics-mac-") and diag.bundle_name("mac").endswith(".zip")


# ---- dependencies --------------------------------------------------------------
def _reqs(name):
    text = open(os.path.join(ROOT, name), encoding="utf-8").read()
    blocks = [b for b in re.split(r"\n(?=\S)", text) if b.strip() and not b.startswith("#")]
    return {re.match(r"([A-Za-z0-9_.-]+)==", b).group(1).lower(): b for b in blocks}


def test_requirements_are_pinned_with_hashes():
    for name, must in (("requirements-mac.txt", {"pyobjc-framework-videotoolbox", "av", "spake2", "rumps"}),
                       ("requirements-windows.txt", {"av", "spake2", "pysdl2", "pysdl2-dll", "pystray", "six"})):
        reqs = _reqs(name)
        assert must <= set(reqs), (name, must - set(reqs))
        for pkg, block in reqs.items():
            assert re.search(r"==\d", block), (name, pkg)
            assert re.search(r"--hash=sha256:[0-9a-f]{64}", block), (name, pkg)


def test_mac_menu_network_and_diag(tmp_path):
    from test_platform_glue import run_script
    out = run_script(f"""
        import stubs, os
        Q, AK, R, pb = stubs.install_mac()
        from mirrorway.config import Config, MAC_DEFAULTS
        cfg = Config('mac-test-210', MAC_DEFAULTS)
        from mirrorway.mac import app as A
        import mirrorway.diag as D
        D.default_output_dir = lambda: __import__('pathlib').Path({str(tmp_path)!r})
        opened = []
        A._reveal = lambda path: opened.append(['/usr/bin/open', '-R', str(path)])
        a = A.MirrorwayMacApp(cfg)
        nets = {{'n': [{{'id': 'gw:aa', 'gateway': '192.168.1.1', 'ifname': 'en0'}}]}}
        a.server.netlock.probe = lambda: nets['n']
        a.server.netlock.refresh()
        a._tick(None)
        assert a.m_net.title == 'Network: home network ✓', a.m_net.title
        nets['n'] = [{{'id': 'gw:bb', 'gateway': '10.0.0.1', 'ifname': 'en0'}}]
        a.server.netlock.refresh()
        a._tick(None)
        assert a.m_net.title.startswith('Network: unknown') and a.mi_status.title.startswith('⏸'), a.mi_status.title
        a._trust_network(None)
        a._tick(None)
        assert a.m_net.title == 'Network: home network ✓'
        p = a.make_diag()
        assert p is not None and os.path.exists(p) and opened and opened[0][:2] == ['/usr/bin/open', '-R']
        print('OK')
    """)
    assert "OK" in out


