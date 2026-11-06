"""Windows network adapter information (sent to the Mac for Wake-on-LAN).

Needs no administrator rights; PowerShell's NetAdapter cmdlets and `powercfg /devicequery` are used.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from typing import Optional

from ..wol import normalize_mac

log = logging.getLogger("mirrorway.netinfo")

CREATE_NO_WINDOW = 0x08000000

PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$armed = @(powercfg /devicequery wake_armed)
$fast = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power').HiberbootEnabled
$list = @(Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' } | ForEach-Object {
  $pm = Get-NetAdapterPowerManagement -Name $_.Name
  $adv = Get-NetAdapterAdvancedProperty -Name $_.Name |
         Where-Object { $_.DisplayName -match 'Magic' } | Select-Object -First 1
  $ip = @(Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 | ForEach-Object { $_.IPAddress })
  $prefix = @(Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 | ForEach-Object { $_.PrefixLength })
  $desc = $_.InterfaceDescription
  [pscustomobject]@{
    name = $_.Name; desc = $desc; media = [string]$_.PhysicalMediaType; mac = $_.MacAddress
    ips = $ip; prefix = $prefix; speed = [string]$_.LinkSpeed
    wake = [string]$pm.WakeOnMagicPacket
    adv = [string]$adv.DisplayValue
    armed = [bool]($armed -contains $desc)
  }
})
ConvertTo-Json -Compress -Depth 3 -InputObject @{ adapters = $list; fast_startup = $fast }
"""


def _as_list(v):
    # Windows PowerShell 5.1 sometimes writes arrays as {"value": [...], "Count": n}
    if isinstance(v, dict) and "value" in v:
        v = v["value"]
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def parse_netinfo(text: str) -> dict:
    """PowerShell JSON output -> {"adapters": [...], "fast_startup": bool|None}."""
    text = (text or "").strip().lstrip("﻿")
    if not text:
        return {"adapters": [], "fast_startup": None}
    data = json.loads(text)
    if isinstance(data, list):           # old format: just the adapter list
        data = {"adapters": data}
    adapters = []
    for a in _as_list(data.get("adapters")):
        if not isinstance(a, dict):
            continue
        mac = normalize_mac(a.get("mac") or "")
        if not mac:
            continue
        ips = [str(x) for x in _as_list(a.get("ips") or a.get("ip")) if x]
        if len(ips) == 1 and "," in ips[0]:
            ips = [x.strip() for x in ips[0].split(",") if x.strip()]
        prefix = _as_list(a.get("prefix"))
        adapters.append({
            "name": str(a.get("name") or ""),
            "desc": str(a.get("desc") or ""),
            "media": str(a.get("media") or ""),
            "mac": mac,
            "ips": ips,
            "prefix": int(prefix[0]) if prefix and str(prefix[0]).isdigit() else None,
            "speed": str(a.get("speed") or ""),
            "wake": str(a.get("wake") or ""),
            "adv": str(a.get("adv") or ""),
            "armed": bool(a.get("armed")),
        })
    fast = data.get("fast_startup")
    return {"adapters": adapters, "fast_startup": None if fast in (None, "") else bool(int(fast))}


_cache = {"t": 0.0, "v": None}


def collect(timeout: float = 20.0, max_age: float = 600.0) -> Optional[dict]:
    if sys.platform != "win32":
        return None
    now = time.monotonic()
    if _cache["v"] is not None and now - _cache["t"] < max_age:
        return _cache["v"]
    try:
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", PS_SCRIPT],
            capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        info = parse_netinfo(res.stdout.decode("utf-8", "replace"))
    except Exception as exc:
        log.warning("Could not get the network adapter information: %s", exc)
        return None
    _cache.update(t=now, v=info)
    return info
