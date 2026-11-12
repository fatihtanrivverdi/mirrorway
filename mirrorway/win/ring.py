"""The switch indicator on Windows: when the Mac's pointer arrives, a ring grows and fades around it.

A layered, click-through, topmost window (it never takes the focus) on a thread with its own message loop; the frames
come from mirrorway/ring.py (PIL, drawn once per screen scale) and go to the window with UpdateLayeredWindow, so the
ring has smooth, partly transparent edges. A new switch during the animation restarts it at the new place.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from typing import Callable, Dict, List, Optional, Tuple

from .. import ring as R

log = logging.getLogger("mirrorway.ring")

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST = 0x00080000, 0x00000020, 0x00000008
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 0x2, 0x0, 0x1
WM_APP, WM_QUIT = 0x8000, 0x0012
PM_NOREMOVE, PM_REMOVE = 0x0, 0x1
MONITOR_DEFAULTTONEAREST = 2
CLASS_NAME = "MirrorwayRing"

LRESULT = ctypes.c_ssize_t
WNDPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                                         wintypes.LPARAM)        # CFUNCTYPE: importable in tests


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


_api_cache: dict = {}


def _api():
    """Own WinDLL objects, so these argtypes do not change the ones other modules set."""
    if _api_cache:
        return _api_cache["u"], _api_cache["g"], _api_cache["k"]
    u = ctypes.WinDLL("user32", use_last_error=True)
    try:
        s = ctypes.WinDLL("shcore")                                    # Windows 8.1+
        s.GetDpiForMonitor.argtypes = [wintypes.HMONITOR, ctypes.c_int, ctypes.POINTER(wintypes.UINT),
                                       ctypes.POINTER(wintypes.UINT)]
        s.GetDpiForMonitor.restype = ctypes.c_long
        _api_cache["s"] = s
    except (OSError, AttributeError):
        _api_cache["s"] = None
    g = ctypes.WinDLL("gdi32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    H, P = wintypes.HANDLE, ctypes.c_void_p
    sig = {
        (u, "DefWindowProcW"): ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], LRESULT),
        (u, "RegisterClassW"): ([ctypes.POINTER(WNDCLASSW)], wintypes.ATOM),
        (u, "CreateWindowExW"): ([wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                  wintypes.HINSTANCE, wintypes.LPVOID], wintypes.HWND),
        (u, "DestroyWindow"): ([wintypes.HWND], wintypes.BOOL),
        (u, "ShowWindow"): ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        (u, "SetWindowPos"): ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               wintypes.UINT], wintypes.BOOL),
        (u, "UpdateLayeredWindow"): ([wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                      ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                      wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD],
                                     wintypes.BOOL),
        (u, "GetDC"): ([wintypes.HWND], wintypes.HDC),
        (u, "ReleaseDC"): ([wintypes.HWND, wintypes.HDC], ctypes.c_int),
        (u, "GetMessageW"): ([ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT],
                             wintypes.BOOL),
        (u, "PeekMessageW"): ([ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                               wintypes.UINT], wintypes.BOOL),
        (u, "TranslateMessage"): ([ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
        (u, "DispatchMessageW"): ([ctypes.POINTER(wintypes.MSG)], LRESULT),
        (u, "PostThreadMessageW"): ([wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
                                    wintypes.BOOL),
        (u, "PostQuitMessage"): ([ctypes.c_int], None),
        (u, "MonitorFromPoint"): ([wintypes.POINT, wintypes.DWORD], wintypes.HMONITOR),
        (g, "CreateCompatibleDC"): ([wintypes.HDC], wintypes.HDC),
        (g, "DeleteDC"): ([wintypes.HDC], wintypes.BOOL),
        (g, "CreateDIBSection"): ([wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT, ctypes.POINTER(P),
                                   H, wintypes.DWORD], wintypes.HBITMAP),
        (g, "SelectObject"): ([wintypes.HDC, H], H),
        (g, "DeleteObject"): ([H], wintypes.BOOL),
        (k, "GetModuleHandleW"): ([wintypes.LPCWSTR], wintypes.HMODULE),
    }
    for (dll, name), (args, res) in sig.items():
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, res
    _api_cache.update(u=u, g=g, k=k)
    return u, g, k


@WNDPROC
def _wndproc(hwnd, msg, wparam, lparam):                 # module level: never freed while a window uses it
    return _api()[0].DefWindowProcW(hwnd, msg, wparam, lparam)


def monitor_scale(x: int, y: int) -> float:
    """The scale (1.0 = 100%) of the monitor at a point, in physical pixels (Mirrorway is per-monitor DPI aware)."""
    try:
        u = _api()[0]
        hmon = u.MonitorFromPoint(wintypes.POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST)
        dx, dy = wintypes.UINT(), wintypes.UINT()
        s = _api_cache.get("s")
        if s is not None and s.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value:
            return dx.value / 96.0
    except Exception:
        pass
    return 1.0


class FrameCache:
    """The ring's frames per screen scale, as premultiplied BGRA bytes."""

    def __init__(self, render=R.render):
        self._render = render
        self._frames: Dict[float, Tuple[int, List[bytes]]] = {}

    def get(self, scale: float) -> Tuple[int, List[bytes]]:
        scale = round(scale * 4) / 4                      # 100%, 125%, 150%… share frames
        hit = self._frames.get(scale)
        if hit is None:
            n = R.frame_count()
            frames = [R.premultiplied_bgra(self._render(i / R.FPS, scale, 3)) for i in range(n)]
            hit = self._frames[scale] = (R.box(scale), frames)
        return hit


class RingIndicator:
    def __init__(self, enabled: Callable[[], bool] = lambda: True, scale_at: Callable = monitor_scale):
        self.enabled = enabled
        self.scale_at = scale_at
        self.cache = FrameCache()
        self._lock = threading.Lock()
        self._pending: Optional[Tuple[int, int]] = None
        self._thread: Optional[threading.Thread] = None
        self._tid: Optional[int] = None
        self._hwnd = None
        self.ready = threading.Event()
        self.shown = 0                                   # animations played (smoke test)
        self.frames = 0
        self.last_rect: Optional[Tuple[int, int, int]] = None      # left, top, size of the last ring

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="mirrorway-ring")
            self._thread.start()

    def show(self, x: float, y: float) -> None:
        """The pointer arrived at (x, y) (physical pixels); called from any thread."""
        if not self.enabled():
            return
        with self._lock:
            self._pending = (int(round(x)), int(round(y)))
        self.start()
        if self.ready.wait(3) and self._tid is not None:
            _api()[0].PostThreadMessageW(self._tid, WM_APP, 0, 0)

    def stop(self) -> None:
        if self._tid is not None:
            try:
                _api()[0].PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            except Exception:
                pass

    # ---- the ring thread -----------------------------------------------------------------------------
    def _run(self) -> None:
        try:
            u, g, k = _api()
            msg = wintypes.MSG()
            u.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)      # makes the thread's message queue
            self._tid = threading.get_native_id()
            hinst = k.GetModuleHandleW(None)
            wc = WNDCLASSW(lpfnWndProc=_wndproc, hInstance=hinst, lpszClassName=CLASS_NAME)
            u.RegisterClassW(ctypes.byref(wc))                               # fails harmlessly if already there
            self._hwnd = u.CreateWindowExW(WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
                                           | WS_EX_NOACTIVATE, CLASS_NAME, "Mirrorway", WS_POPUP,
                                           0, 0, 1, 1, None, None, hinst, None)
            if not self._hwnd:
                raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
        except Exception as exc:
            log.info("The switch ring is not available: %s", exc)
            self.ready.set()
            return
        self.ready.set()
        try:
            self.cache.get(self.scale_at(0, 0))          # the primary monitor's frames, before the first switch
        except Exception:
            log.exception("could not draw the switch ring")
        try:
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_APP and not msg.hWnd:
                    if not self._play():
                        break
                else:
                    u.TranslateMessage(ctypes.byref(msg))
                    u.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.exception("switch ring loop ended")
        finally:
            try:
                u.DestroyWindow(self._hwnd)
            except Exception:
                pass

    def _take(self) -> Optional[Tuple[int, int]]:
        with self._lock:
            req, self._pending = self._pending, None
        return req

    def _play(self) -> bool:
        """Plays the ring (again, at the newest place, if the pointer switched meanwhile). False: quit."""
        u, g, _k = _api()
        while True:
            req = self._take()
            if req is None:
                return True
            x, y = req
            size, frames = self.cache.get(self.scale_at(x, y))
            left, top = x - size // 2, y - size // 2
            self.last_rect = (left, top, size)
            screen = u.GetDC(None)
            mem = g.CreateCompatibleDC(screen)
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = size, -size          # top-down rows
            bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
            bits = ctypes.c_void_p()
            hbm = g.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            old = g.SelectObject(mem, hbm)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            pos, sz, src = wintypes.POINT(left, top), wintypes.SIZE(size, size), wintypes.POINT(0, 0)
            quit_ = again = False
            try:
                t0 = time.monotonic()
                for i, data in enumerate(frames):
                    ctypes.memmove(bits, data, len(data))
                    u.UpdateLayeredWindow(self._hwnd, screen, ctypes.byref(pos), ctypes.byref(sz), mem,
                                          ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
                    if i == 0:
                        u.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
                        u.SetWindowPos(self._hwnd, wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                                       SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                    self.frames += 1
                    quit_, again = self._pump()
                    if quit_ or again:
                        break
                    delay = t0 + (i + 1) / R.FPS - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
            finally:
                u.ShowWindow(self._hwnd, SW_HIDE)
                g.SelectObject(mem, old)
                g.DeleteObject(hbm)
                g.DeleteDC(mem)
                u.ReleaseDC(None, screen)
            self.shown += 1
            if quit_:
                return False

    def _pump(self) -> Tuple[bool, bool]:
        """Handles waiting messages during the animation: (quit, a new switch)."""
        u = _api()[0]
        msg = wintypes.MSG()
        again = False
        while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message == WM_QUIT:
                return True, again
            if msg.message == WM_APP and not msg.hWnd:
                again = True
                continue
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        return False, again
