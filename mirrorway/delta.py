"""Changed-region (delta) encoding: compares two consecutive frames tile by tile and sends only the
changed rectangles as JPEG. Needs numpy; uses simplejpeg if available, Pillow otherwise.
Platform-independent (the Mac encodes, Windows decodes)."""
from __future__ import annotations

import io
import struct
from typing import List, Optional, Tuple

import numpy as np

try:
    import simplejpeg  # libjpeg-turbo, encodes BGRX input without conversion
except Exception:      # pragma: no cover - Pillow when simplejpeg is missing
    simplejpeg = None

from PIL import Image

TILE = 32                 # comparison tile size (pixels)
FULL_FRAME_RATIO = 0.55   # send a full frame when the changed area is larger than this
MAX_RECTS = 40            # more rectangles than this -> full frame
KEYFRAME_INTERVAL = 10.0  # when something changes, a full frame at least this often (seconds)

_DELTA_HDR = struct.Struct("<HHIHHH")   # width, height, sequence, encode ms, flags, rectangle count
_RECT_HDR = struct.Struct("<HHHHI")     # x, y, width, height, JPEG length

Rect = Tuple[int, int, int, int]        # x, y, w, h


def backend_name() -> str:
    return "simplejpeg" if simplejpeg is not None else "Pillow"


# ---------------------------------------------------------------------------
# Changed tiles -> rectangles
# ---------------------------------------------------------------------------
def _changed_mask(prev: np.ndarray, cur: np.ndarray) -> np.ndarray:
    if cur.ndim == 3 and cur.shape[2] == 4 and cur.flags.c_contiguous and prev.flags.c_contiguous:
        return prev.view(np.uint32)[..., 0] != cur.view(np.uint32)[..., 0]
    return np.any(prev != cur, axis=2)


def tile_grid(prev: np.ndarray, cur: np.ndarray, tile: int = TILE) -> np.ndarray:
    """Bool array of (tile rows, tile columns): which tiles changed."""
    h, w = cur.shape[:2]
    mask = _changed_mask(prev, cur)
    th, tw = -(-h // tile), -(-w // tile)
    if th * tile != h or tw * tile != w:
        padded = np.zeros((th * tile, tw * tile), dtype=bool)
        padded[:h, :w] = mask
        mask = padded
    return mask.reshape(th, tile, tw, tile).any(axis=(1, 3))


def grid_to_rects(grid: np.ndarray, tile: int, width: int, height: int) -> List[Rect]:
    """Merge changed tiles row by row, then stack consecutive rows that cover the same range."""
    open_rects = {}       # (x0, x1) -> [y0, y1]  (in tiles, y1 exclusive)
    done: List[Tuple[int, int, int, int]] = []
    for ty in range(grid.shape[0]):
        row = grid[ty]
        runs = []
        tx = 0
        n = row.shape[0]
        while tx < n:
            if row[tx]:
                start = tx
                while tx < n and row[tx]:
                    tx += 1
                runs.append((start, tx))
            else:
                tx += 1
        next_open = {}
        for run in runs:
            if run in open_rects and open_rects[run][1] == ty:
                y0, _ = open_rects.pop(run)
                next_open[run] = [y0, ty + 1]
            else:
                next_open[run] = [ty, ty + 1]
        for (x0, x1), (y0, y1) in open_rects.items():
            done.append((x0, y0, x1, y1))
        open_rects = next_open
    for (x0, x1), (y0, y1) in open_rects.items():
        done.append((x0, y0, x1, y1))
    rects = []
    for x0, y0, x1, y1 in done:
        px, py = x0 * tile, y0 * tile
        rects.append((px, py, min(x1 * tile, width) - px, min(y1 * tile, height) - py))
    rects.sort(key=lambda r: (r[1], r[0]))
    return rects


def changed_rects(prev: Optional[np.ndarray], cur: np.ndarray, tile: int = TILE) -> Optional[List[Rect]]:
    """Changed rectangles; [] = no change, None = a full frame must be sent."""
    if prev is None or prev.shape != cur.shape:
        return None
    grid = tile_grid(prev, cur, tile)
    changed = int(grid.sum())
    if changed == 0:
        return []
    if changed / grid.size > FULL_FRAME_RATIO:
        return None
    h, w = cur.shape[:2]
    rects = grid_to_rects(grid, tile, w, h)
    if len(rects) > MAX_RECTS:
        return None
    return rects


# ---------------------------------------------------------------------------
# JPEG encoding / decoding
# ---------------------------------------------------------------------------
def encode(arr: np.ndarray, colorspace: str, quality: int, subsampling: int) -> bytes:
    """colorspace: 'BGRX' (Mac screen capture) or 'RGB'. subsampling: 0 = 4:4:4, 2 = 4:2:0."""
    arr = np.ascontiguousarray(arr)
    if simplejpeg is not None:
        return simplejpeg.encode_jpeg(arr, quality=int(quality), colorspace=colorspace,
                                      colorsubsampling="444" if subsampling == 0 else "420")
    h, w = arr.shape[:2]
    if colorspace == "BGRX":
        img = Image.frombuffer("RGB", (w, h), arr.tobytes(), "raw", "BGRX", 0, 1)
    else:
        img = Image.fromarray(arr, "RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=int(quality), subsampling=subsampling)
    return buf.getvalue()


def decode(data: bytes) -> np.ndarray:
    """JPEG -> (h, w, 3) RGB dizi."""
    if simplejpeg is not None:
        return simplejpeg.decode_jpeg(data, colorspace="RGB")
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))


# ---------------------------------------------------------------------------
# Packing
# ---------------------------------------------------------------------------
def pack_delta(width: int, height: int, seq: int, enc_ms: float,
               parts: List[Tuple[Rect, bytes]], flags: int = 0) -> bytes:
    out = [_DELTA_HDR.pack(width, height, seq & 0xFFFFFFFF, min(int(enc_ms), 65535), flags, len(parts))]
    for (x, y, w, h), data in parts:
        out.append(_RECT_HDR.pack(x, y, w, h, len(data)))
        out.append(data)
    return b"".join(out)


def unpack_delta(b: bytes):
    w, h, seq, enc_ms, flags, n = _DELTA_HDR.unpack_from(b)
    off = _DELTA_HDR.size
    parts = []
    for _ in range(n):
        x, y, rw, rh, ln = _RECT_HDR.unpack_from(b, off)
        off += _RECT_HDR.size
        parts.append(((x, y, rw, rh), b[off:off + ln]))
        off += ln
    return w, h, seq, enc_ms, flags, parts


class Canvas:
    """Windows side: keeps the last full frame and pastes incoming changed regions onto it."""

    def __init__(self):
        self.arr: Optional[np.ndarray] = None

    def set_full(self, data: bytes) -> np.ndarray:
        self.arr = np.array(decode(data), copy=True)
        return self.arr

    def apply(self, width: int, height: int, parts) -> bool:
        """Apply changed regions; False if there is no canvas or the size does not match (ask for a full frame)."""
        if self.arr is None or self.arr.shape[0] != height or self.arr.shape[1] != width:
            return False
        for (x, y, w, h), data in parts:
            patch = decode(data)
            ph, pw = patch.shape[:2]
            if ph != h or pw != w or x + w > width or y + h > height:
                return False
            self.arr[y:y + h, x:x + w] = patch
        return True
