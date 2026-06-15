"""Stage 4 — deterministic logo compositor (spec §5.4).

Pillow stand-in for the spec's Sharp implementation (the backend is Python).
Implements the Stage-4 prompt's rules exactly: top-left placement, 4%-of-width
margin, 20% width (25% if wider than 3:1, 15% if taller than 1:2), proportional
scaling, and — critically — every base pixel outside the logo's bounding box is
left byte-identical. Generative models cannot guarantee that; this does.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

MARGIN_RATIO = 0.04
WIDTH_RATIO_DEFAULT = 0.20
WIDTH_RATIO_WIDE = 0.25  # logo aspect ratio wider than 3:1
WIDTH_RATIO_TALL = 0.15  # logo aspect ratio taller than 1:2


def _logo_width_ratio(logo_w: int, logo_h: int) -> float:
    ar = logo_w / logo_h if logo_h else 1.0
    if ar > 3.0:
        return WIDTH_RATIO_WIDE
    if ar < 0.5:
        return WIDTH_RATIO_TALL
    return WIDTH_RATIO_DEFAULT


def logo_placement(base_w: int, base_h: int, logo_w: int, logo_h: int) -> dict:
    """Compute the logo bounding box without rendering (used by the UI preview)."""
    target_w = max(1, round(base_w * _logo_width_ratio(logo_w, logo_h)))
    target_h = max(1, round(target_w * (logo_h / logo_w)))
    margin = round(base_w * MARGIN_RATIO)
    return {"x": margin, "y": margin, "w": target_w, "h": target_h}


def composite_logo(base_png: bytes, logo_png: bytes) -> bytes:
    """Overlay ``logo_png`` onto ``base_png`` per the Stage-4 rules.

    Returns PNG bytes at the base image's exact dimensions. Base pixels outside
    the logo bounding box are guaranteed identical to the input.
    """
    base = Image.open(BytesIO(base_png)).convert("RGBA")
    logo = Image.open(BytesIO(logo_png)).convert("RGBA")
    box = logo_placement(base.width, base.height, logo.width, logo.height)
    logo_resized = logo.resize((box["w"], box["h"]), Image.LANCZOS)
    out = base.copy()
    out.alpha_composite(logo_resized, (box["x"], box["y"]))
    buf = BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def pixels_identical_outside_box(base_png: bytes, out_png: bytes, box: dict) -> bool:
    """Test helper: are all pixels outside ``box`` identical between the two PNGs?"""
    base = Image.open(BytesIO(base_png)).convert("RGBA")
    out = Image.open(BytesIO(out_png)).convert("RGBA")
    if base.size != out.size:
        return False
    bpx, opx = base.load(), out.load()
    x0, y0, x1, y1 = box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"]
    for y in range(base.height):
        for x in range(base.width):
            if x0 <= x < x1 and y0 <= y < y1:
                continue
            if bpx[x, y] != opx[x, y]:
                return False
    return True
