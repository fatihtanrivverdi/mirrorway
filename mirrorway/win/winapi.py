"""Windows API (ctypes): SendInput, the pointer, monitors, DPI, clipboard sequence number."""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import List, Tuple

from ..geometry import Rect

log = logging.getLogger("mirrorway.winapi")

user32 = ctypes.WinDLL("user32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                     ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC,
                                       wintypes.LPARAM]
user32.EnumDisplayMonitors.restype = wintypes.BOOL


def set_dpi_aware() -> None:
    """To work in physical pixels (even with 125%/150% scaling)."""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def send_inputs(*inputs: INPUT) -> int:
    arr = (INPUT * len(inputs))(*inputs)
    n = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if n != len(inputs):
        log.debug("SendInput %d/%d (error %d)", n, len(inputs), ctypes.get_last_error())
    return n


def mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return inp


def key_input(vk: int, scan: int, flags: int) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return inp


def get_cursor_pos() -> Tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def virtual_screen() -> Rect:
    return Rect(user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
                user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def monitors() -> List[Rect]:
    rects: List[Rect] = []

    def _cb(hmon, hdc, lprect, lparam):
        r = lprect.contents
        rects.append(Rect(r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    # the primary monitor (the one containing 0,0) first
    rects.sort(key=lambda r: 0 if r.contains(0, 0) else 1)
    return rects or [virtual_screen()]


def move_absolute(x: float, y: float) -> None:
    vs = virtual_screen()
    # Normalise to pixel centres (0..65535); no rounding error
    nx = int(((x - vs.x) + 0.5) * 65536.0 / max(vs.w, 1))
    ny = int(((y - vs.y) + 0.5) * 65536.0 / max(vs.h, 1))
    nx = min(max(nx, 0), 65535)
    ny = min(max(ny, 0), 65535)
    send_inputs(mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny))


def clipboard_sequence() -> int:
    return int(user32.GetClipboardSequenceNumber())


def map_vk_to_scan(vk: int) -> int:
    return int(user32.MapVirtualKeyW(vk, 0))
