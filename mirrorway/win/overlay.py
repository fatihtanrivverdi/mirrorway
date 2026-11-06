"""Measurements shown in the top right corner of the screen window (the title bar is hidden in full screen).

The text is drawn with PIL into a translucent box; the SDL window shows it as a texture, the Tk window as a
label. F9 shows/hides the measurements (remembered separately for full screen and windowed mode).
"""
from __future__ import annotations

from typing import List, Optional

from ..i18n import T

FONT_CANDIDATES = ("consola.ttf", "CascadiaMono.ttf", "segoeui.ttf",          # Windows
                   "DejaVuSansMono.ttf", "DejaVuSans.ttf", "Menlo.ttc")       # others (tests)


def overlay_lines(stats: dict, info: Optional[dict] = None) -> List[str]:
    info = info or {}
    lines = []
    fps = float(stats.get("fps", 0) or 0)
    kbps = float(stats.get("kbps", 0) or 0)
    lines.append(f"{fps:.0f} fps · {kbps / 1024:.1f} MB/s")
    codec = info.get("codec")
    if codec == "h264":
        parts = ["H.264"]
        tk = info.get("target_kbps")
        if tk:
            parts.append(T("target {mbit} Mbit/s").format(mbit=f"{float(tk) / 1000:.0f}"))
        tf = info.get("target_fps")
        if tf:
            parts.append(f"{int(tf)} fps")
        line = " · ".join(parts)
        if info.get("adaptive"):
            line += " (" + T("auto") + ")"
        lines.append(line)
    elif codec == "jpeg":
        lines.append(T("JPEG (changed regions)"))
    lat = float(stats.get("latency_ms", 0) or 0)
    rtt = float(stats.get("rtt_ms", 0) or 0)
    if lat > 0:
        lines.append(T("latency ≈ {lat} ms · network {rtt} ms").format(lat=f"{lat:.0f}", rtt=f"{rtt:.0f}"))
    enc = float(stats.get("enc_ms", 0) or 0)
    dec = float(stats.get("decode_ms", 0) or 0)
    disp = float(stats.get("display_ms", 0) or 0)
    decoder = stats.get("decoder") or ""
    dec_s = T("decode {ms}").format(ms=f"{dec:.1f}") + (f" ({decoder})" if decoder else "")
    lines.append(T("Mac {enc} · {decode} · draw {disp} ms").format(enc=f"{enc:.1f}", decode=dec_s, disp=f"{disp:.1f}"))
    return lines


_FONT_CACHE = {}


def load_font(size: int):
    from PIL import ImageFont
    size = max(9, int(size))
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    font = None
    for name in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(name, size)
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:                    # old Pillow
            font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def render_overlay(lines: List[str], font_px: int):
    """Draws the lines into a translucent dark box -> PIL RGBA image."""
    from PIL import Image, ImageDraw
    font = load_font(font_px)
    pad, gap = max(6, font_px // 2), max(2, font_px // 5)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    boxes = [probe.textbbox((0, 0), t or " ", font=font) for t in lines]
    line_h = max(b[3] - min(0, b[1]) for b in boxes) if boxes else font_px
    w = max(b[2] for b in boxes) + 2 * pad if boxes else 2 * pad
    h = len(lines) * line_h + (len(lines) - 1) * gap + 2 * pad
    img = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    try:
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=max(4, font_px // 2), fill=(15, 15, 18, 175))
    except AttributeError:
        d.rectangle((0, 0, w - 1, h - 1), fill=(15, 15, 18, 175))
    y = pad
    for i, t in enumerate(lines):
        color = (255, 255, 255, 255) if i == 0 else (215, 220, 228, 255)
        d.text((pad, y), t, font=font, fill=color)
        y += line_h + gap
    return img


def font_px_for(height: int) -> int:
    """Font size for the screen height (~17 px at 1080p)."""
    return max(12, min(28, int(height / 64)))
