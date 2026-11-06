"""Automatic discovery on the local network: the Mac broadcasts over UDP every 2 s, Windows listens.

The beacon carries no secrets (only a name and a port); the connection itself is still authenticated.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

from .config import DISCOVERY_PORT

log = logging.getLogger("mirrorway.discovery")


def local_ip() -> str:
    """IPv4 address of the default network interface (without sending anything)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Network interfaces (to find the Wi-Fi/Ethernet address on macOS even when a VPN takes the default route)
# ---------------------------------------------------------------------------
_VPN_PREFIXES = ("utun", "ipsec", "ppp", "tun", "tap", "wg")
_SKIP_PREFIXES = ("lo", "awdl", "llw", "anpi", "ap", "gif", "stf", "bridge", "vmenet", "vnic")
_HDR = re.compile(r"^(\S+?):\s+flags=\w+<([^>]*)>")
_INET = re.compile(r"^\s+inet (\d+\.\d+\.\d+\.\d+)(?: --> \S+)?\s+netmask (0x[0-9a-fA-F]+|[\d.]+)"
                   r"(?:\s+broadcast (\d+\.\d+\.\d+\.\d+))?")


def parse_ifconfig(text: str) -> List[dict]:
    """IPv4 interfaces from `ifconfig` output: [{name, ip, broadcast, kind}] (kind: lan/vpn)."""
    out, name, flags = [], None, ""
    for line in text.splitlines():
        m = _HDR.match(line)
        if m:
            name, flags = m.group(1), m.group(2)
            continue
        m = _INET.match(line)
        if not m or name is None or "UP" not in flags.split(","):
            continue
        ip, bcast = m.group(1), m.group(3)
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        if name.startswith(_VPN_PREFIXES):
            kind = "vpn"
        elif name.startswith(_SKIP_PREFIXES):
            continue
        else:
            kind = "lan"
        out.append({"name": name, "ip": ip, "broadcast": bcast, "kind": kind})
    out.sort(key=lambda d: (d["kind"] != "lan", d["name"] != "en0", d["name"]))
    return out


def interfaces() -> List[dict]:
    if sys.platform == "darwin":
        try:
            text = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=5).stdout
            found = parse_ifconfig(text)
            if found:
                return found
        except Exception:
            pass
    ip = local_ip()
    parts = ip.split(".")
    return [{"name": "default", "ip": ip, "broadcast": ".".join(parts[:3] + ["255"]), "kind": "lan"}]


def lan_ips() -> List[str]:
    ips = [d["ip"] for d in interfaces() if d["kind"] == "lan"]
    return ips or [local_ip()]


def vpn_active() -> bool:
    return any(d["kind"] == "vpn" for d in interfaces())


def _broadcast_targets() -> List[Tuple[Optional[str], str]]:
    """(interface name, destination) pairs."""
    targets: List[Tuple[Optional[str], str]] = [(None, "255.255.255.255")]
    for d in interfaces():
        if d["kind"] != "lan":
            continue
        targets.append((d["name"], "255.255.255.255"))
        if d.get("broadcast"):
            targets.append((None, d["broadcast"]))
    return targets


def _bound_socket(ifname: Optional[str]):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if ifname and sys.platform == "darwin":
        try:  # IP_BOUND_IF = 25: force the packet out of this interface (bypass the VPN)
            sock.setsockopt(socket.IPPROTO_IP, 25, socket.if_nametoindex(ifname))
        except OSError:
            pass
    return sock


class Beacon(threading.Thread):
    def __init__(self, name: str, port: int, interval: float = 2.0):
        super().__init__(daemon=True, name="mirrorway-beacon")
        self.name_ = name
        self.port = port
        self.interval = interval
        self.enabled_fn = None          # when it returns False, nothing is announced (e.g. untrusted network)
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        socks = {}
        n = 0
        targets = []
        while not self._stop_evt.is_set():
            if self.enabled_fn is not None and not self.enabled_fn():
                self._stop_evt.wait(self.interval)
                continue
            if n % 15 == 0:            # rescan for network changes every ~30 s
                targets = _broadcast_targets()
            n += 1
            msg = json.dumps({"app": "mirrorway", "v": 1, "name": self.name_, "port": self.port}).encode()
            for ifname, target in targets:
                try:
                    if ifname not in socks:
                        socks[ifname] = _bound_socket(ifname)
                    socks[ifname].sendto(msg, (target, DISCOVERY_PORT))
                except OSError:
                    pass
            self._stop_evt.wait(self.interval)
        for s_ in socks.values():
            s_.close()


class Listener(threading.Thread):
    """Keeps the Mirrorway servers seen so far as {ip: (name, port, last_seen)}."""

    def __init__(self):
        super().__init__(daemon=True, name="mirrorway-discovery")
        self.servers: Dict[str, Tuple[str, int, float]] = {}
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self.error: Optional[str] = None

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", DISCOVERY_PORT))
        except OSError as exc:
            self.error = str(exc)
            log.warning("Cannot listen on the discovery port: %s", exc)
            return
        sock.settimeout(1.0)
        while not self._stop_evt.is_set():
            try:
                data, (ip, _) = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                info = json.loads(data.decode())
                if info.get("app") != "mirrorway":
                    continue
                with self._lock:
                    self.servers[ip] = (str(info.get("name", ip)), int(info.get("port")), time.time())
            except Exception:
                continue
        sock.close()

    def best(self, max_age: float = 10.0) -> Optional[Tuple[str, str, int]]:
        """The most recently seen server: (ip, name, port)."""
        now = time.time()
        with self._lock:
            live = [(seen, ip, name, port) for ip, (name, port, seen) in self.servers.items()
                    if now - seen <= max_age]
        if not live:
            return None
        live.sort(reverse=True)
        _, ip, name, port = live[0]
        return ip, name, port

    def all(self, max_age: float = 10.0):
        now = time.time()
        with self._lock:
            return [(ip, name, port) for ip, (name, port, seen) in self.servers.items() if now - seen <= max_age]
