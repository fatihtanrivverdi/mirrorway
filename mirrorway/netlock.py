"""Home network restriction: Mirrorway accepts connections only on trusted networks.

A network is recognised by the hardware (MAC) address of the default gateway of the local interface (Wi-Fi/Ethernet).
Unlike reading the Wi-Fi name, this needs no location permission on macOS; and because the route is queried per
interface, the right gateway is found even while a VPN is active.

The gateway's hardware address is read from these sources in turn:
  1. macOS's network signature (SystemConfiguration, `scutil`): the "IPv4.RouterHardwareAddress" the system
     records to recognise the network itself. Newer macOS versions may show apps an empty ARP table, so this
     is the first source.
  2. The ARP table (`arp`).
  3. The IPv6 neighbour table (`ndp`): a home router's IPv6 address usually carries the same hardware address.
If none works, "weak recognition" is used: the gateway's IP address and the broadcast address. This cannot tell
apart another network using the same addresses (e.g. a café that also uses 192.168.1.1); the menu and the log say so.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

from .i18n import T

log = logging.getLogger("mirrorway.netlock")

_GW = re.compile(r"^\s*gateway:\s*(\S+)", re.M)
_IFACE = re.compile(r"^\s*interface:\s*(\S+)", re.M)
_IPV4 = re.compile(r"\d+\.\d+\.\d+\.\d+")
_KV = re.compile(r"^\s*([A-Za-z0-9]+) : (.+?)\s*$")

STRONG_PREFIX = "gw:"
WEAK_PREFIX = "ip:"
MAC_RETRY_S = 60.0          # retry interval when no source yielded the hardware address


def _run(args: List[str], input: Optional[str] = None) -> Tuple[str, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=3, input=input)
        return r.stdout or "", (r.stderr or "").strip()
    except Exception as exc:
        return "", str(exc)


def _short(text: str, n: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def parse_route_gateway(text: str) -> Optional[str]:
    m = _GW.search(text or "")
    if not m:
        return None
    gw = m.group(1)
    return gw if _IPV4.fullmatch(gw) else None


def parse_route_gateway6(text: str) -> Optional[str]:
    m = _GW.search(text or "")
    if not m:
        return None
    gw = m.group(1).split("%")[0].lower()
    return gw if gw.startswith("fe80:") else None


def parse_route_interface(text: str) -> Optional[str]:
    m = _IFACE.search(text or "")
    return m.group(1) if m else None


def parse_netstat_defaults(text: str) -> Dict[str, str]:
    """Default routes in `netstat -rn -f inet` output: {interface: gateway} (first one seen)."""
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "default" and _IPV4.fullmatch(parts[1]):
            ifname = next((p for p in reversed(parts[2:]) if re.fullmatch(r"[a-z]+\d+", p)), None)
            if ifname and ifname not in out:
                out[ifname] = parts[1]
    return out


def parse_scutil(text: str) -> Dict[str, str]:
    """Top-level keys of `scutil` `show` output (nested dictionary/array contents are skipped)."""
    out: Dict[str, str] = {}
    lines = (text or "").splitlines()
    base = 1 if any(ln.startswith("<dictionary>") for ln in lines) else 0
    depth = 0
    for line in lines:
        st = line.strip()
        if st.endswith("{"):
            depth += 1
            continue
        if st == "}":
            depth -= 1
            continue
        m = _KV.match(line)
        if depth == base and m and m.group(1) not in out:
            out[m.group(1)] = m.group(2)
    return out


def split_scutil_dicts(text: str) -> List[str]:
    """Splits consecutive `show` outputs into top-level dictionary blocks."""
    blocks, cur = [], None
    for line in (text or "").splitlines():
        while line.startswith("> "):              # interactive prompt (if any)
            line = line[2:]
        if line.startswith("<dictionary> {"):
            cur = []
            continue
        if cur is not None:
            if line.startswith("}"):
                blocks.append("\n".join(cur))
                cur = None
            else:
                cur.append(line)
    return blocks


def scutil_service_ipv4(ifname: str, run=_run) -> Tuple[Dict[str, str], str]:
    """IPv4 state of the interface's network service (DHCP: Router, NetworkSignature, ARPResolved...)."""
    out, err = run(["/usr/sbin/scutil"], input="list State:/Network/Service/[^/]+/IPv4\nquit\n")
    keys = re.findall(r"=\s*(State:/Network/Service/[^/\s]+/IPv4)", out or "")
    if not keys:
        return {}, "no service" + (f" ({_short(err)})" if err else "")
    cmds = "".join(f"show {k}\n" for k in keys[:8]) + "quit\n"
    out, err = run(["/usr/sbin/scutil"], input=cmds)
    for block in split_scutil_dicts(out):
        info = parse_scutil(block)
        if info.get("InterfaceName") == ifname or info.get("ConfirmedInterfaceName") == ifname:
            return info, ""
    return {}, f"no service for {ifname}"


def scutil_router(info: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """(gateway IP, hardware address) from the network signature."""
    from .wol import normalize_mac
    sig = {}
    for part in (info.get("NetworkSignature") or "").split(";"):
        k, _, v = part.partition("=")
        sig[k.strip()] = v.strip()
    gw = sig.get("IPv4.Router") or info.get("ARPResolvedIPAddress") or info.get("Router")
    mac = normalize_mac(sig.get("IPv4.RouterHardwareAddress") or "")
    if not mac and info.get("ARPResolvedIPAddress") and info.get("ARPResolvedIPAddress") == gw:
        mac = normalize_mac(info.get("ARPResolvedHardwareAddress") or "")
    if gw and not _IPV4.fullmatch(gw):
        gw = None
    return gw, mac


def parse_ndp(text: str, addr: str) -> Optional[str]:
    """Hardware address of an IPv6 address from `ndp -an` output."""
    from .wol import normalize_mac
    want = addr.split("%")[0].lower()
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].split("%")[0].lower() == want:
            mac = normalize_mac(parts[1])
            if mac:
                return mac
    return None


def interface_gateway(ifname: str, detail: Optional[List[str]] = None, run=_run) -> Optional[str]:
    """Default gateway of the interface (macOS). In order: interface-scoped route, global route, routing table."""
    if sys.platform != "darwin" and run is _run:
        return None
    notes = []
    out, err = run(["/sbin/route", "-n", "get", "-ifscope", ifname, "default"])
    gw = parse_route_gateway(out)
    if gw:
        return gw
    notes.append("route -ifscope: " + _short(err or out or "empty"))
    out, err = run(["/sbin/route", "-n", "get", "default"])
    gw, via = parse_route_gateway(out), parse_route_interface(out)
    if gw and via == ifname:
        return gw
    notes.append("route: " + (f"{gw} via {via}" if gw else _short(err or out or "empty")))
    out, err = run(["/usr/sbin/netstat", "-rn", "-f", "inet"])
    gw = parse_netstat_defaults(out).get(ifname)
    if gw:
        return gw
    notes.append("netstat: " + ("no default route" if out else _short(err or "empty")))
    if detail is not None:
        detail.append(f"{ifname}: gateway not found ({'; '.join(notes)})")
    return None


def _poke(ip: str) -> None:
    """If the ARP table has no entry, send one empty UDP packet to the gateway so that one appears."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(b"", (ip, 9))
        finally:
            s.close()
    except OSError:
        pass


def router_mac(ifname: str, gw: str, notes: List[str], run=_run, poke=_poke,
               sleep=time.sleep) -> Tuple[Optional[str], str]:
    """The gateway's hardware address and its source. Why failed sources failed is appended to `notes`."""
    from .wol import parse_arp
    # 1) macOS network signature
    info, why = scutil_service_ipv4(ifname, run)
    sgw, mac = scutil_router(info)
    if mac and (sgw is None or sgw == gw):
        return mac, "network signature"
    notes.append("network signature: " + (why or ("no hardware address" if not mac else f"different gateway ({sgw})")))
    # 2) ARP table (if there is no entry, send a packet and look again)
    out, err = run(["/usr/sbin/arp", "-n", gw])
    mac = parse_arp(out, gw)
    if not mac:
        poke(gw)
        sleep(0.3)
        out, err = run(["/usr/sbin/arp", "-an"])
        mac = parse_arp(out, gw)
    if mac:
        return mac, "ARP"
    line = next((ln for ln in out.splitlines() if f"({gw})" in ln), "")
    notes.append("ARP: " + _short(line or err or ("no entry" if out.strip() else "empty table")))
    # 3) IPv6 neighbour table (the router's IPv6 link-local address)
    out, err = run(["/sbin/route", "-n", "get", "-inet6", "-ifscope", ifname, "default"])
    gw6 = parse_route_gateway6(out)
    if not gw6:
        notes.append("IPv6: no router")
        return None, ""
    out, err = run(["/usr/sbin/ndp", "-an"])
    mac = parse_ndp(out, gw6)
    if mac:
        return mac, "IPv6 neighbour table"
    notes.append(f"IPv6 ({gw6}): " + ("no entry" if out.strip() else _short(err or "empty table")))
    return None, ""


def weak_id(gw: str, broadcast: Optional[str]) -> str:
    return f"{WEAK_PREFIX}{gw}|{broadcast or '?'}"


def is_strong(net_id: str) -> bool:
    return str(net_id).startswith(STRONG_PREFIX)


_mac_fail: Dict[Tuple[str, str], Tuple[float, List[str]]] = {}


def current_networks(interfaces_fn=None, gateway_fn=None, arp_fn=None,
                     detail: Optional[List[str]] = None, mac_fn=None, clock=time.monotonic) -> List[dict]:
    """Networks of the local interfaces: [{id, weak, gateway, ifname, source}].

    id: "gw:<hardware address>" (strong) or, if the hardware address cannot be read, "ip:<gateway>|<broadcast>" (weak).
    If any interface is recognised strongly, weak ones are left out. Reasons for skipping go to `detail`.
    """
    if interfaces_fn is None:
        from .discovery import interfaces as interfaces_fn
    if gateway_fn is None:
        def gateway_fn(ifn):
            return interface_gateway(ifn, detail)
    if mac_fn is None:
        if arp_fn is not None:
            def mac_fn(ifn, gw, notes):
                m = arp_fn(gw)
                if not m:
                    notes.append("ARP: no entry")
                return m, "ARP"
        else:
            def mac_fn(ifn, gw, notes):
                key = (ifn, gw)
                failed = _mac_fail.get(key)
                if failed and clock() - failed[0] < MAC_RETRY_S:
                    notes.extend(failed[1])
                    return None, ""
                m, src = router_mac(ifn, gw, notes)
                if m:
                    _mac_fail.pop(key, None)
                else:
                    _mac_fail[key] = (clock(), list(notes))
                return m, src
    strong, weak = [], []
    lans = [d for d in interfaces_fn() if d.get("kind") == "lan"]
    if not lans and detail is not None:
        detail.append("no local network interface")
    for d in lans:
        gw = gateway_fn(d["name"])
        if not gw:
            continue
        w = weak_id(gw, d.get("broadcast"))
        notes: List[str] = []
        mac, src = mac_fn(d["name"], gw, notes)
        if mac:
            strong.append({"id": STRONG_PREFIX + mac, "weak": w, "gateway": gw, "ifname": d["name"], "source": src})
        else:
            weak.append({"id": w, "weak": w, "gateway": gw, "ifname": d["name"], "source": "gateway address"})
            if detail is not None:
                detail.append(f"{d['name']} ({gw}): hardware address could not be read ({'; '.join(notes)})")
    return strong or weak


class NetworkLock:
    """List of trusted networks and the state of the current network.

    state: "trusted" (a trusted network), "untrusted" (recognised but not trusted),
           "unknown" (no network identity found: the last decision stands), "off" (restriction disabled).
    Trusted network record: {id, weak, gateway, added}. A strongly recognised network only matches its strong
    identity (another network with the same IP does not match); without a hardware address the weak identity is compared.
    """

    def __init__(self, cfg, probe: Optional[Callable[[], List[dict]]] = None, clock=time.monotonic):
        self.cfg = cfg
        self.probe = probe or self._probe
        self.clock = clock
        self.networks: List[dict] = []
        self.detail: List[str] = []       # why the network was not recognised / only weakly recognised
        self._logged_detail = None
        self._logged_ident = None
        self.state = "unknown"
        self.allowed = True               # whether connections are accepted (the last decision)

    def _probe(self) -> List[dict]:
        detail: List[str] = []
        nets = current_networks(detail=detail)
        self.detail = detail
        return nets

    def enabled(self) -> bool:
        return bool(self.cfg.get("network_lock", True))

    def trusted(self) -> List[dict]:
        return [n for n in (self.cfg.get("trusted_networks") or []) if isinstance(n, dict) and n.get("id")]

    def weak(self) -> bool:
        """Was the current network recognised only by its weak identity?"""
        return bool(self.networks) and not any(is_strong(n["id"]) for n in self.networks)

    @staticmethod
    def _match(n: dict, trusted: List[dict]) -> Optional[dict]:
        if is_strong(n["id"]):
            for t in trusted:
                if t["id"] == n["id"]:
                    return t
            for t in trusted:             # a network saved only weakly: upgraded to the strong identity
                if not is_strong(t["id"]) and n.get("weak") and t.get("weak", t["id"]) == n["weak"]:
                    return t
            return None
        for t in trusted:
            if n.get("weak") and t.get("weak", t["id"]) == n["weak"]:
                return t
        return None

    @staticmethod
    def _entry(n: dict) -> dict:
        return {"id": n["id"], "weak": n.get("weak"), "gateway": n["gateway"], "added": int(time.time())}

    def trust_current(self) -> Optional[dict]:
        nets = self.networks or self.probe()
        if not nets:
            return None
        lst = self.trusted()
        added = None
        for n in nets:
            if self._match(n, lst) is None:
                added = self._entry(n)
                lst.append(added)
        self.cfg.set("trusted_networks", lst)
        self.refresh()
        return added or nets[0]

    def forget_all(self) -> None:
        self.cfg.set("trusted_networks", [])
        self.refresh()

    def refresh(self) -> bool:
        """Checks the network again; True if permission changed."""
        before = self.allowed
        try:
            self.networks = self.probe()
        except Exception as exc:
            log.warning("Could not get the network identity: %s", exc)
            self.networks, self.detail = [], [f"error: {exc}"]
        if not self.enabled():
            self.state, self.allowed = "off", True
            return before != self.allowed
        if not self.networks:
            self.state = "unknown"                 # the decision does not change (brief outage, cable etc.)
            why = "; ".join(self.detail) or "reason unknown"
            if why != self._logged_detail:
                self._logged_detail = why
                log.warning("Network not recognised (%s); keeping the last decision: %s", why,
                            "connections accepted" if self.allowed else "Mirrorway off")
            return False
        self._logged_detail = None
        self._log_identity()
        lst = self.trusted()
        if not lst:
            # First time: the network Mirrorway is set up on is taken as the home network
            self.trust_current_silent()
            lst = self.trusted()
            log.info("Saved as the home network: %s", ", ".join(n["gateway"] for n in self.networks))
        ok, upgraded = False, False
        for n in self.networks:
            t = self._match(n, lst)
            if t is None:
                continue
            ok = True
            if is_strong(n["id"]) and not is_strong(t["id"]):
                t["id"], t["weak"] = n["id"], n.get("weak")
                upgraded = True
                log.info("Home network now recognised by the gateway's hardware address (%s)", n["gateway"])
        if upgraded:
            self.cfg.set("trusted_networks", lst)
        self.state, self.allowed = ("trusted", True) if ok else ("untrusted", False)
        return before != self.allowed

    def _log_identity(self) -> None:
        sig = tuple((n["ifname"], n["gateway"], n.get("source")) for n in self.networks)
        if sig == self._logged_ident:
            return
        self._logged_ident = sig
        for n in self.networks:
            if is_strong(n["id"]):
                log.info("Network recognised: %s, gateway %s (source: %s)", n["ifname"], n["gateway"], n.get("source"))
            else:
                log.warning("Network weakly recognised: %s, gateway %s — hardware address unreadable (%s); another "
                            "network using the same addresses cannot be told apart", n["ifname"], n["gateway"], "; ".join(self.detail))

    def trust_current_silent(self) -> None:
        lst = self.trusted()
        for n in self.networks:
            if self._match(n, lst) is None:
                lst.append(self._entry(n))
        self.cfg.set("trusted_networks", lst)

    def describe(self) -> str:
        gws = ", ".join(n["gateway"] for n in self.networks) or "?"
        weak = T("; weak recognition") if self.weak() else ""
        if self.state == "off":
            return T("Network restriction off (works on any network)")
        if self.state == "trusted":
            return T("Home network ✓ (gateway {gateways}{weak})").format(gateways=gws, weak=weak)
        if self.state == "untrusted":
            return T("Unknown network ({gateways}{weak}) — Mirrorway is off on this network").format(gateways=gws, weak=weak)
        return T("Network could not be determined")
