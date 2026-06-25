"""Stage-3 infographic icon set — small monochrome glyphs drawn programmatically
with PIL (no external assets). Each icon draws inside a pixel box in one colour.

Addressable by key via ``ICON_KEYS``; an unknown key is a safe no-op so a stale
selection can never crash a render.
"""
from __future__ import annotations

from PIL import ImageDraw

ICON_KEYS = ("check", "star", "bolt", "plus", "dot", "arrow", "circle-check", "minus")


def _rgba(c):
    return (int(c[0]), int(c[1]), int(c[2]), 255)


def draw_icon(canvas, key, box, color):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    col = _rgba(color)
    d = ImageDraw.Draw(canvas, "RGBA")
    lw = max(2, int(min(w, h) * 0.12))

    def pt(fx, fy):
        return (x0 + fx * w, y0 + fy * h)

    if key == "check":
        d.line([pt(0.15, 0.55), pt(0.42, 0.8), pt(0.85, 0.22)], fill=col, width=lw, joint="curve")
    elif key == "circle-check":
        d.ellipse([x0, y0, x1, y1], outline=col, width=lw)
        d.line([pt(0.30, 0.52), pt(0.45, 0.68), pt(0.72, 0.34)], fill=col, width=lw, joint="curve")
    elif key == "star":
        import math
        cx, cy, R, r = x0 + w / 2, y0 + h / 2, min(w, h) / 2, min(w, h) / 4
        pts = []
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            rad = R if i % 2 == 0 else r
            pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
        d.polygon(pts, fill=col)
    elif key == "bolt":
        d.polygon([pt(0.55, 0.05), pt(0.2, 0.55), pt(0.45, 0.55), pt(0.4, 0.95),
                   pt(0.8, 0.4), pt(0.52, 0.4)], fill=col)
    elif key == "plus":
        d.rectangle([pt(0.44, 0.15)[0], pt(0.44, 0.15)[1], pt(0.56, 0.85)[0], pt(0.56, 0.85)[1]], fill=col)
        d.rectangle([pt(0.15, 0.44)[0], pt(0.15, 0.44)[1], pt(0.85, 0.56)[0], pt(0.85, 0.56)[1]], fill=col)
    elif key == "minus":
        d.rectangle([pt(0.15, 0.44)[0], pt(0.15, 0.44)[1], pt(0.85, 0.56)[0], pt(0.85, 0.56)[1]], fill=col)
    elif key == "dot":
        d.ellipse([pt(0.3, 0.3)[0], pt(0.3, 0.3)[1], pt(0.7, 0.7)[0], pt(0.7, 0.7)[1]], fill=col)
    elif key == "arrow":
        my = y0 + h / 2
        head = w * 0.32
        d.line([(x0, my), (x1 - head * 0.4, my)], fill=col, width=lw)
        d.polygon([(x1, my), (x1 - head, my - head * 0.6), (x1 - head, my + head * 0.6)], fill=col)
    # unknown key → nothing drawn
    return canvas
