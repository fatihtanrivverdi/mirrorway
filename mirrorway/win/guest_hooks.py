"""The Windows plumbing for using this PC's keyboard and mouse on the Mac (3.6), with plain ctypes.

One thread owns everything, with its own message loop:
  - a message-only window that receives raw mouse input (WM_INPUT, RIDEV_INPUTSINK): real movement even when the
    pointer cannot move further, so pushing against the edge is seen (input Windows itself injected, such as the
    Mac's mouse arriving through SendInput, has no device and is ignored);
  - the Ctrl+Alt+arrow hotkey towards the Mac (RegisterHotKey on that window);
  - while captured, the low-level keyboard and mouse hooks (WH_KEYBOARD_LL, WH_MOUSE_LL), which hand events to
    WinGuest and keep them from Windows; injected events pass (unless accept_injected, for CI);
  - a timer for WinGuest.tick.
The pointer is hidden by swapping the system cursors for a blank one (SetSystemCursor) and restored with
SPI_SETCURSORS; a flag in the settings restores them at the next start if Mirrorway stopped in between.
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import List, Optional

from ..geometry import Rect
from ..protocol import KEYFLAG_EXTENDED

log = logging.getLogger("mirrorway.guest")

WH_KEYBOARD_LL, WH_MOUSE_LL, HC_ACTION = 13, 14, 0
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN, WM_RBUTTONUP = 0x200, 0x201, 0x202, 0x204, 0x205
WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MOUSEWHEEL, WM_XBUTTONDOWN, WM_XBUTTONUP, WM_MOUSEHWHEEL = (0x207, 0x208, 0x20A,
                                                                                           0x20B, 0x20C, 0x20E)
WM_INPUT, WM_HOTKEY, WM_TIMER, WM_QUIT, WM_APP = 0x00FF, 0x0312, 0x0113, 0x0012, 0x8000
WM_CONFIG, WM_CAPTURE_ON, WM_CAPTURE_OFF = WM_APP + 1, WM_APP + 2, WM_APP + 3
LLKHF_EXTENDED, LLKHF_INJECTED = 0x01, 0x10
LLMHF_INJECTED = 0x01
RIDEV_INPUTSINK, RID_INPUT, RIM_TYPEMOUSE, MOUSE_MOVE_ABSOLUTE = 0x100, 0x10000003, 0, 0x01
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
HWND_MESSAGE = -3
SPI_SETCURSORS = 0x0057
OCR_IDS = (32512, 32513, 32514, 32515, 32516, 32642, 32643, 32644, 32645, 32646, 32648, 32649, 32650, 32651)
HOTKEY_ID = 0x4D57

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM) \
    if hasattr(ctypes, "WINFUNCTYPE") else None
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM) \
    if hasattr(ctypes, "WINFUNCTYPE") else None
BUTTONS = {WM_LBUTTONDOWN: (0, True), WM_LBUTTONUP: (0, False), WM_RBUTTONDOWN: (1, True),
           WM_RBUTTONUP: (1, False), WM_MBUTTONDOWN: (2, True), WM_MBUTTONUP: (2, False)}


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT), ("dwFlags", wintypes.DWORD),
                ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD), ("hDevice", wintypes.HANDLE),
                ("wParam", wintypes.WPARAM)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [("usFlags", wintypes.USHORT), ("ulButtons", wintypes.ULONG), ("ulRawButtons", wintypes.ULONG),
                ("lLastX", wintypes.LONG), ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


def _api():
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    u.SetWindowsHookExW.restype = wintypes.HHOOK
    u.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    u.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    u.CallNextHookEx.restype = LRESULT
    u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.DefWindowProcW.restype = LRESULT
    u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    u.RegisterClassW.restype = wintypes.ATOM
    u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                  wintypes.HINSTANCE, wintypes.LPVOID]
    u.CreateWindowExW.restype = wintypes.HWND
    u.DestroyWindow.argtypes = [wintypes.HWND]
    u.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]
    u.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID, ctypes.POINTER(wintypes.UINT),
                                  wintypes.UINT]
    u.GetRawInputData.restype = wintypes.UINT
    u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.DispatchMessageW.restype = LRESULT
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    u.SetTimer.restype = ctypes.c_size_t
    u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    u.CreateCursor.argtypes = [wintypes.HINSTANCE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_void_p, ctypes.c_void_p]
    u.CreateCursor.restype = wintypes.HANDLE
    u.CopyIcon.argtypes = [wintypes.HANDLE]
    u.CopyIcon.restype = wintypes.HANDLE
    u.SetSystemCursor.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    u.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT]
    k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    k.GetModuleHandleW.restype = wintypes.HMODULE
    return u, k


def restore_cursors(user32=None) -> bool:
    """Back to the user's own pointer scheme."""
    try:
        u = user32 or _api()[0]
        return bool(u.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0))
    except Exception:
        return False


class GuestHooks:
    """WinGuest's backend on Windows. start() runs the thread; set guest before starting."""

    def __init__(self, monitors_fn, cfg=None, accept_injected: bool = False):
        self.monitors_fn = monitors_fn
        self.cfg = cfg
        self.accept_injected = accept_injected
        self.guest = None
        self.hwnd = None
        self._tid = None
        self._hooks: List[int] = []
        self._procs = []                      # keep the callbacks alive
        self._hotkey_vk: Optional[int] = None
        self.hidden = False
        self.ready = threading.Event()
        self.error = ""
        self.raw_events = 0
        self.hook_events = 0
        self._u = self._k = None

    # -- the backend interface --
    def monitors(self) -> List[Rect]:
        return list(self.monitors_fn())

    def cursor_pos(self):
        pt = wintypes.POINT()
        self._u.GetCursorPos(ctypes.byref(pt))
        return (pt.x, pt.y)

    def set_cursor(self, x, y) -> None:
        self._u.SetCursorPos(int(x), int(y))

    def capture(self, on: bool) -> None:
        if self.hwnd:
            self._u.PostMessageW(self.hwnd, WM_CAPTURE_ON if on else WM_CAPTURE_OFF, 0, 0)

    def hide_cursor(self) -> None:
        u = self._u
        if self.hidden:
            return
        if self.cfg is not None:
            self.cfg.set("guest_cursor_hidden", True)
        inst = self._k.GetModuleHandleW(None)
        and_mask = (ctypes.c_ubyte * 128)(*([0xFF] * 128))
        xor_mask = (ctypes.c_ubyte * 128)()
        blank = u.CreateCursor(inst, 0, 0, 32, 32, and_mask, xor_mask)
        if not blank:
            return
        for ocr in OCR_IDS:
            u.SetSystemCursor(u.CopyIcon(blank), ocr)     # SetSystemCursor takes ownership of the copy
        self.hidden = True

    def show_cursor(self) -> None:
        if not self.hidden:
            return
        restore_cursors(self._u)
        self.hidden = False
        if self.cfg is not None:
            self.cfg.set("guest_cursor_hidden", False)

    def configure(self) -> None:
        """The edge towards the Mac changed: register Ctrl+Alt + that arrow."""
        if self.hwnd:
            self._u.PostMessageW(self.hwnd, WM_CONFIG, 0, 0)

    # -- the thread --
    def start(self) -> None:
        self._u, self._k = _api()
        threading.Thread(target=self._run, daemon=True, name="mirrorway-guest").start()
        self.ready.wait(5)

    def stop(self) -> None:
        if self._tid is not None:
            self._u.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)

    def _run(self) -> None:
        u, k = self._u, self._k
        try:
            self._tid = threading.get_native_id()
            inst = k.GetModuleHandleW(None)
            wndproc = WNDPROC(self._wndproc)
            self._procs.append(wndproc)
            wc = WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
            wc.hInstance = inst
            wc.lpszClassName = "MirrorwayGuest"
            u.RegisterClassW(ctypes.byref(wc))
            self.hwnd = u.CreateWindowExW(0, "MirrorwayGuest", "Mirrorway", 0, 0, 0, 0, 0, HWND_MESSAGE, None,
                                          inst, None)
            if not self.hwnd:
                raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
            rid = RAWINPUTDEVICE(1, 2, RIDEV_INPUTSINK, self.hwnd)
            if not u.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)):
                raise OSError(f"RegisterRawInputDevices failed ({ctypes.get_last_error()})")
            u.SetTimer(self.hwnd, 1, 250, None)
            self._register_hotkey()
        except Exception as exc:
            self.error = str(exc)
            log.warning("Using this PC's keyboard and mouse on the Mac is not available: %s", exc)
            self.ready.set()
            return
        self.ready.set()
        msg = wintypes.MSG()
        try:
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._unhook()
            self.show_cursor()
            if self._hotkey_vk is not None:
                u.UnregisterHotKey(self.hwnd, HOTKEY_ID)

    def _register_hotkey(self) -> None:
        u = self._u
        g = self.guest
        vk = g.toward_vk() if g is not None else None
        if vk == self._hotkey_vk:
            return
        if self._hotkey_vk is not None:
            u.UnregisterHotKey(self.hwnd, HOTKEY_ID)
            self._hotkey_vk = None
        if vk is not None:
            if u.RegisterHotKey(self.hwnd, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, vk):
                self._hotkey_vk = vk
            else:
                log.info("Ctrl+Alt+arrow is used by another program; the edge still works")

    def _hook(self) -> None:
        if self._hooks:
            return
        u, inst = self._u, self._k.GetModuleHandleW(None)
        mp, kp = HOOKPROC(self._mouse_proc), HOOKPROC(self._key_proc)
        self._procs += [mp, kp]
        hm = u.SetWindowsHookExW(WH_MOUSE_LL, mp, inst, 0)
        hk = u.SetWindowsHookExW(WH_KEYBOARD_LL, kp, inst, 0)
        self._hooks = [h for h in (hm, hk) if h]
        if len(self._hooks) != 2:
            log.warning("Could not install the keyboard and mouse hooks (%s)", ctypes.get_last_error())

    def _unhook(self) -> None:
        for h in self._hooks:
            try:
                self._u.UnhookWindowsHookEx(h)
            except Exception:
                pass
        self._hooks = []
        self._procs = self._procs[:1]                    # keep the window procedure

    # -- callbacks (this thread) --
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            g = self.guest
            if msg == WM_INPUT and g is not None:
                self._raw(lparam, g)
            elif msg == WM_HOTKEY and wparam == HOTKEY_ID and g is not None:
                g.hotkey()
            elif msg == WM_TIMER and g is not None:
                g.tick()
            elif msg == WM_CONFIG:
                self._register_hotkey()
            elif msg == WM_CAPTURE_ON:
                self._hook()
                return 0
            elif msg == WM_CAPTURE_OFF:
                self._unhook()
                return 0
        except Exception:
            log.exception("guest window")
        return self._u.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _raw(self, lparam, g) -> None:
        ri = RAWINPUT()
        size = wintypes.UINT(ctypes.sizeof(ri))
        n = self._u.GetRawInputData(lparam, RID_INPUT, ctypes.byref(ri), ctypes.byref(size),
                                    ctypes.sizeof(RAWINPUTHEADER))
        if n == 0xFFFFFFFF or ri.header.dwType != RIM_TYPEMOUSE:
            return
        if not ri.header.hDevice and not self.accept_injected:
            return                                       # injected (e.g. the Mac's mouse through SendInput)
        m = ri.mouse
        if m.usFlags & MOUSE_MOVE_ABSOLUTE:
            return                                       # tablets, remote desktop: no movement to push with
        self.raw_events += 1
        if m.lLastX or m.lLastY:
            g.on_raw(m.lLastX, m.lLastY)

    def _mouse_proc(self, code, wparam, lparam):
        try:
            g = self.guest
            if code == HC_ACTION and g is not None:
                info = MSLLHOOKSTRUCT.from_address(lparam)
                if not (info.flags & LLMHF_INJECTED) or self.accept_injected:
                    self.hook_events += 1
                    block = False
                    if wparam == WM_MOUSEMOVE:
                        block = g.on_move(info.pt.x, info.pt.y)
                    elif wparam in BUTTONS:
                        block = g.on_button(*BUTTONS[wparam])
                    elif wparam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                        block = g.on_button(3 if (info.mouseData >> 16) == 1 else 4, wparam == WM_XBUTTONDOWN)
                    elif wparam in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                        d = ctypes.c_short((info.mouseData >> 16) & 0xFFFF).value
                        block = g.on_wheel(d, 0) if wparam == WM_MOUSEWHEEL else g.on_wheel(0, d)
                    if block:
                        return 1
        except Exception:
            log.exception("mouse hook")
        return self._u.CallNextHookEx(None, code, wparam, lparam)

    def _key_proc(self, code, wparam, lparam):
        try:
            g = self.guest
            if code == HC_ACTION and g is not None:
                info = KBDLLHOOKSTRUCT.from_address(lparam)
                if not (info.flags & LLKHF_INJECTED) or self.accept_injected:
                    self.hook_events += 1
                    down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    flags = KEYFLAG_EXTENDED if info.flags & LLKHF_EXTENDED else 0
                    if g.on_key(down, flags, info.scanCode, info.vkCode):
                        return 1
        except Exception:
            log.exception("keyboard hook")
        return self._u.CallNextHookEx(None, code, wparam, lparam)
