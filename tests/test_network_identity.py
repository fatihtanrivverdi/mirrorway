"""Network identity: the macOS network signature, ARP, the IPv6 neighbour table, weak recognition and matching rules."""
from conftest import DictCfg

from mirrorway import netlock as N

NETSTAT = """Routing tables

Internet:
Destination        Gateway            Flags               Netif Expire
default            192.168.1.1        UGScg                 en0
default            10.0.0.1           UGScIg                en7
127                127.0.0.1          UCS                   lo0
"""

SC_LIST = """  subKey [0] = State:/Network/Service/1A2B-WIFI/IPv4
  subKey [1] = State:/Network/Service/3C4D-USB/IPv4
"""

SC_SHOW = """<dictionary> {
  Addresses : <array> {
    0 : 10.0.0.5
  }
  InterfaceName : en7
  Router : 10.0.0.1
}
<dictionary> {
  ARPResolvedHardwareAddress : 4c:ed:fb:1:2:3
  ARPResolvedIPAddress : 192.168.1.1
  AdditionalRoutes : <array> {
    0 : <dictionary> {
      DestinationAddress : 192.168.1.117
      SubnetMask : 255.255.255.255
    }
  }
  Addresses : <array> {
    0 : 192.168.1.117
  }
  ConfirmedInterfaceName : en0
  InterfaceName : en0
  NetworkSignature : IPv4.Router=192.168.1.1;IPv4.RouterHardwareAddress=4c:ed:fb:01:02:03
  Router : 192.168.1.1
  SubnetMasks : <array> {
    0 : 255.255.255.0
  }
}
"""

NDP = """Neighbor                        Linklayer Address  Netif Expire    St Flgs Prbs
fe80::1%en0                     (incomplete)          en0 permanent R
fe80::4eed:fbff:fe01:203%en0    4c:ed:fb:1:2:3        en0 23h59m58s S  R
"""


def fake_run(table, calls=None):
    def run(args, input=None):
        cmd = " ".join(args) + (" <" + input.splitlines()[0] if input else "")
        if calls is not None:
            calls.append(cmd)
        for key, val in table.items():
            if key in cmd:
                return val
        return "", ""
    return run


# ---- parsers ---------------------------------------------------------------
def test_parsers():
    assert N.parse_netstat_defaults(NETSTAT) == {"en0": "192.168.1.1", "en7": "10.0.0.1"}
    assert N.parse_route_interface("    gateway: 192.168.1.1\n  interface: en0\n") == "en0"
    assert N.parse_route_gateway6("    gateway: fe80::4eed:fbff:fe01:203%en0\n") == "fe80::4eed:fbff:fe01:203"
    assert N.parse_route_gateway6("    gateway: 192.168.1.1\n") is None
    blocks = N.split_scutil_dicts(SC_SHOW)
    assert len(blocks) == 2
    info = N.parse_scutil(blocks[1])
    assert info["InterfaceName"] == "en0" and "DestinationAddress" not in info
    assert N.scutil_router(info) == ("192.168.1.1", "4c:ed:fb:01:02:03")
    # without a signature the ARPResolved fields (for the same gateway) are used
    assert N.scutil_router({"ARPResolvedIPAddress": "192.168.1.1", "ARPResolvedHardwareAddress": "a:b:c:d:e:f",
                            "Router": "192.168.1.1"}) == ("192.168.1.1", "0a:0b:0c:0d:0e:0f")
    assert N.parse_ndp(NDP, "fe80::4EED:fbff:fe01:203") == "4c:ed:fb:01:02:03"
    assert N.parse_ndp(NDP, "fe80::1") is None


def test_scutil_service_lookup():
    run = fake_run({"list State:/Network/Service": (SC_LIST, ""), "/usr/sbin/scutil <show": (SC_SHOW, "")})
    info, why = N.scutil_service_ipv4("en0", run)
    assert why == "" and info["Router"] == "192.168.1.1"
    info, why = N.scutil_service_ipv4("en9", run)
    assert info == {} and "en9" in why
    assert N.scutil_service_ipv4("en0", fake_run({}))[1].startswith("no service")


def test_interface_gateway_fallbacks():
    assert N.interface_gateway("en0", run=fake_run({"-ifscope": ("gateway: 192.168.1.1\n", "")})) == "192.168.1.1"
    r = fake_run({"-ifscope": ("", "route: writing to routing socket: not in table"),
                  "get default": ("gateway: 192.168.1.1\ninterface: en0\n", "")})
    assert N.interface_gateway("en0", run=r) == "192.168.1.1"
    r = fake_run({"-ifscope": ("", "not in table"), "get default": ("gateway: 10.8.0.1\ninterface: utun3\n", ""),
                  "netstat": (NETSTAT, "")})
    assert N.interface_gateway("en7", run=r) == "10.0.0.1"
    detail = []
    r = fake_run({"-ifscope": ("", "not in table"), "get default": ("", "not in table"),
                  "netstat": ("Internet:\n", "")})
    assert N.interface_gateway("en0", detail, run=r) is None
    assert detail[0].startswith("en0: gateway not found") and "not in table" in detail[0]


# ---- hardware address sources --------------------------------------------------------
def _mac(table, gw="192.168.1.1"):
    notes, calls, poked = [], [], []
    mac, src = N.router_mac("en0", gw, notes, run=fake_run(table, calls), poke=poked.append, sleep=lambda s: None)
    return mac, src, notes, calls, poked


def test_router_mac_from_network_signature():
    mac, src, notes, calls, poked = _mac({"list State:/Network/Service": (SC_LIST, ""),
                                          "/usr/sbin/scutil <show": (SC_SHOW, "")})
    assert (mac, src) == ("4c:ed:fb:01:02:03", "network signature") and not notes and not poked
    assert not any("arp" in c for c in calls)                       # ARP was never consulted


def test_router_mac_arp_scrubbed_falls_back_to_ndp():
    # the case where macOS shows apps an empty ARP table
    mac, src, notes, calls, poked = _mac({
        "list State:/Network/Service": ("", ""),
        "arp -n": ("", ""), "arp -an": ("", ""),
        "-inet6": ("   route to: default\n    gateway: fe80::4eed:fbff:fe01:203%en0\n  interface: en0\n", ""),
        "ndp -an": (NDP, "")})
    assert (mac, src) == ("4c:ed:fb:01:02:03", "IPv6 neighbour table")
    assert poked == ["192.168.1.1"]
    assert notes[0].startswith("network signature: no service") and notes[1] == "ARP: empty table"


def test_router_mac_from_arp_after_poke():
    arp_all = "? (192.168.1.1) at 0:1a:2b:3c:4d:5e on en0 ifscope [ethernet]\n"
    mac, src, notes, _calls, poked = _mac({"arp -n": ("", "192.168.1.1 (192.168.1.1) -- no entry"),
                                           "arp -an": (arp_all, "")})
    assert (mac, src) == ("00:1a:2b:3c:4d:5e", "ARP") and poked == ["192.168.1.1"]


def test_router_mac_none():
    mac, src, notes, *_ = _mac({"arp -an": ("? (192.168.1.1) at (incomplete) on en0 ifscope [ethernet]\n", "")})
    assert mac is None
    joined = "; ".join(notes)
    assert "incomplete" in joined and "IPv6: no router" in joined


# ---- network list: strong / weak ---------------------------------------------------------
IFS = [{"name": "en0", "kind": "lan", "broadcast": "192.168.1.255"},
       {"name": "en7", "kind": "lan", "broadcast": "10.0.0.255"},
       {"name": "utun3", "kind": "vpn"}]
GWS = {"en0": "192.168.1.1", "en7": "10.0.0.1"}


def test_current_networks_strong_hides_weak():
    nets = N.current_networks(interfaces_fn=lambda: IFS, gateway_fn=GWS.get,
                              mac_fn=lambda i, g, n: ("aa:bb:cc:dd:ee:01", "network signature") if i == "en0" else (None, ""))
    assert [(n["id"], n["source"]) for n in nets] == [("gw:aa:bb:cc:dd:ee:01", "network signature")]
    assert nets[0]["weak"] == "ip:192.168.1.1|192.168.1.255"


def test_current_networks_weak_only_with_reason():
    detail = []

    def mac_fn(i, g, notes):
        notes.append("ARP: empty table")
        return None, ""

    nets = N.current_networks(interfaces_fn=lambda: IFS[:1], gateway_fn=GWS.get, mac_fn=mac_fn, detail=detail)
    assert [n["id"] for n in nets] == ["ip:192.168.1.1|192.168.1.255"]
    assert detail == ["en0 (192.168.1.1): hardware address could not be read (ARP: empty table)"]


def test_mac_failure_is_cached(monkeypatch):
    N._mac_fail.clear()
    calls = []
    monkeypatch.setattr(N, "router_mac", lambda i, g, notes: (calls.append(1), notes.append("none"), (None, ""))[2])
    t = {"now": 100.0}
    kw = dict(interfaces_fn=lambda: IFS[:1], gateway_fn=GWS.get, clock=lambda: t["now"])
    d1, d2 = [], []
    N.current_networks(detail=d1, **kw)
    N.current_networks(detail=d2, **kw)
    assert len(calls) == 1 and d1 == d2                       # not retried within 60 s, the reason is kept
    t["now"] += N.MAC_RETRY_S + 1
    N.current_networks(**kw)
    assert len(calls) == 2
    N._mac_fail.clear()


# ---- matching rules -------------------------------------------------------------
HOME_W = {"id": "ip:192.168.1.1|192.168.1.255", "weak": "ip:192.168.1.1|192.168.1.255", "gateway": "192.168.1.1",
          "ifname": "en0", "source": "gateway address"}
HOME_S = dict(HOME_W, id="gw:aa:bb:cc:dd:ee:01", source="network signature")
CAFE_S = dict(HOME_W, id="gw:12:34:56:78:9a:bc", source="network signature")     # another network with the same IP


def _lock(nets):
    cur = {"n": nets}
    lock = N.NetworkLock(DictCfg(), probe=lambda: cur["n"])
    return lock, cur


def test_strong_home_rejects_same_ip_cafe():
    lock, cur = _lock([HOME_S])
    lock.refresh()
    assert lock.allowed and lock.trusted()[0]["id"] == HOME_S["id"] and not lock.weak()
    cur["n"] = [CAFE_S]
    lock.refresh()
    assert not lock.allowed and lock.state == "untrusted"
    cur["n"] = [HOME_W]                     # at home, a hardware address that briefly cannot be read does not shut down
    lock.refresh()
    assert lock.allowed and lock.weak() and "weak recognition" in lock.describe()


def test_weak_home_upgrades_to_strong(caplog):
    lock, cur = _lock([HOME_W])
    lock.refresh()
    assert lock.allowed and lock.trusted()[0]["id"].startswith("ip:")
    cur["n"] = [HOME_S]
    with caplog.at_level("INFO", logger="mirrorway.netlock"):
        lock.refresh()
    assert lock.allowed and lock.trusted()[0]["id"] == HOME_S["id"]
    assert any("recognised by the gateway's hardware address" in r.getMessage() for r in caplog.records)
    cur["n"] = [CAFE_S]                     # after the upgrade, a cafe with the same IP is refused
    lock.refresh()
    assert not lock.allowed


def test_weak_cafe_with_different_ip_rejected():
    lock, cur = _lock([HOME_W])
    lock.refresh()
    cur["n"] = [dict(HOME_W, id="ip:10.0.0.1|10.0.0.255", weak="ip:10.0.0.1|10.0.0.255", gateway="10.0.0.1")]
    lock.refresh()
    assert not lock.allowed
    lock.trust_current()
    assert lock.allowed and len(lock.trusted()) == 2


def test_old_entries_without_weak_key_still_match():
    lock, cur = _lock([HOME_S])
    lock.cfg.set("trusted_networks", [{"id": HOME_S["id"], "gateway": "192.168.1.1"}])
    lock.refresh()
    assert lock.allowed


def test_identity_logged_once(caplog):
    lock, cur = _lock([HOME_W])
    lock.detail = ["en0 (192.168.1.1): hardware address could not be read (ARP: empty table)"]
    with caplog.at_level("INFO", logger="mirrorway.netlock"):
        lock.refresh()
        lock.refresh()
        cur["n"] = [HOME_S]
        lock.refresh()
        lock.refresh()
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("weakly recognised" in m for m in msgs) == 1 and "ARP: empty table" in " ".join(msgs)
    assert sum(m.startswith("Network recognised") for m in msgs) == 1


def test_unknown_network_reason_logged_once(caplog):
    lock = N.NetworkLock(DictCfg())

    def probe():
        lock.detail = ["en0: gateway not found (route -ifscope: not in table)"]
        return []

    lock.probe = probe
    with caplog.at_level("WARNING", logger="mirrorway.netlock"):
        lock.refresh()
        lock.refresh()
    msgs = [r.getMessage() for r in caplog.records if "Network not recognised" in r.getMessage()]
    assert len(msgs) == 1 and "not in table" in msgs[0] and "connections accepted" in msgs[0]
    assert lock.state == "unknown" and lock.allowed
