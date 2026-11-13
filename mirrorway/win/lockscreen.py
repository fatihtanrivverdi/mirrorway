"""Lock screen support on Windows: where its parts live and what they share.

Three parts (Settings › General › "Install lock screen support", one UAC prompt):
  - the service "MirrorwaySecure" (LocalSystem, mirrorway.win.service): keeps the helper running in the session at
    the console and sends Ctrl+Alt+Del (SendSAS) when the helper asks;
  - the helper (LocalSystem in the user's session, mirrorway.win.secure_helper): connects to the Mac itself and types
    on the lock screen, the Ctrl+Alt+Del screen and UAC prompts (the "Winlogon" desktop, closed to normal programs);
  - their files: the code and a copy of Python under Program Files (only administrators may change them, the in-app
    update does not touch them), the trust file next to them, readable only by SYSTEM and administrators.

The trust file holds this PC's device key and the paired Mac's public key, copied from the user's settings when it is
installed. The helper takes input only from that Mac, over its own encrypted connection: a program running as the
user cannot make it type, so it cannot be used to approve UAC prompts.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Optional

SERVICE_NAME = "MirrorwaySecure"
SERVICE_DISPLAY = "Mirrorway lock screen"
SERVICE_PROTOCOL = 1               # raised when the helper and the Mac need each other's newer parts
TRUST_VERSION = 1
SAS_POLICY_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
SAS_POLICY_VALUE = "SoftwareSASGeneration"     # 1 = services may simulate Ctrl+Alt+Del
CRYPTPROTECT_UI_FORBIDDEN, CRYPTPROTECT_LOCAL_MACHINE = 0x1, 0x4
ENTROPY = b"mirrorway-lock-screen"


def program_dir() -> Path:
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Mirrorway" / "Service"


def data_dir() -> Path:
    """The trust file, state and logs: next to the program under Program Files (not ProgramData, where any user may
    create folders and so could prepare or swap ours), readable only by SYSTEM and administrators."""
    return program_dir().parent / "ServiceData"


def trust_path() -> Path:
    return data_dir() / "trust.json"


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


# ---- DPAPI, machine scope (the file's permissions are the real lock) --------------------------------------------
def _dpapi(fn_name: str, data: bytes, flags: int) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes):
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf
    b_in, _k1 = blob(data)
    ent, _k2 = blob(ENTROPY)
    out = DATA_BLOB()
    fn = getattr(ctypes.windll.crypt32, fn_name)
    if fn_name == "CryptProtectData":
        ok = fn(ctypes.byref(b_in), "Mirrorway", ctypes.byref(ent), None, None, flags, ctypes.byref(out))
    else:
        ok = fn(ctypes.byref(b_in), None, ctypes.byref(ent), None, None, flags, ctypes.byref(out))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(secret: bytes) -> str:
    return "dpapi-machine:" + b64e(_dpapi("CryptProtectData", secret,
                                          CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_LOCAL_MACHINE))


def unprotect(value: str) -> bytes:
    if value.startswith("plain:"):                    # tests and CI only
        return b64d(value[6:])
    if not value.startswith("dpapi-machine:"):
        raise ValueError("unknown key format")
    return _dpapi("CryptUnprotectData", b64d(value[len("dpapi-machine:"):]), CRYPTPROTECT_UI_FORBIDDEN)


# ---- the trust file -------------------------------------------------------------------------------------------------
def make_trust(device_key: bytes, mac_pub: bytes, mac_name: str, config: str, port: int, plain: bool = False) -> dict:
    return {"version": TRUST_VERSION, "device_key": ("plain:" + b64e(device_key)) if plain else protect(device_key),
            "mac_pub": b64e(mac_pub), "mac_name": mac_name, "config": str(config), "port": int(port)}


def read_trust(path: Optional[Path] = None) -> dict:
    """The trust file as {"identity", "mac_pub", "mac_name", "config", "port"}; raises if it is missing or broken."""
    from ..devkeys import Identity
    raw = json.loads(Path(path or trust_path()).read_text("utf-8"))
    if int(raw.get("version") or 0) != TRUST_VERSION:
        raise ValueError(f"trust file version {raw.get('version')}")
    return {"identity": Identity.from_bytes(unprotect(str(raw["device_key"]))), "mac_pub": b64d(raw["mac_pub"]),
            "mac_name": str(raw.get("mac_name") or "Mac"), "config": str(raw.get("config") or ""),
            "port": int(raw.get("port") or 24850)}


def mac_address(trust: dict) -> Optional[tuple]:
    """Where the Mac is: the address in the user's Mirrorway settings (set by hand, or the one it connected to last).
    Not trusted: the Mac's key decides whether the other side is really the Mac."""
    try:
        cfg = json.loads(Path(trust["config"]).read_text("utf-8"))
    except Exception:
        return None
    host = str(cfg.get("host") or cfg.get("last_mac_host") or "").strip()
    if not host:
        return None
    port = trust.get("port") or 24850
    if ":" in host and host.count(":") == 1:
        host, p = host.split(":")
        try:
            port = int(p)
        except ValueError:
            pass
    return host, int(port)
