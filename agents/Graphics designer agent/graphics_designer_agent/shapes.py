"""Stage-3 2D shape primitives (deterministic PIL drawing).

Each function draws onto an RGBA ``canvas`` within a pixel box
``(x0, y0, x1, y1)``. ``fill``/``stroke`` are RGB tuples (or ``None``);
``stroke_w`` is in pixels. Pure drawing — no model calls, fully reproducible.
"""
from __future__ import annotations

from PIL import ImageDraw

SHAPE_KINDS = ("rect", "rounded-rect", "circle", "triangle", "arrow", "divider", "callout")


def _d(canvas):
    return ImageDraw.Draw(canvas, "RGBA")


def _rgba(c, a=255):
    return None if c is None else (int(c[0]), int(c[1]), int(c[2]), a)


def draw_rect(canvas, box, *, fill=None, stroke=None, stroke_w=0, radius=0):
    x0, y0, x1, y1 = box
    r = max(0, int(radius))
    kw = dict(fill=_rgba(fill), outline=_rgba(stroke), width=max(0, int(stroke_w)))
    if r > 0:
        _d(canvas).rounded_rectangle([x0, y0, x1, y1], radius=r, **kw)
    else:
        _d(canvas).rectangle([x0, y0, x1, y1], **kw)
    return canvas


def draw_ellipse(canvas, box, *, fill=None, stroke=None, stroke_w=0):
    _d(canvas).ellipse(list(box), fill=_rgba(fill), outline=_rgba(stroke), width=max(0, int(stroke_w)))
    return canvas


def draw_triangle(canvas, box, *, fill=None, stroke=None, stroke_w=0):
    x0, y0, x1, y1 = box
    pts = [((x0 + x1) / 2, y0), (x1, y1), (x0, y1)]
    _d(canvas).polygon(pts, fill=_rgba(fill), outline=_rgba(stroke), width=max(0, int(stroke_w)))
    return canvas


def draw_arrow(canvas, box, *, fill=None, stroke=None, stroke_w=6):
    """Horizontal arrow pointing right, spanning the box width at mid-height."""
    x0, y0, x1, y1 = box
    my = (y0 + y1) / 2
    col = _rgba(fill) or _rgba(stroke) or (15, 15, 15, 255)
    w = max(2, int(stroke_w))
    head = max(6.0, (x1 - x0) * 0.32)
    d = _d(canvas)
    d.line([(x0, my), (x1 - head * 0.4, my)], fill=col, width=w)
    d.polygon([(x1, my), (x1 - head, my - head * 0.6), (x1 - head, my + head * 0.6)], fill=col)
    return canvas


def draw_divider(canvas, box, *, fill=None, stroke=None, stroke_w=3):
    x0, y0, x1, y1 = box
    my = (y0 + y1) / 2
    col = _rgba(fill) or _rgba(stroke) or (15, 15, 15, 255)
    _d(canvas).line([(x0, my), (x1, my)], fill=col, width=max(1, int(stroke_w)))
    return canvas


def draw_callout(canvas, box, *, fill=None, stroke=None, stroke_w=0, radius=14):
    """A rounded box for callout text (the text itself is drawn as a text layer)."""
    return draw_rect(canvas, box, fill=fill, stroke=stroke, stroke_w=stroke_w, radius=radius)


# Dispatch by kind for the renderer.
def draw(canvas, kind, box, *, fill=None, stroke=None, stroke_w=0, radius=0):
    if kind in ("rect", "rounded-rect", "callout"):
        r = radius if kind != "rect" else 0
        if kind == "callout" and not r:
            r = max(6, int((box[3] - box[1]) * 0.18))
        return draw_rect(canvas, box, fill=fill, stroke=stroke, stroke_w=stroke_w, radius=r)
    if kind == "circle":
        return draw_ellipse(canvas, box, fill=fill, stroke=stroke, stroke_w=stroke_w)
    if kind == "triangle":
        return draw_triangle(canvas, box, fill=fill, stroke=stroke, stroke_w=stroke_w)
    if kind == "arrow":
        return draw_arrow(canvas, box, fill=fill, stroke=stroke, stroke_w=stroke_w or 6)
    if kind == "divider":
        return draw_divider(canvas, box, fill=fill, stroke=stroke, stroke_w=stroke_w or 3)
    return canvas  # unknown kind → no-op (never crash)
