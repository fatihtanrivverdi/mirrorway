"""The Mac's screen lock, for "Lock both computers together": following it (the system's distributed notifications
com.apple.screenIsLocked / screenIsUnlocked, delivered on the main thread) and locking the Mac when Windows locks.

Locking uses SACLockScreenImmediate from the system's login framework (what the Lock Screen menu item does); if that
is missing the displays are put to sleep, which locks the Mac when it asks for the password right after sleep.
"""
from __future__ import annotations

import ctypes
import logging
import subprocess
from typing import Callable, Optional

from Foundation import NSDistributedNotificationCenter, NSObject

log = logging.getLogger("mirrorway.lock")

LOCKED, UNLOCKED = "com.apple.screenIsLocked", "com.apple.screenIsUnlocked"
LOGIN_FRAMEWORK = "/System/Library/PrivateFrameworks/login.framework/Versions/Current/login"


class MirrorwayLockObserver(NSObject):
    """Receives the lock notifications; mw_changed(locked) is a Python callable. Errors are logged here."""

    def screenLocked_(self, _note):
        _call(self.mw_changed, True)

    def screenUnlocked_(self, _note):
        _call(self.mw_changed, False)


def _call(fn, *args) -> None:
    if fn is None:
        return
    try:
        fn(*args)
    except Exception:
        log.exception("lock callback failed")


def watch(on_change: Callable[[bool], None]):
    """Calls on_change(True) when the Mac locks and on_change(False) when it unlocks; keep the returned observer."""
    obs = MirrorwayLockObserver.alloc().init()
    obs.mw_changed = on_change
    center = NSDistributedNotificationCenter.defaultCenter()
    center.addObserver_selector_name_object_(obs, "screenLocked:", LOCKED, None)
    center.addObserver_selector_name_object_(obs, "screenUnlocked:", UNLOCKED, None)
    return obs


def _sac_lock() -> Optional[Callable[[], int]]:
    try:
        fn = ctypes.CDLL(LOGIN_FRAMEWORK).SACLockScreenImmediate
    except (OSError, AttributeError):
        return None
    fn.restype = ctypes.c_int
    fn.argtypes = []
    return fn


def can_lock() -> str:
    """How lock_mac() would lock: "login" (the system's lock) or "display sleep"."""
    return "login" if _sac_lock() is not None else "display sleep"


def lock_mac() -> str:
    """Locks the Mac now; returns the way it was done."""
    fn = _sac_lock()
    if fn is not None:
        try:
            if fn() == 0:
                return "login"
        except Exception:
            log.exception("SACLockScreenImmediate failed")
    subprocess.run(["/usr/bin/pmset", "displaysleepnow"], check=False, timeout=10)
    return "display sleep"
