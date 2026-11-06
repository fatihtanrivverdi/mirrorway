"""Applies a Windows update that came from the Mac.

Order: verify the hash -> extract into a temporary folder -> (if needed) install new packages with pip ->
test the new code by importing it in a separate process -> replace the mirrorway folder (the old one is backed up).
If any step fails, the installed version is left untouched.
"""
from __future__ import annotations

import hashlib
import io
import logging
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("mirrorway.updater")

CREATE_NO_WINDOW = 0x08000000
STAGE_DIR = "_update"
BACKUP_DIR = "mirrorway.old"
REQ_FILE = "requirements-windows.txt"


class UpdateError(Exception):
    pass


def _run(args, timeout: float):
    kw = {"capture_output": True, "text": True, "timeout": timeout}
    if sys.platform == "win32":
        kw["creationflags"] = CREATE_NO_WINDOW
    return subprocess.run(args, **kw)


def _safe_extract(data: bytes, dest: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            name = info.filename
            p = Path(name)
            if p.is_absolute() or ".." in p.parts or name.startswith(("/", "\\")):
                raise UpdateError(f"Invalid file path: {name}")
            if not (name.startswith("mirrorway/") or name in (REQ_FILE, "CHANGELOG.md")):
                raise UpdateError(f"Unexpected file: {name}")
        z.extractall(dest)


def apply_update(meta: dict, data: bytes, install_root: Path, python_exe: Optional[str] = None,
                 runner: Callable = _run) -> str:
    """Applies the update; returns the new version number. On error UpdateError."""
    if python_exe is None:
        # python.exe from the same folder instead of pythonw.exe (so its output can be captured; no window opens)
        exe = Path(sys.executable)
        console = exe.with_name("python.exe")
        python_exe = str(console if exe.name.lower() == "pythonw.exe" and console.exists() else exe)
    install_root = Path(install_root)
    version = str(meta.get("version", "?"))
    if hashlib.sha256(data).hexdigest() != meta.get("sha256"):
        raise UpdateError("Package hash does not match")
    if not (install_root / "mirrorway").is_dir():
        raise UpdateError(f"Installed code not found: {install_root}")

    stage = install_root / STAGE_DIR
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    try:
        _safe_extract(data, stage)
        if not (stage / "mirrorway" / "win" / "app.py").exists():
            raise UpdateError("The package has no Windows app")

        # 1) Install the dependencies if they changed
        new_req = stage / REQ_FILE
        old_req = install_root / REQ_FILE
        if new_req.exists() and (not old_req.exists() or new_req.read_bytes() != old_req.read_bytes()):
            log.info("Installing new dependencies")
            r = runner([python_exe, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                        "-r", str(new_req)], timeout=600)
            if r.returncode != 0:
                raise UpdateError("pip install failed: " + (r.stderr or r.stdout or "")[-400:])

        # 2) Can the new code be imported?
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); "
                 "import mirrorway, mirrorway.win.client, mirrorway.win.remote, mirrorway.win.updater; "
                 "print(mirrorway.__version__)")
        r = runner([python_exe, "-I", "-c", probe, str(stage)], timeout=120)
        if r.returncode != 0:
            raise UpdateError("The new version failed its test import: " + (r.stderr or "")[-400:])
        got = (r.stdout or "").strip().splitlines()[-1:] or [""]
        if got[0] != version:
            raise UpdateError(f"Version mismatch: package {version}, code {got[0]}")

        # 3) Swap (the old version stays as a backup)
        backup = install_root / BACKUP_DIR
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        (install_root / "mirrorway").rename(backup)
        try:
            (stage / "mirrorway").rename(install_root / "mirrorway")
        except Exception:
            backup.rename(install_root / "mirrorway")
            raise
        for name in (REQ_FILE, "CHANGELOG.md"):
            if (stage / name).exists():
                shutil.copyfile(stage / name, install_root / name)
        log.info("Update applied: %s", version)
        return version
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def restart_command() -> list:
    """Command that restarts Mirrorway with the same interpreter and arguments."""
    return [sys.executable, "-m", "mirrorway.win"] + sys.argv[1:]
