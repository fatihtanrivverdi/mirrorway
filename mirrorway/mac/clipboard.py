"""The macOS clipboard (NSPasteboard): text, HTML, RTF and images."""
from __future__ import annotations

import logging
from typing import Dict, Optional

from AppKit import NSPasteboard, NSPasteboardTypeString

from .. import richclip as RC

log = logging.getLogger("mirrorway.clipboard")

T_HTML = "public.html"
T_RTF = "public.rtf"
T_PNG = "public.png"
T_TIFF = "public.tiff"


def _bytes(nsdata) -> Optional[bytes]:
    if nsdata is None:
        return None
    try:
        return bytes(nsdata)
    except Exception:
        return nsdata.bytes().tobytes() if hasattr(nsdata, "bytes") else None


def tiff_to_png(tiff: bytes) -> Optional[bytes]:
    try:
        from AppKit import NSBitmapImageRep, NSBitmapImageFileTypePNG
        from Foundation import NSData
        rep = NSBitmapImageRep.imageRepWithData_(NSData.dataWithBytes_length_(tiff, len(tiff)))
        if rep is not None:
            return _bytes(rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}))
    except Exception as exc:
        log.debug("Could not convert TIFF (AppKit): %s", exc)
    try:
        import io
        from PIL import Image
        out = io.BytesIO()
        Image.open(io.BytesIO(tiff)).save(out, "PNG")
        return out.getvalue()
    except Exception as exc:
        log.debug("Could not convert TIFF (Pillow): %s", exc)
        return None


def png_to_tiff(png: bytes) -> Optional[bytes]:
    try:
        from AppKit import NSBitmapImageRep, NSBitmapImageFileTypeTIFF
        from Foundation import NSData
        rep = NSBitmapImageRep.imageRepWithData_(NSData.dataWithBytes_length_(png, len(png)))
        if rep is not None:
            return _bytes(rep.representationUsingType_properties_(NSBitmapImageFileTypeTIFF, {}))
    except Exception as exc:
        log.debug("PNG -> TIFF failed: %s", exc)
    return None


class MacClipboard:
    def __init__(self):
        self.pb = NSPasteboard.generalPasteboard()

    def change_count(self) -> int:
        return int(self.pb.changeCount())

    # ---- rich -----------------------------------------------------------------------------
    def _file_paths(self):
        """Paths of the files/folders copied in Finder (an empty list if none)."""
        try:
            from AppKit import NSURL
            try:
                from AppKit import NSPasteboardURLReadingFileURLsOnlyKey as only_files
            except ImportError:
                only_files = "NSPasteboardURLReadingFileURLsOnlyKey"
            urls = self.pb.readObjectsForClasses_options_([NSURL], {only_files: True}) or []
            return [str(u.path()) for u in urls if u is not None and u.path()]
        except Exception as exc:
            log.debug("Could not read file paths: %s", exc)
            return []

    def get_formats(self) -> Dict[str, bytes]:
        pb = self.pb
        types = set(str(t) for t in (pb.types() or []))
        out: Dict[str, bytes] = {}
        if "public.file-url" in types:
            paths = self._file_paths()
            if paths:
                out[RC.FILES] = "\n".join(paths).encode("utf-8")
        s = pb.stringForType_(NSPasteboardTypeString)
        if s is not None and str(s):
            out[RC.TEXT] = str(s).encode("utf-8")
        if T_HTML in types:
            h = pb.stringForType_(T_HTML)
            if h is not None and str(h).strip():
                out[RC.HTML] = RC.html_fragment(str(h)).encode("utf-8")
        if T_RTF in types:
            r = _bytes(pb.dataForType_(T_RTF))
            if r:
                out[RC.RTF] = r
        png = _bytes(pb.dataForType_(T_PNG)) if T_PNG in types else None
        if not png and T_TIFF in types:
            tiff = _bytes(pb.dataForType_(T_TIFF))
            png = tiff_to_png(tiff) if tiff else None
        if png:
            out[RC.PNG] = png
        return out

    def set_formats(self, formats: Dict[str, bytes]) -> None:
        from Foundation import NSData
        pb = self.pb
        if formats.get(RC.FILES):
            # Files from Windows: only file URLs (Finder copies them with ⌘V; if text were added,
            # Finder might also create a text clipping file)
            from AppKit import NSURL
            paths = [p for p in bytes(formats[RC.FILES]).decode("utf-8", "replace").split("\n") if p]
            pb.clearContents()
            pb.writeObjects_([NSURL.fileURLWithPath_(p) for p in paths])
            return
        pb.clearContents()
        if formats.get(RC.TEXT):
            pb.setString_forType_(formats[RC.TEXT].decode("utf-8", "replace"), NSPasteboardTypeString)
        if formats.get(RC.HTML):
            pb.setString_forType_(RC.html_document(formats[RC.HTML].decode("utf-8", "replace")), T_HTML)
        if formats.get(RC.RTF):
            r = formats[RC.RTF]
            pb.setData_forType_(NSData.dataWithBytes_length_(r, len(r)), T_RTF)
        if formats.get(RC.PNG):
            png = formats[RC.PNG]
            pb.setData_forType_(NSData.dataWithBytes_length_(png, len(png)), T_PNG)
            tiff = png_to_tiff(png)                 # older apps expect TIFF
            if tiff:
                pb.setData_forType_(NSData.dataWithBytes_length_(tiff, len(tiff)), T_TIFF)

    # ---- text only (old interface) -----------------------------------------------------
    def get_text(self) -> Optional[str]:
        s = self.pb.stringForType_(NSPasteboardTypeString)
        return str(s) if s is not None else None

    def set_text(self, text: str) -> None:
        self.pb.clearContents()
        self.pb.setString_forType_(text, NSPasteboardTypeString)

    def copy(self, text: str) -> None:
        self.set_text(text)
