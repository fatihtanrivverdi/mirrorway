"""The switch indicator: where the pointer appears after a switch, a violet ring grows and fades for half a second.

The same shape on both computers: the Mac animates it with Core Animation (mac/ring.py), Windows draws the frames
below into a layered, click-through window (win/ring.py). Sizes are in points (Mac) / 100%-scale pixels (Windows).
"""
from __future__ import annotations

from typing import Tuple

DURATION = 0.5                      # seconds
FPS = 60
R_START, R_END = 10.0, 42.0          # ring radius (to the middle of the stroke)
W_START, W_END = 5.0, 2.5            # stroke width
HALO = 1.5                          # the white edge on both sides of the stroke (visible on dark and light screens)
COLOR = (123, 97, 255)              # Mirrorway violet (between the panel's #5B3FD6 and #A898FF)
HALO_COLOR = (255, 255, 255)
HALO_ALPHA = 0.7
MARGIN = 4.0


def ease_out(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1.0 - (1.0 - t) ** 3


def at(t: float) -> Tuple[float, float, float]:
    """(radius, stroke width, opacity) at t seconds after the switch."""
    p = min(max(t / DURATION, 0.0), 1.0)
    e = ease_out(p)
    radius = R_START + (R_END - R_START) * e
    width = W_START + (W_END - W_START) * e
    opacity = 1.0 - p ** 2                        # stays strong at first, then fades
    return radius, width, opacity


def box(scale: float = 1.0) -> int:
    """Side of the square the ring is drawn in, in pixels (it is centred on the pointer)."""
    return int(round(2 * (R_END + W_START / 2 + HALO + MARGIN) * scale)) | 1      # odd: a centre pixel


def layer_shape(radius: float, width: float, halo: float = 0.0) -> Tuple[float, float]:
    """(side, border width) of a circular layer whose border is the ring (a border is drawn inside the bounds);
    halo widens it on both sides (the white edge)."""
    w = width + 2 * halo
    return 2 * (radius + w / 2), w


def frame_count() -> int:
    return int(DURATION * FPS)


def render(t: float, scale: float = 1.0, supersample: int = 4):
    """The ring at time t as a PIL RGBA image of box(scale) pixels (straight alpha, drawn 4× larger for smooth
    edges)."""
    from PIL import Image, ImageDraw
    size = box(scale)
    ss = size * supersample
    k = scale * supersample
    radius, width, opacity = at(t)
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    if opacity <= 0:
        return img.resize((size, size), Image.LANCZOS)
    c = ss / 2.0

    def ring(r: float, w: float, rgb, alpha: float):
        layer = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        outer = (r + w / 2) * k
        d.ellipse((c - outer, c - outer, c + outer, c + outer), outline=rgb + (int(round(255 * alpha)),),
                  width=max(1, int(round(w * k))))
        return layer
    img = Image.alpha_composite(img, ring(radius, width + 2 * HALO, HALO_COLOR, HALO_ALPHA * opacity))
    img = Image.alpha_composite(img, ring(radius, width, COLOR, opacity))
    return img.resize((size, size), Image.LANCZOS)


def premultiplied_bgra(img) -> bytes:
    """A PIL RGBA image as top-down BGRA bytes with premultiplied alpha (what UpdateLayeredWindow wants)."""
    from PIL import Image, ImageChops
    r, g, b, a = img.convert("RGBA").split()
    r, g, b = (ImageChops.multiply(ch, a) for ch in (r, g, b))
    return Image.merge("RGBA", (b, g, r, a)).tobytes()
