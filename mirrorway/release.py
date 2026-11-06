"""Signed release packages: creation (CI) and verification (the updater on the Mac).

A release consists of three files (on GitHub Releases):
  mirrorway-app-<version>.zip      application code (the contents of ~/.mirrorway/app)
  mirrorway-<version>.json         manifest: version, file name, size, SHA-256
  mirrorway-<version>.json.sig     Ed25519 signature of the manifest (base64)

The signing key exists only in CI (a GitHub secret) and in a backup on the owner's Mac. The public key
is embedded in the code below; the updater only installs a package that is signed with one of these keys,
newer than the running version and matches its hash. To change the key, the new public key is first shipped
in a release signed with the old key (the list can hold more than one key).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

PUBLIC_KEYS = [
    "GaZiNKbVWHNMFALljh5/Is6plXiazgN7cy7OdaCkBLE=",       # 2026-11-05
]
MANIFEST_FORMAT = 1
APP_EXTRA_FILES = ("requirements-mac.txt", "requirements-windows.txt", "CHANGELOG.md")
_FIXED_DATE = (2020, 1, 1, 0, 0, 0)
MAX_ZIP_BYTES = 20 * 1024 * 1024


class ReleaseError(Exception):
    pass


def zip_name(version: str) -> str:
    return f"mirrorway-app-{version}.zip"


def manifest_name(version: str) -> str:
    return f"mirrorway-{version}.json"


def sig_name(version: str) -> str:
    return manifest_name(version) + ".sig"


def app_files(root: Path) -> List[Tuple[str, Path]]:
    """Files that go into the app package (sorted): the .py files under mirrorway/ and the extra files."""
    root = Path(root)
    out = []
    for p in sorted((root / "mirrorway").rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo"):
            continue
        out.append((p.relative_to(root).as_posix(), p))
    for name in APP_EXTRA_FILES:
        p = root / name
        if p.exists():
            out.append((name, p))
    return out


def build_app_zip(root: Path) -> bytes:
    """Same content and same zlib -> same bytes (fixed dates and order). Different zlib versions may compress
    differently; to compare content, look at the extracted files."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, p in app_files(root):
            zi = zipfile.ZipInfo(arc, date_time=_FIXED_DATE)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, p.read_bytes())
    return buf.getvalue()


def version_in_zip(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        text = z.read("mirrorway/__init__.py").decode("utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise ReleaseError("no version information in the package")


def make_manifest(version: str, data: bytes, notes: str = "", created: Optional[int] = None) -> bytes:
    m = {"format": MANIFEST_FORMAT, "app": "mirrorway", "version": version, "file": zip_name(version),
         "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
         "created": int(created if created is not None else time.time()), "notes": notes[:4000]}
    return json.dumps(m, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sign(manifest: bytes, private_key_b64: str) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw = base64.b64decode(private_key_b64.strip())
    key = Ed25519PrivateKey.from_private_bytes(raw)
    return base64.b64encode(key.sign(manifest)).decode()


def public_key_of(private_key_b64: str) -> str:
    from cryptography.hazmat.primitives import serialization as S
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64.strip()))
    return base64.b64encode(key.public_key().public_bytes(S.Encoding.Raw, S.PublicFormat.Raw)).decode()


def verify_manifest(manifest: bytes, sig_b64: str, keys: Iterable[str] = None) -> Dict:
    """Returns the manifest if the signature matches one of the known public keys; otherwise ReleaseError."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        sig = base64.b64decode(sig_b64.strip(), validate=True)
    except Exception:
        raise ReleaseError("corrupt signature file")
    for k in (PUBLIC_KEYS if keys is None else list(keys)):
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(k)).verify(sig, manifest)
        except InvalidSignature:
            continue
        try:
            m = json.loads(manifest.decode("utf-8"))
        except Exception:
            raise ReleaseError("could not read the manifest")
        if m.get("format") != MANIFEST_FORMAT or m.get("app") != "mirrorway":
            raise ReleaseError("unknown manifest format")
        return m
    raise ReleaseError("signature could not be verified (the package is not signed with Mirrorway's signing key)")


def verify_zip(manifest: Dict, data: bytes) -> None:
    if len(data) != manifest.get("size"):
        raise ReleaseError("package size does not match the manifest")
    if hashlib.sha256(data).hexdigest() != manifest.get("sha256"):
        raise ReleaseError("package hash does not match the manifest")
    got = version_in_zip(data)
    if got != manifest.get("version"):
        raise ReleaseError(f"the version in the package ({got}) does not match the manifest ({manifest.get('version')})")


def changelog_section(text: str, version: str) -> str:
    """The items of one version from CHANGELOG.md (without the heading)."""
    out, on = [], False
    for line in (text or "").splitlines():
        if line.startswith("## "):
            if on:
                break
            on = line[3:].split()[0:1] == [version]
            continue
        if on:
            out.append(line)
    return "\n".join(out).strip()
