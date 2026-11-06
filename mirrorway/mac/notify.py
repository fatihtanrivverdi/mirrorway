"""macOS notifications.

rumps.notification (NSUserNotification) silently shows nothing on newer macOS versions when called from a Python
process without an app bundle identity. osascript's "display notification" works from any process; the notification
appears under the name "Script Editor" (macOS may ask for permission the first time).
"""
from __future__ import annotations

import logging
import subprocess

log = logging.getLogger("mirrorway.app")

OSASCRIPT = "/usr/bin/osascript"


def quote(s: str) -> str:
    """An AppleScript string literal."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ") + '"'


def script(title: str, message: str, subtitle: str = "") -> str:
    s = f"display notification {quote(message)} with title {quote(title)}"
    if subtitle:
        s += f" subtitle {quote(subtitle)}"
    return s


def notify(title: str, message: str, subtitle: str = "", popen=subprocess.Popen) -> bool:
    try:
        popen([OSASCRIPT, "-e", script(title, message, subtitle)],
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("Notification: %s — %s", subtitle or title, message)
        return True
    except Exception as exc:
        log.warning("Could not show the notification: %s", exc)
        return False
