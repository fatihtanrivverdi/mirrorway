"""Diagnostics bundle: logs, settings with secrets removed, versions and the current status in one zip.

Sharing this file is enough when reporting a problem; it contains no pairing code or secret key.
"""
from __future__ import annotations

import json
import platform
import sys
import time
import zipfile
from pathlib import Path
from typing import Iterable, Optional

from . import __version__

SECRET_KEYS = {"pair_code", "pairing_code", "device_key", "update_token"}
SECRET_PREFIXES = ("plain:", "dpapi:")


def redact(obj):
    """Removes secret values from a settings dict (also inside nested structures)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in SECRET_KEYS and v:
                out[k] = "<secret, not included>"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str) and obj.startswith(SECRET_PREFIXES):
        return "<secret, not included>"
    return obj


def versions() -> dict:
    pkgs = {}
    try:
        from importlib import metadata
        for d in metadata.distributions():
            name = d.metadata.get("Name") or ""
            if name:
                pkgs[name.lower()] = d.version
    except Exception:
        pass
    return {"mirrorway": __version__, "python": sys.version.split()[0], "platform": platform.platform(),
            "machine": platform.machine(), "packages": dict(sorted(pkgs.items()))}


def _jsonable(obj):
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        if isinstance(obj, dict):
            return {str(k): _jsonable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_jsonable(v) for v in obj]
        return repr(obj)


def make_bundle(out_path: Path, logs: Iterable[Path], settings: dict, status: Optional[dict] = None,
                extra: Optional[dict] = None) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in logs:
            p = Path(p)
            if p.exists() and p.is_file():
                z.write(p, arcname=f"logs/{p.name}")
        z.writestr("settings.json", json.dumps(redact(settings), indent=2, ensure_ascii=False))
        info = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "versions": versions()}
        if status is not None:
            info["status"] = _jsonable(redact(status))
        if extra:
            info.update(_jsonable(redact(extra)))
        z.writestr("diagnostics.json", json.dumps(info, indent=2, ensure_ascii=False))
        z.writestr("README.txt", "Mirrorway diagnostics bundle. Secrets (pairing codes, device keys) "
                                 "are never included.\n")
    return out_path


def default_output_dir() -> Path:
    home = Path.home()
    for cand in (home / "Desktop", home):
        if cand.is_dir():
            return cand
    return home


def bundle_name(side: str) -> str:
    return f"Mirrorway-diagnostics-{side}-{time.strftime('%Y%m%d-%H%M%S')}.zip"


def log_files(directory: Path, stem: str) -> list:
    d = Path(directory)
    return sorted(d.glob(f"{stem}.log*")) + [p for p in (d / "install.log",) if p.exists()]
