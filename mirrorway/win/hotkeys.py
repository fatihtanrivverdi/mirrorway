"""Global shortcuts on Windows (RegisterHotKey), Control + Alt + a letter, on a thread with its own message loop.

A shortcut another app already uses cannot be registered; it is skipped and logged. On_hotkey(action) is called
on the shortcut thread and must hand the work to the interface thread.
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable, Dict, List, Optional

log = logging.getLogger("mirrorway.hotkeys")

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012


def vk_of(letter: str) -> int:
    """The virtual key code of a letter (VK_A..VK_Z are the ASCII codes of A..Z)."""
    return ord(letter.upper())


class Hotkeys:
    def __init__(self, bindings: Dict[str, str], on_hotkey: Callable[[str], None], user32=None):
        """bindings: {action: letter}; an empty letter is skipped."""
        self.bindings = {a: letter for a, letter in bindings.items() if letter}
        self.on_hotkey = on_hotkey
        self._user32 = user32
        self._thread: Optional[threading.Thread] = None
        self._tid: Optional[int] = None
        self.registered: List[str] = []
        self.failed: List[str] = []
        self.ready = threading.Event()

    def _api(self):
        if self._user32 is None:
            u = ctypes.WinDLL("user32", use_last_error=True)
            u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            u.RegisterHotKey.restype = wintypes.BOOL
            u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            u.UnregisterHotKey.restype = wintypes.BOOL
            u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
            u.GetMessageW.restype = wintypes.BOOL
            u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            u.PostThreadMessageW.restype = wintypes.BOOL
            self._user32 = u
        return self._user32

    def start(self) -> None:
        if not self.bindings or self._thread is not None:
            self.ready.set()
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="mirrorway-hotkeys")
        self._thread.start()

    def stop(self) -> None:
        t, tid = self._thread, self._tid
        if t is None:
            return
        self._thread = None
        if tid is not None:
            try:
                self._api().PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass

    def _run(self) -> None:
        ids: Dict[int, str] = {}
        try:
            u = self._api()
            self._tid = threading.get_native_id()
            for i, (action, letter) in enumerate(sorted(self.bindings.items()), start=1):
                if u.RegisterHotKey(None, i, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, vk_of(letter)):
                    ids[i] = action
                    self.registered.append(action)
                else:
                    self.failed.append(action)
                    log.info("The shortcut Ctrl+Alt+%s is used by another app; it does not work in Mirrorway",
                             letter.upper())
            if ids:
                log.info("Shortcuts: %s", ", ".join(f"Ctrl+Alt+{self.bindings[a].upper()} ({a})"
                                                    for a in ids.values()))
        except Exception as exc:
            log.info("Shortcuts are not available: %s", exc)
            self.ready.set()
            return
        self.ready.set()
        msg = wintypes.MSG()
        try:
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    action = ids.get(int(msg.wParam))
                    if action is not None:
                        try:
                            self.on_hotkey(action)
                        except Exception:
                            log.exception("shortcut %s failed", action)
        except Exception as exc:
            log.info("Shortcut loop ended: %s", exc)
        finally:
            for i in ids:
                try:
                    u.UnregisterHotKey(None, i)
                except Exception:
                    pass
