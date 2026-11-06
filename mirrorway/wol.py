"""Wake-on-LAN: wake a sleeping/powered-off Windows PC from the Mac with a "magic packet".

Magic packet: 6 × 0xFF followed by 16 repetitions of the target MAC address (102 bytes), broadcast to UDP port 9
(and 7). The packet carries no secrets; it only wakes a device on the same local network.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
from typing import List, Optional, Tuple

log = logging.getLogger("mirrorway.wol")

WOL_PORTS = (9, 7)
_HEX = re.compile(r"[0-9a-fA-F]{1,2}")


def normalize_mac(mac: str) -> Optional[str]:
    """'AA-BB-CC-DD-EE-FF', 'aa:bb:..', 'a:b:c:..' (macOS arp shorthand) -> 'aa:bb:cc:dd:ee:ff'."""
    if not mac:
        return None
    s = str(mac).strip()
    if re.fullmatch(r"[0-9a-fA-F]{12}", s):
        parts = [s[i:i + 2] for i in range(0, 12, 2)]
    else:
        parts = re.split(r"[:\-.]", s)
        if len(parts) == 3 and all(len(p) == 4 for p in parts):        # Cisco format aabb.ccdd.eeff
            parts = [p[i:i + 2] for p in parts for i in (0, 2)]
    if len(parts) != 6 or not all(_HEX.fullmatch(p) for p in parts):
        return None
    out = ":".join(p.zfill(2).lower() for p in parts)
    if out in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        return None
    return out


def magic_packet(mac: str) -> bytes:
    norm = normalize_mac(mac)
    if norm is None:
        raise ValueError(f"invalid MAC address: {mac!r}")
    raw = bytes(int(p, 16) for p in norm.split(":"))
    return b"\xff" * 6 + raw * 16


_ARP_LINE = re.compile(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-fA-F:]+)")


def parse_arp(text: str, ip: str) -> Optional[str]:
    """The MAC address of ip from `arp -n <ip>` / `arp -an` output."""
    for m in _ARP_LINE.finditer(text or ""):
        if m.group(1) == ip:
            mac = normalize_mac(m.group(2))
            if mac:
                return mac
    return None


def arp_lookup(ip: str) -> Optional[str]:
    """MAC from the local ARP table (macOS/Linux only; present while Windows is connected)."""
    if sys.platform == "win32" or not ip:
        return None
    for exe in ("/usr/sbin/arp", "/sbin/arp", "arp"):
        try:
            out = subprocess.run([exe, "-n", ip], capture_output=True, text=True, timeout=3).stdout
        except Exception:
            continue
        mac = parse_arp(out, ip)
        if mac:
            return mac
    return None


def subnet_broadcast(ip: str, prefix: int = 24) -> Optional[str]:
    try:
        parts = [int(p) for p in ip.split(".")]
        if len(parts) != 4:
            return None
    except ValueError:
        return None
    n = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    b = n | (~mask & 0xFFFFFFFF)
    return ".".join(str((b >> s) & 0xFF) for s in (24, 16, 8, 0))


def wol_targets(last_ip: Optional[str] = None) -> List[Tuple[Optional[str], str]]:
    """(interface, destination) pairs: the global broadcast from every LAN interface, subnet broadcasts, the last IP."""
    from .discovery import _broadcast_targets
    targets = list(_broadcast_targets())
    if last_ip:
        b = subnet_broadcast(last_ip)
        if b and (None, b) not in targets:
            targets.append((None, b))
        targets.append((None, last_ip))     # goes directly while the ARP entry is still there
    seen, out = set(), []
    for t in targets:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def send_wol(mac: str, last_ip: Optional[str] = None, repeat: int = 3, targets=None,
             socket_factory=None) -> int:
    """Sends the magic packet; returns the number of packets sent successfully."""
    pkt = magic_packet(mac)
    if targets is None:
        targets = wol_targets(last_ip)
    if socket_factory is None:
        from .discovery import _bound_socket as socket_factory
    socks = {}
    sent = 0
    try:
        for i in range(max(1, repeat)):
            for ifname, addr in targets:
                for port in WOL_PORTS:
                    try:
                        if ifname not in socks:
                            socks[ifname] = socket_factory(ifname)
                        socks[ifname].sendto(pkt, (addr, port))
                        sent += 1
                    except OSError as exc:
                        log.debug("Could not send WoL (%s via %s): %s", addr, ifname, exc)
            if i + 1 < repeat:
                time.sleep(0.2)
    finally:
        for s in socks.values():
            try:
                s.close()
            except Exception:
                pass
    log.info("Wake packet sent: %s (%d packets, %d destinations)", mac, sent, len(targets))
    return sent


def pick_adapter(adapters: list, peer_ip: str) -> Optional[dict]:
    """The adapter that owns the IP the connection came from; else Ethernet; else the first one with a MAC."""
    valid = [a for a in adapters or [] if normalize_mac(a.get("mac", ""))]
    for a in valid:
        if peer_ip in (a.get("ips") or []):
            return a
    for a in valid:
        if is_wired(a):
            return a
    return valid[0] if valid else None


def is_wired(adapter: dict) -> bool:
    media = f"{adapter.get('media', '')} {adapter.get('desc', '')} {adapter.get('name', '')}".lower()
    if any(w in media for w in ("802.11", "wireless", "wi-fi", "wifi", "wlan", "kablosuz")):     # "kablosuz" = wireless on Turkish Windows
        return False
    return "802.3" in media or "ethernet" in media or "gbe" in media


class WakeSession:
    """A wake attempt: resends the packet at intervals and ends when a connection arrives.

    state: "waking" -> "woke" (Windows connected) or "timeout".
    """

    RESEND = 10.0
    TIMEOUT = 120.0

    def __init__(self, send_fn, clock=time.monotonic):
        self.send_fn = send_fn
        self.clock = clock
        self.state = "idle"
        self.t0 = 0.0
        self.last = 0.0
        self.sends = 0
        self.elapsed = 0.0

    def _send(self) -> None:
        self.last = self.clock()
        self.sends += 1
        try:
            self.send_fn()
        except Exception as exc:
            log.warning("Could not send the wake packet: %s", exc)

    def start(self) -> None:
        self.t0 = self.clock()
        self.sends = 0
        self.state = "waking"
        self._send()

    def remaining(self) -> int:
        return max(0, int(self.TIMEOUT - (self.clock() - self.t0) + 0.999))

    def tick(self, connected: bool) -> str:
        if self.state != "waking":
            return self.state
        now = self.clock()
        self.elapsed = now - self.t0
        if connected:
            self.state = "woke"
            log.info("Windows woke up and connected (%.0f s, %d sends)", self.elapsed, self.sends)
        elif self.elapsed >= self.TIMEOUT:
            self.state = "timeout"
            log.warning("Windows did not wake up within %d s (%d sends)", int(self.TIMEOUT), self.sends)
        elif now - self.last >= self.RESEND:
            self._send()
        return self.state
