"""Starting Mirrorway with Windows: the Mirrorway shortcut in the user's Startup folder (the installer makes it)."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("mirrorway.startup")

NAME = "Mirrorway.lnk"


def _folder(kind: str) -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    base = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return base / "Startup" if kind == "startup" else base


def link() -> Optional[Path]:
    d = _folder("startup")
    return d / NAME if d else None


def available() -> bool:
    return sys.platform == "win32" and link() is not None


def is_enabled() -> bool:
    p = link()
    return bool(p and p.exists())


def disable() -> None:
    p = link()
    if p and p.exists():
        p.unlink()
        log.info("Mirrorway no longer starts with Windows")


def enable(install_root: Path) -> None:
    """Copy the Start menu shortcut, or make a new one (pythonw -m mirrorway.win in the install folder)."""
    p = link()
    if p is None:
        raise OSError("APPDATA is not set")
    p.parent.mkdir(parents=True, exist_ok=True)
    menu = _folder("programs")
    if menu is not None and (menu / NAME).exists():
        shutil.copyfile(menu / NAME, p)
    else:
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
        script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:MW_LINK); "
                  "$s.TargetPath = $env:MW_EXE; $s.Arguments = '-m mirrorway.win'; "
                  "$s.WorkingDirectory = $env:MW_DIR; $s.Save()")
        env = dict(os.environ, MW_LINK=str(p), MW_EXE=str(pyw), MW_DIR=str(install_root))
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env, check=True,
                       timeout=30, creationflags=0x08000000)          # CREATE_NO_WINDOW
    log.info("Mirrorway starts with Windows")
