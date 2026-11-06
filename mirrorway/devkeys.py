"""Device identities: a persistent X25519 key pair per device.

The private key lives in the system's secret store:
  - macOS: Keychain (the security command; only that user can read it)
  - Windows: DPAPI (CryptProtectData; only that Windows user can decrypt it), the encrypted form in the settings file
  - other (development/tests): in the settings file in the clear (base64)
Public keys and the list of paired devices are not secret; they live in the settings file.
Keys stored by Köprü (Mirrorway's former name) are still read and moved over on first use.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import subprocess
import sys
from typing import Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

log = logging.getLogger("mirrorway.devkeys")

KEYCHAIN_SERVICE = "app.mirrorway"
LEGACY_KEYCHAIN_SERVICE = "app.kopru"          # Köprü 1.x–2.x
DPAPI_ENTROPY = b"mirrorway-device-key"
LEGACY_DPAPI_ENTROPY = b"kopru-device-key"     # Köprü 1.x–2.x


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def fingerprint(pub: bytes) -> str:
    """Short, readable fingerprint, e.g. 'a1b2-c3d4'."""
    h = hashlib.sha256(b"mirrorway-device|" + pub).hexdigest()
    return f"{h[:4]}-{h[4:8]}"


class Identity:
    def __init__(self, private: X25519PrivateKey):
        self.private = private
        self.public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.fingerprint = fingerprint(self.public)

    @classmethod
    def generate(cls) -> "Identity":
        return cls(X25519PrivateKey.generate())

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Identity":
        return cls(X25519PrivateKey.from_private_bytes(raw))

    def private_bytes(self) -> bytes:
        return self.private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                          serialization.NoEncryption())

    def dh(self, peer_pub: bytes) -> bytes:
        return self.private.exchange(X25519PublicKey.from_public_bytes(peer_pub))


# ---------------------------------------------------------------------------
# Secret storage
# ---------------------------------------------------------------------------
class PlainStore:
    """Development/tests: base64 in the settings file (not secret)."""
    name = "file"

    def save(self, cfg, key: str, secret: bytes) -> None:
        cfg.set(key, "plain:" + b64e(secret))

    def load(self, cfg, key: str) -> Optional[bytes]:
        v = cfg.get(key) or ""
        return b64d(v[6:]) if v.startswith("plain:") else None

    def delete(self, cfg, key: str) -> None:
        cfg.set(key, "")


class KeychainStore:
    """macOS Keychain (through the security command, so access survives changes to Mirrorway's signature).
    Items saved by Köprü (service LEGACY_KEYCHAIN_SERVICE) are read when the new item is missing and copied over."""
    name = "Keychain"

    def __init__(self, runner=subprocess.run, service: str = KEYCHAIN_SERVICE,
                 legacy_service: Optional[str] = LEGACY_KEYCHAIN_SERVICE):
        self.run = runner
        self.service = service
        self.legacy_service = legacy_service

    def save(self, cfg, key: str, secret: bytes) -> None:
        r = self.run(["/usr/bin/security", "add-generic-password", "-U", "-s", self.service, "-a", key,
                      "-l", "Mirrorway device key", "-w", b64e(secret)], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise RuntimeError(f"Could not write to the Keychain: {(r.stderr or '').strip()}")
        cfg.set(key, "keychain:" + key)

    def _find(self, service: str, key: str):
        return self.run(["/usr/bin/security", "find-generic-password", "-s", service, "-a", key, "-w"],
                        capture_output=True, text=True, timeout=10)

    def load(self, cfg, key: str) -> Optional[bytes]:
        v = cfg.get(key) or ""
        if not v.startswith("keychain:"):
            return None
        r = self._find(self.service, key)
        if r.returncode != 0 and self.legacy_service:
            old = self._find(self.legacy_service, key)
            if old.returncode == 0:
                secret = b64d(old.stdout.strip())
                try:
                    self.save(cfg, key, secret)
                    log.info("Keychain item %s moved over from the old app", key)
                except Exception as exc:
                    log.warning("Could not copy the old Keychain item %s: %s", key, exc)
                return secret
        if r.returncode != 0:
            log.warning("Could not read from the Keychain: %s", (r.stderr or "").strip())
            return None
        return b64d(r.stdout.strip())

    def delete(self, cfg, key: str) -> None:
        self.run(["/usr/bin/security", "delete-generic-password", "-s", self.service, "-a", key],
                 capture_output=True, text=True, timeout=10)
        cfg.set(key, "")


class DPAPIStore:
    """Windows DPAPI: the encrypted form lives in the settings file; only the same Windows user can decrypt it.
    Blobs written by Köprü used a different entropy value; they are still decrypted and re-saved."""
    name = "DPAPI"

    def _blob(self, data: bytes):
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        buf = ctypes.create_string_buffer(data, len(data))
        return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf, DATA_BLOB

    def _call(self, fn_name: str, data: bytes, entropy: bytes = DPAPI_ENTROPY) -> bytes:
        import ctypes
        blob_in, _keep, DATA_BLOB = self._blob(data)
        blob_out = DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        fn = getattr(crypt32, fn_name)
        ent, _keep2, _ = self._blob(entropy)
        if fn_name == "CryptProtectData":
            ok = fn(ctypes.byref(blob_in), "Mirrorway", ctypes.byref(ent), None, None, 0x01, ctypes.byref(blob_out))
        else:
            ok = fn(ctypes.byref(blob_in), None, ctypes.byref(ent), None, None, 0x01, ctypes.byref(blob_out))
        if not ok:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)

    def save(self, cfg, key: str, secret: bytes) -> None:
        cfg.set(key, "dpapi:" + b64e(self._call("CryptProtectData", secret)))

    def load(self, cfg, key: str) -> Optional[bytes]:
        v = cfg.get(key) or ""
        if not v.startswith("dpapi:"):
            return None
        blob = b64d(v[6:])
        try:
            return self._call("CryptUnprotectData", blob)
        except Exception as exc:
            first = exc
        try:
            secret = self._call("CryptUnprotectData", blob, LEGACY_DPAPI_ENTROPY)
        except Exception:
            log.warning("Could not decrypt with DPAPI: %s", first)
            return None
        try:
            self.save(cfg, key, secret)
        except Exception:
            pass
        return secret

    def delete(self, cfg, key: str) -> None:
        cfg.set(key, "")


def default_store():
    if sys.platform == "darwin":
        return KeychainStore()
    if sys.platform == "win32":
        return DPAPIStore()
    return PlainStore()


SECRET_KEY = "device_key"


class KeyUnavailable(RuntimeError):
    """A key is on record but could not be read from the store (no new one is generated, so pairings stay intact)."""


def load_identity(cfg, store=None, create: bool = True) -> Optional[Identity]:
    """Loads the device identity; generates and stores one if there is none (create=True)."""
    store = store or default_store()
    marker = cfg.get(SECRET_KEY) or ""
    raw = None
    for st in (store, PlainStore()):
        try:
            raw = st.load(cfg, SECRET_KEY)
        except Exception as exc:
            log.warning("Could not read the device key (%s): %s", st.name, exc)
        if raw:
            return Identity.from_bytes(raw)
    if marker:
        raise KeyUnavailable(f"Could not read the device key ({marker.split(':', 1)[0]})")
    if not create:
        return None
    ident = Identity.generate()
    where = save_identity(cfg, ident, store)
    log.info("New device key generated (%s), fingerprint %s", where, ident.fingerprint)
    return ident


def save_identity(cfg, ident: Identity, store=None) -> str:
    store = store or default_store()
    try:
        store.save(cfg, SECRET_KEY, ident.private_bytes())
        if store.load(cfg, SECRET_KEY) != ident.private_bytes():      # make sure it reads back
            raise RuntimeError("the saved key could not be read back")
        return store.name
    except Exception as exc:
        log.warning("Could not store the device key in %s (%s); keeping it in the settings file", store.name, exc)
        PlainStore().save(cfg, SECRET_KEY, ident.private_bytes())
        return "file"
