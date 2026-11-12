"""Windows' lock and unlock, for "Lock both computers together": a hidden window on its own thread registered with
WTSRegisterSessionNotification gets WM_WTSSESSION_CHANGE when this session is locked (Windows + L, idle, Ctrl+Alt+Del
› Lock) or unlocked. on_change(locked) is called on that thread.
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable, Dict, Optional

from .ring import WNDCLASSW, WNDPROC, _api

log = logging.getLogger("mirrorway.session")

WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK, WTS_SESSION_UNLOCK = 0x7, 0x8
NOTIFY_FOR_THIS_SESSION = 0
WS_POPUP, WS_EX_TOOLWINDOW = 0x80000000, 0x00000080
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0
CLASS_NAME = "MirrorwaySession"

_watchers: Dict[int, "SessionWatcher"] = {}


def event_of(wparam: int) -> Optional[bool]:
    """True for a lock, False for an unlock, None for other session changes (logon, remote connect…)."""
    return {WTS_SESSION_LOCK: True, WTS_SESSION_UNLOCK: False}.get(int(wparam))


@WNDPROC
def _wndproc(hwnd, msg, wparam, lparam):                 # module level: never freed while a window uses it
    if msg == WM_WTSSESSION_CHANGE:
        w = _watchers.get(int(hwnd or 0))
        locked = event_of(wparam)
        if w is not None and locked is not None:
            w._changed(locked)
    return _api()[0].DefWindowProcW(hwnd, msg, wparam, lparam)


class SessionWatcher:
    def __init__(self, on_change: Callable[[bool], None]):
        self.on_change = on_change
        self.locked: Optional[bool] = None
        self.registered = False
        self.ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tid: Optional[int] = None
        self._hwnd = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="mirrorway-session")
            self._thread.start()

    def stop(self) -> None:
        if self._tid is not None:
            try:
                _api()[0].PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            except Exception:
                pass

    def _changed(self, locked: bool) -> None:
        if locked == self.locked:
            return
        self.locked = locked
        log.info("Windows was %s", "locked" if locked else "unlocked")
        try:
            self.on_change(locked)
        except Exception:
            log.exception("lock change handler failed")

    def _run(self) -> None:
        wts = None
        try:
            u, _g, k = _api()
            msg = wintypes.MSG()
            u.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
            self._tid = threading.get_native_id()
            hinst = k.GetModuleHandleW(None)
            wc = WNDCLASSW(lpfnWndProc=_wndproc, hInstance=hinst, lpszClassName=CLASS_NAME)
            u.RegisterClassW(ctypes.byref(wc))
            self._hwnd = u.CreateWindowExW(WS_EX_TOOLWINDOW, CLASS_NAME, "Mirrorway", WS_POPUP, 0, 0, 0, 0,
                                           None, None, hinst, None)
            if not self._hwnd:
                raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
            _watchers[int(self._hwnd)] = self
            wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
            wts.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
            wts.WTSRegisterSessionNotification.restype = wintypes.BOOL
            wts.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
            wts.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
            if not wts.WTSRegisterSessionNotification(self._hwnd, NOTIFY_FOR_THIS_SESSION):
                raise OSError(f"WTSRegisterSessionNotification failed ({ctypes.get_last_error()})")
            self.registered = True
        except Exception as exc:
            log.info("Windows' lock is not followed: %s", exc)
            self.ready.set()
            return
        self.ready.set()
        try:
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.exception("session watcher ended")
        finally:
            try:
                wts.WTSUnRegisterSessionNotification(self._hwnd)
                _watchers.pop(int(self._hwnd), None)
                u.DestroyWindow(self._hwnd)
            except Exception:
                pass
