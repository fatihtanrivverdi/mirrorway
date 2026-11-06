"""Windows update package: the Mac zips the files Windows needs from its own code folder and sends them over
the encrypted channel (platform independent, testable)."""
from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Optional, Tuple

import mirrorway

from . import __version__

_EXTRA_FILES = ("requirements-windows.txt", "CHANGELOG.md")
_FIXED_DATE = (2020, 1, 1, 0, 0, 0)      # same content -> same zip (same hash)

_cache: dict = {}


def package_root() -> Path:
    """The folder that contains the mirrorway package (~/.mirrorway/app or the project root)."""
    return Path(mirrorway.__file__).resolve().parent.parent


def build_windows_bundle(root: Optional[Path] = None) -> Tuple[str, bytes, str]:
    """Returns (version, zip bytes, sha256). mirrorway/mac and __pycache__ are left out."""
    root = Path(root) if root else package_root()
    key = (str(root), __version__)
    if key in _cache:
        return _cache[key]
    pkg = root / "mirrorway"
    files = []
    # the code, plus the icons (the Windows shortcuts point at mirrorway/assets/mirrorway.ico)
    for p in sorted(list(pkg.rglob("*.py")) + list((pkg / "assets").glob("*.ico"))):
        rel = p.relative_to(root)
        parts = rel.parts
        if "__pycache__" in parts or (len(parts) > 1 and parts[1] == "mac"):
            continue
        files.append((rel.as_posix(), p))
    for name in _EXTRA_FILES:
        p = root / name
        if p.exists():
            files.append((name, p))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, p in files:
            zi = zipfile.ZipInfo(arc, date_time=_FIXED_DATE)
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, p.read_bytes())
    data = buf.getvalue()
    result = (__version__, data, hashlib.sha256(data).hexdigest())
    _cache[key] = result
    return result
