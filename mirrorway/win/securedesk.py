"""Windows' input desktop: the one that gets the keyboard and mouse right now.

Normally that is the user's "Default" desktop. The lock screen, the Ctrl+Alt+Del screen and the UAC prompt run on
the "Winlogon" desktop (a secure screen saver on "Screen-saver"). Only a few accounts, LocalSystem among them, may
open Winlogon, and input reaches a desktop only from a thread attached to it; so the lock screen helper runs as
SYSTEM and attaches its input thread here with SetThreadDesktop (the thread must have no windows or hooks).
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional

DESKTOP_READOBJECTS = 0x0001
GENERIC_ALL = 0x10000000
UOI_NAME = 2
DEFAULT = "default"

_dll: dict = {}


def _user32():
    u = _dll.get("u")
    if u is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        u.OpenInputDesktop.restype = wintypes.HANDLE
        u.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
                                                ctypes.POINTER(wintypes.DWORD)]
        u.GetUserObjectInformationW.restype = wintypes.BOOL
        u.SetThreadDesktop.argtypes = [wintypes.HANDLE]
        u.SetThreadDesktop.restype = wintypes.BOOL
        u.CloseDesktop.argtypes = [wintypes.HANDLE]
        u.CloseDesktop.restype = wintypes.BOOL
        _dll["u"] = u
    return u


def desktop_name(h) -> str:
    buf = ctypes.create_unicode_buffer(256)
    need = wintypes.DWORD()
    if not _user32().GetUserObjectInformationW(h, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(need)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buf.value


def input_desktop() -> Optional[str]:
    """The input desktop's name; None if it cannot be opened (a normal user process cannot open Winlogon: for it,
    None means a secure desktop is up)."""
    u = _user32()
    h = u.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not h:
        return None
    try:
        return desktop_name(h)
    finally:
        u.CloseDesktop(h)


def is_secure(name: Optional[str]) -> bool:
    """Lock screen, Ctrl+Alt+Del, UAC prompt or a secure screen saver: anything but the user's desktop."""
    return name is None or name.lower() != DEFAULT


class ThreadDesktop:
    """Keeps the calling thread attached to the input desktop (call follow() on the thread that sends input)."""

    def __init__(self):
        self.name: Optional[str] = None
        self._h = None

    def follow(self) -> str:
        u = _user32()
        h = u.OpenInputDesktop(0, False, GENERIC_ALL)
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            name = desktop_name(h)
        except Exception:
            u.CloseDesktop(h)
            raise
        if name == self.name:
            u.CloseDesktop(h)
            return name
        if not u.SetThreadDesktop(h):
            err = ctypes.get_last_error()
            u.CloseDesktop(h)
            raise ctypes.WinError(err)
        old, self._h, self.name = self._h, h, name
        if old:
            u.CloseDesktop(old)
        return name
