"""Rich clipboard: text, HTML, RTF and images (PNG) — the platform-independent part.

Clipboard content travels as a "bundle": {"text": bytes, "html": bytes, "rtf": bytes, "png": bytes}.
The bundle is not sent as one channel message but in 64 KB chunks (MSG_CLIPDATA), so mouse and keyboard
messages can slip in between while a large image is copied and the pointer never stalls.

Windows-specific format conversions also live here (so they can be tested):
  HTML  <->  "HTML Format" (CF_HTML: UTF-8 with byte offsets in a header)
  PNG   <->  CF_DIB (a BMP without the file header)
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import struct
import sys
import threading
import time
from typing import Callable, Dict, Iterator, Optional

log = logging.getLogger("mirrorway.clipboard")

TEXT, HTML, RTF, PNG = "text", "html", "rtf", "png"
FORMATS = (TEXT, HTML, RTF, PNG)
FILES = "files"            # local paths of copied files (UTF-8, one per line); not part of the clipboard bundle,
                           # they go through the file transfer (mirrorway.filexfer)
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
FLAG_FIRST, FLAG_LAST = 1, 2
FEATURE = "clip2"

_U32 = struct.Struct("!I")
_CHUNK = struct.Struct("!IIB")


# ---- demet ------------------------------------------------------------------------------
def pack_bundle(formats: Dict[str, bytes]) -> bytes:
    parts = [(k, bytes(v)) for k, v in formats.items() if k in FORMATS and v]
    head = json.dumps({"v": 1, "parts": [[k, len(v)] for k, v in parts]}, separators=(",", ":")).encode()
    return _U32.pack(len(head)) + head + b"".join(v for _, v in parts)


def unpack_bundle(data: bytes) -> Dict[str, bytes]:
    (n,) = _U32.unpack_from(data)
    if n > 4096:
        raise ValueError("clipboard header too large")
    head = json.loads(data[4:4 + n].decode("utf-8"))
    out, pos = {}, 4 + n
    for name, size in head.get("parts", []):
        size = int(size)
        if size < 0 or pos + size > len(data):
            raise ValueError("clipboard data incomplete")
        if name in FORMATS:
            out[name] = data[pos:pos + size]
        pos += size
    return out


def digest(formats: Dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for k in sorted(formats):
        v = formats[k]
        if v:
            h.update(k.encode() + b"\0" + _U32.pack(len(v)) + v)
    return h.hexdigest()


def summary(formats: Dict[str, bytes]) -> str:
    def size(n):
        return f"{n} B" if n < 1024 else (f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB")
    names = {TEXT: "text", HTML: "HTML", RTF: "RTF", PNG: "image"}
    parts = [f"{names[k]} {size(len(formats[k]))}" for k in FORMATS if formats.get(k)]
    if formats.get(FILES):
        n = len([p for p in bytes(formats[FILES]).decode("utf-8", "replace").split("\n") if p])
        parts.insert(0, f"{n} files/folders")
    return ", ".join(parts)




def to_peer(peer: Optional[str]) -> str:
    return f"to {peer}" if peer else "to the other side"


def from_peer(peer: Optional[str]) -> str:
    return f"from {peer}" if peer else "from the other side"


def limit(formats: Dict[str, bytes]) -> Dict[str, bytes]:
    """Apply the limits: an oversized image is dropped (the text stays), oversized text is not sent at all."""
    out = {k: v for k, v in formats.items() if k in FORMATS and v}
    if sum(len(v) for v in out.values()) > MAX_BUNDLE_BYTES and PNG in out:
        log.info("Clipboard image too large (%d MB), sending text only", len(out[PNG]) // 1048576)
        out.pop(PNG)
    for k in (TEXT, HTML, RTF):
        if k in out and len(out[k]) > MAX_TEXT_BYTES:
            log.info("Clipboard content (%s) too large, not sent", k)
            out.pop(k)
    return out


# ---- chunking ------------------------------------------------------------------------------
def chunks(clip_id: int, data: bytes, size: int = CHUNK_BYTES) -> Iterator[bytes]:
    n = max(1, (len(data) + size - 1) // size)
    for i in range(n):
        flags = (FLAG_FIRST if i == 0 else 0) | (FLAG_LAST if i == n - 1 else 0)
        yield _CHUNK.pack(clip_id & 0xFFFFFFFF, i, flags) + data[i * size:(i + 1) * size]


class Assembler:
    """Reassembles chunks; drops a half-received bundle when a new one starts."""

    def __init__(self, max_bytes: int = MAX_BUNDLE_BYTES + 65536):
        self.max_bytes = max_bytes
        self._id = None
        self._next = 0
        self._buf = bytearray()

    def feed(self, payload: bytes) -> Optional[bytes]:
        clip_id, idx, flags = _CHUNK.unpack_from(payload)
        body = payload[_CHUNK.size:]
        if flags & FLAG_FIRST:
            self._id, self._next, self._buf = clip_id, 0, bytearray()
        if clip_id != self._id or idx != self._next:
            self._id = None                          # out of order or an old chunk: wait for the next bundle
            return None
        self._buf.extend(body)
        self._next += 1
        if len(self._buf) > self.max_bytes:
            log.info("Incoming clipboard content too large, dropped")
            self._id = None
            return None
        if flags & FLAG_LAST:
            data, self._id, self._buf = bytes(self._buf), None, bytearray()
            return data
        return None


class ClipSender:
    """Sends clipboard bundles chunk by chunk in the background. If a new copy arrives while one is being sent,
    the old one is abandoned (the other side drops the partial bundle).

    Every send is logged with formats and sizes (never the content)."""

    def __init__(self, send_raw: Callable[[bytes], None], rich_fn: Callable[[], bool],
                 send_text: Callable[[str], None], chunk_bytes: int = CHUNK_BYTES, peer: Optional[str] = None):
        self.send_raw, self.rich_fn, self.send_text, self.chunk_bytes = send_raw, rich_fn, send_text, chunk_bytes
        self.peer = peer
        self._cond = threading.Condition()
        self._pending: Optional[Dict[str, bytes]] = None
        self._gen = 0
        self._next_id = 1
        self._thread: Optional[threading.Thread] = None

    def send(self, formats: Dict[str, bytes]) -> None:
        if not self.rich_fn():
            text = formats.get(TEXT)
            if text:
                try:
                    self.send_text(text.decode("utf-8", "replace"))
                except Exception as exc:
                    log.info("Clipboard could not be sent %s: %s", to_peer(self.peer), exc)
                    return
                skipped = [k for k in FORMATS if k != TEXT and formats.get(k)]
                log.info("Clipboard sent %s: %s%s", to_peer(self.peer), summary({TEXT: text}),
                         " (the other side is an old version: text only)" if skipped else "")
            return
        with self._cond:
            self._pending, self._gen = dict(formats), self._gen + 1
            self._cond.notify()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name="mirrorway-clip-send")
                self._thread.start()

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._pending is None:
                    self._cond.wait(5.0)
                    if self._pending is None:
                        self._thread = None
                        return
                formats, gen, self._pending = self._pending, self._gen, None
                clip_id, self._next_id = self._next_id, self._next_id + 1
            data = pack_bundle(formats)
            t0, sent, done = time.monotonic(), 0, False
            try:
                for part in chunks(clip_id, data, self.chunk_bytes):
                    if self._gen != gen:
                        break                             # there is a newer copy
                    self.send_raw(part)
                    sent += 1
                else:
                    done = True
            except Exception as exc:
                log.info("Clipboard could not be sent %s (%s): %s", to_peer(self.peer), summary(formats), exc)
                continue
            if done:
                log.info("Clipboard sent %s: %s (%d chunks, %.0f ms)", to_peer(self.peer), summary(formats),
                         sent, (time.monotonic() - t0) * 1000.0)
            else:
                log.info("Clipboard send abandoned, a newer copy is going (%s)", summary(formats))


# ---- HTML <-> CF_HTML ----------------------------------------------------------------------
_CF_HTML_HEAD = ("Version:0.9\r\nStartHTML:{0:010d}\r\nEndHTML:{1:010d}\r\n"
                 "StartFragment:{2:010d}\r\nEndFragment:{3:010d}\r\n")
_BODY = re.compile(r"<body[^>]*>(.*)</body\s*>", re.I | re.S)


def html_fragment(html: str) -> str:
    """The body of a full HTML document (or the input itself if it is a fragment)."""
    m = _BODY.search(html)
    return m.group(1) if m else html


def to_cf_html(html: str) -> bytes:
    frag = html_fragment(html).encode("utf-8")
    pre = b"<html><body>\r\n<!--StartFragment-->"
    post = b"<!--EndFragment-->\r\n</body>\r\n</html>"
    head_len = len(_CF_HTML_HEAD.format(0, 0, 0, 0).encode("ascii"))
    start_html = head_len
    start_frag = start_html + len(pre)
    end_frag = start_frag + len(frag)
    end_html = end_frag + len(post)
    head = _CF_HTML_HEAD.format(start_html, end_html, start_frag, end_frag).encode("ascii")
    return head + pre + frag + post


def from_cf_html(data: bytes) -> str:
    """HTML fragment from CF_HTML (offsets in bytes, UTF-8)."""
    data = data.rstrip(b"\0")
    head = data[:400].decode("ascii", "replace")

    def num(key):
        m = re.search(key + r":\s*(-?\d+)", head)
        return int(m.group(1)) if m else -1
    s, e = num("StartFragment"), num("EndFragment")
    if 0 <= s <= e <= len(data):
        return data[s:e].decode("utf-8", "replace")
    s, e = num("StartHTML"), num("EndHTML")
    if 0 <= s <= e <= len(data):
        return html_fragment(data[s:e].decode("utf-8", "replace"))
    i = data.find(b"<")
    return data[i:].decode("utf-8", "replace") if i >= 0 else ""


def html_document(fragment: str) -> str:
    """HTML to write on the Mac: a full document with a declared character set."""
    if re.search(r"<html", fragment, re.I):
        return fragment
    return f'<html><head><meta charset="utf-8"></head><body>{fragment}</body></html>'


# ---- PNG <-> DIB --------------------------------------------------------------------------
def png_to_dib(png: bytes) -> bytes:
    """Windows CF_DIB: a 24-bit BMP body (transparency is flattened onto white; the transparent version is the "PNG")."""
    from PIL import Image
    img = Image.open(io.BytesIO(png))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "BMP")
    return out.getvalue()[14:]                       # drop the BITMAPFILEHEADER


def dib_to_png(dib: bytes) -> bytes:
    """CF_DIB / CF_DIBV5 -> PNG. The file header is rebuilt (taking the colour table and masks into account)."""
    from PIL import Image
    if len(dib) < 40:
        raise ValueError("DIB too short")
    hsize, = struct.unpack_from("<I", dib, 0)
    bitcount, = struct.unpack_from("<H", dib, 14)
    compression, = struct.unpack_from("<I", dib, 16)
    clr_used, = struct.unpack_from("<I", dib, 32)
    masks = 12 if (compression in (3, 6) and hsize == 40) else 0
    palette = (clr_used or (1 << bitcount)) * 4 if bitcount <= 8 else clr_used * 4
    off = 14 + hsize + masks + palette
    bmp = b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, off) + dib
    img = Image.open(io.BytesIO(bmp))
    img.load()
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


# ---- file list <-> CF_HDROP (Windows) ------------------------------------------------------------
_DROPFILES = struct.Struct("<IiiII")      # pFiles, pt.x, pt.y, fNC, fWide
DROPEFFECT_COPY = 1


def paths_to_dropfiles(paths) -> bytes:
    body = "".join(str(p) + "\0" for p in paths) + "\0"
    return _DROPFILES.pack(_DROPFILES.size, 0, 0, 0, 1) + body.encode("utf-16-le")


def dropfiles_to_paths(data: bytes):
    if len(data) < _DROPFILES.size:
        raise ValueError("DROPFILES too short")
    off, _x, _y, _nc, wide = _DROPFILES.unpack_from(data)
    raw = data[off:]
    if wide:
        text = raw.decode("utf-16-le", "replace")
    else:
        text = raw.decode("mbcs" if sys.platform == "win32" else "latin-1", "replace")
    out = []
    for part in text.split("\0"):
        if not part:
            break
        out.append(part)
    return out
