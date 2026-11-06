"""'Open at login': writes a small LaunchAgent under ~/Library/LaunchAgents.

The agent opens the app with `open`, so Mirrorway still runs as Mirrorway.app and the permissions
stay with the app. The file only takes effect at the next login.
"""
from __future__ import annotations

import os
import plistlib
from pathlib import Path
from typing import Optional

LABEL = "app.mirrorway.mac"
LEGACY_LABEL = "app.kopru.mac"          # Köprü 1.x–2.x


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def bundle_path() -> Optional[str]:
    """Path of the app bundle if we are running from Mirrorway.app."""
    if os.environ.get("MIRRORWAY_APP") == "1":
        b = os.environ.get("MIRRORWAY_BUNDLE")
        if b and b.endswith(".app"):
            return b
    return None


def is_enabled() -> bool:
    return plist_path().exists()


def enable(bundle: str) -> None:
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/open", "-g", bundle],
        "RunAtLoad": True,
        "LimitLoadToSessionType": "Aqua",
    }
    with open(p, "wb") as fh:
        plistlib.dump(data, fh)


def disable() -> None:
    try:
        plist_path().unlink()
    except FileNotFoundError:
        pass


def replace_legacy(bundle: Optional[str]) -> bool:
    """Köprü's login item is removed; if it was on, Mirrorway's is turned on instead. True if one was found."""
    old = plist_path().parent / f"{LEGACY_LABEL}.plist"
    if not old.exists():
        return False
    try:
        old.unlink()
    except OSError:
        return False
    if bundle and not is_enabled():
        enable(bundle)
    return True
