"""The Windows clipboard (Win32, ctypes): text, HTML, RTF and images.

Formats: CF_UNICODETEXT, "HTML Format" (CF_HTML), "Rich Text Format", "PNG" and CF_DIB.
When writing an image, both "PNG" (keeps transparency; Chrome, Office, Paint.NET read it) and CF_DIB (every app
reads it) are set. When reading, "PNG" comes first; otherwise CF_DIB/CF_DIBV5 is taken and converted to PNG.
"""
from __future__ import annotations

import ctypes
import logging
import struct
import time
from ctypes import wintypes
from typing import Dict, Optional

from .. import richclip as RC

log = logging.getLogger("mirrorway.clipboard")

CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIB = 8
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002
HWND_MESSAGE = -3


class Win32Clipboard:
    """A thin ctypes layer (tests swap it for a fake object with the same interface)."""

    def __init__(self):
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        H, UINT, BOOL = wintypes.HANDLE, wintypes.UINT, wintypes.BOOL
        u.OpenClipboard.argtypes, u.OpenClipboard.restype = [wintypes.HWND], BOOL
        u.CloseClipboard.argtypes, u.CloseClipboard.restype = [], BOOL
        u.EmptyClipboard.argtypes, u.EmptyClipboard.restype = [], BOOL
        u.GetClipboardData.argtypes, u.GetClipboardData.restype = [UINT], H
        u.SetClipboardData.argtypes, u.SetClipboardData.restype = [UINT, H], H
        u.IsClipboardFormatAvailable.argtypes, u.IsClipboardFormatAvailable.restype = [UINT], BOOL
        u.RegisterClipboardFormatW.argtypes, u.RegisterClipboardFormatW.restype = [wintypes.LPCWSTR], UINT
        u.GetClipboardSequenceNumber.argtypes, u.GetClipboardSequenceNumber.restype = [], wintypes.DWORD
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        u.CreateWindowExW.restype = wintypes.HWND
        k.GlobalAlloc.argtypes, k.GlobalAlloc.restype = [UINT, ctypes.c_size_t], H
        k.GlobalLock.argtypes, k.GlobalLock.restype = [H], ctypes.c_void_p
        k.GlobalUnlock.argtypes, k.GlobalUnlock.restype = [H], BOOL
        k.GlobalSize.argtypes, k.GlobalSize.restype = [H], ctypes.c_size_t
        k.GlobalFree.argtypes, k.GlobalFree.restype = [H], H
        self.u, self.k = u, k
        self._hwnd = None

    def seq(self) -> int:
        return int(self.u.GetClipboardSequenceNumber())

    def register(self, name: str) -> int:
        return int(self.u.RegisterClipboardFormatW(name))

    def open(self) -> bool:
        if self._hwnd is None:
            # For SetClipboardData to work the clipboard needs an owner window (a message-only window)
            self._hwnd = self.u.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0,
                                                wintypes.HWND(HWND_MESSAGE), None, None, None)
        for _ in range(25):                      # another app may be holding the clipboard briefly
            if self.u.OpenClipboard(self._hwnd):
                return True
            time.sleep(0.02)
        return False

    def close(self) -> None:
        self.u.CloseClipboard()

    def empty(self) -> None:
        self.u.EmptyClipboard()

    def available(self, fmt: int) -> bool:
        return bool(self.u.IsClipboardFormatAvailable(fmt))

    def get(self, fmt: int) -> Optional[bytes]:
        h = self.u.GetClipboardData(fmt)
        if not h:
            return None
        size = self.k.GlobalSize(h)
        ptr = self.k.GlobalLock(h)
        if not ptr:
            return None
        try:
            return ctypes.string_at(ptr, size)
        finally:
            self.k.GlobalUnlock(h)

    def set(self, fmt: int, data: bytes) -> bool:
        h = self.k.GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
        if not h:
            return False
        ptr = self.k.GlobalLock(h)
        if not ptr:
            self.k.GlobalFree(h)
            return False
        ctypes.memmove(ptr, data, len(data))
        self.k.GlobalUnlock(h)
        if not self.u.SetClipboardData(fmt, h):
            self.k.GlobalFree(h)             # sistem sahiplenmedi
            return False
        return True


class WinRichClipboard:
    def __init__(self, api=None):
        self.api = api or Win32Clipboard()
        self.cf_html = self.api.register("HTML Format")
        self.cf_rtf = self.api.register("Rich Text Format")
        self.cf_png = self.api.register("PNG")
        self.cf_dropeffect = self.api.register("Preferred DropEffect")

    def change_count(self) -> int:
        return self.api.seq()

    def get_formats(self) -> Dict[str, bytes]:
        api = self.api
        if not api.open():
            raise OSError("could not open the clipboard (another app is using it)")
        raw: Dict[str, bytes] = {}
        try:
            if api.available(CF_HDROP):
                raw["hdrop"] = api.get(CF_HDROP) or b""
            if api.available(CF_UNICODETEXT):
                raw["text"] = api.get(CF_UNICODETEXT) or b""
            if api.available(self.cf_html):
                raw["html"] = api.get(self.cf_html) or b""
            if api.available(self.cf_rtf):
                raw["rtf"] = api.get(self.cf_rtf) or b""
            if api.available(self.cf_png):
                raw["png"] = api.get(self.cf_png) or b""
            elif api.available(CF_DIB):
                raw["dib"] = api.get(CF_DIB) or b""
            elif api.available(CF_DIBV5):
                raw["dib"] = api.get(CF_DIBV5) or b""
        finally:
            api.close()
        # Conversions happen after the clipboard is closed (so it is not held long against other apps)
        out: Dict[str, bytes] = {}
        if raw.get("hdrop"):
            try:
                paths = RC.dropfiles_to_paths(raw["hdrop"])
                if paths:
                    out[RC.FILES] = "\n".join(paths).encode("utf-8")
            except Exception as exc:
                log.debug("Could not read the file list: %s", exc)
        if raw.get("text"):
            t = raw["text"].decode("utf-16-le", "replace").split("\0", 1)[0]
            if t:
                out[RC.TEXT] = t.encode("utf-8")
        if raw.get("html"):
            h = RC.from_cf_html(raw["html"])
            if h.strip():
                out[RC.HTML] = h.encode("utf-8")
        if raw.get("rtf"):
            r = raw["rtf"].rstrip(b"\0")
            if r:
                out[RC.RTF] = r
        if raw.get("png"):
            out[RC.PNG] = raw["png"]
        elif raw.get("dib"):
            try:
                out[RC.PNG] = RC.dib_to_png(raw["dib"])
            except Exception as exc:
                log.debug("Could not convert the clipboard image: %s", exc)
        return out

    def set_formats(self, formats: Dict[str, bytes]) -> None:
        items = []
        if formats.get(RC.FILES):
            # Files from the Mac: Explorer copies them with Ctrl+V
            paths = [p for p in bytes(formats[RC.FILES]).decode("utf-8", "replace").split("\n") if p]
            items.append((CF_HDROP, RC.paths_to_dropfiles(paths)))
            items.append((self.cf_dropeffect, struct.pack("<I", RC.DROPEFFECT_COPY)))
        if formats.get(RC.TEXT):
            items.append((CF_UNICODETEXT, formats[RC.TEXT].decode("utf-8", "replace").encode("utf-16-le") + b"\0\0"))
        if formats.get(RC.HTML):
            items.append((self.cf_html, RC.to_cf_html(formats[RC.HTML].decode("utf-8", "replace")) + b"\0"))
        if formats.get(RC.RTF):
            items.append((self.cf_rtf, formats[RC.RTF] + b"\0"))
        if formats.get(RC.PNG):
            items.append((self.cf_png, formats[RC.PNG]))
            try:
                items.append((CF_DIB, RC.png_to_dib(formats[RC.PNG])))
            except Exception as exc:
                log.debug("Could not convert the image to CF_DIB: %s", exc)
        api = self.api
        if not api.open():
            raise OSError("could not open the clipboard (another app is using it)")
        try:
            api.empty()
            for fmt, data in items:
                if not api.set(fmt, data):
                    log.debug("Could not write a clipboard format: %s", fmt)
        finally:
            api.close()

    # old interface (text only)
    def get_text(self) -> Optional[str]:
        t = self.get_formats().get(RC.TEXT)
        return t.decode("utf-8") if t else None

    def set_text(self, text: str) -> None:
        self.set_formats({RC.TEXT: text.encode("utf-8")})
