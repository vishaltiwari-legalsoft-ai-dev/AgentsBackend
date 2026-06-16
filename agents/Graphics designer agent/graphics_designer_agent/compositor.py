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

# Nine-cell placement grid. Position key is "<vertical>-<horizontal>".
LOGO_POSITION_KEYS = (
    "top-left", "top-center", "top-right",
    "middle-left", "middle-center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
)
DEFAULT_LOGO_POSITION = "top-left"


def default_logo_layout() -> dict:
    """Factory placement: top-left, aspect-aware size, 4% margin, no nudge —
    matches the original deterministic behaviour."""
    return {
        "position": DEFAULT_LOGO_POSITION,
        "size_pct": None,        # None → aspect-aware default (20/25/15%)
        "margin_pct": round(MARGIN_RATIO * 100, 2),
        "offset_x": 0,
        "offset_y": 0,
    }


def _logo_width_ratio(logo_w: int, logo_h: int) -> float:
    ar = logo_w / logo_h if logo_h else 1.0
    if ar > 3.0:
        return WIDTH_RATIO_WIDE
    if ar < 0.5:
        return WIDTH_RATIO_TALL
    return WIDTH_RATIO_DEFAULT


def _anchor(position: str, base_w: int, base_h: int, w: int, h: int, margin: int) -> tuple[int, int]:
    """Top-left corner for ``position`` on the 3×3 grid (before fine offsets)."""
    parts = (position or DEFAULT_LOGO_POSITION).split("-")
    v, hpos = (parts + ["top", "left"])[:2] if len(parts) == 2 else ("top", "left")
    if hpos == "right":
        x = base_w - w - margin
    elif hpos == "center":
        x = (base_w - w) // 2
    else:  # left
        x = margin
    if v == "bottom":
        y = base_h - h - margin
    elif v == "middle":
        y = (base_h - h) // 2
    else:  # top
        y = margin
    return x, y


def logo_placement(
    base_w: int, base_h: int, logo_w: int, logo_h: int,
    *,
    position: str = DEFAULT_LOGO_POSITION,
    size_pct: float | None = None,
    margin_pct: float | None = None,
    offset_x: int = 0,
    offset_y: int = 0,
) -> dict:
    """Compute the logo bounding box without rendering (also drives the UI preview).

    ``size_pct`` is the logo width as a percentage of the base width; when ``None``
    the original aspect-aware default (20/25/15%) is used. ``margin_pct`` is the
    edge inset (% of base width, default 4%). ``offset_x``/``offset_y`` are fine
    pixel nudges applied after anchoring. The box is clamped inside the canvas.
    """
    ratio = (size_pct / 100.0) if size_pct else _logo_width_ratio(logo_w, logo_h)
    target_w = max(1, round(base_w * ratio))
    target_h = max(1, round(target_w * (logo_h / logo_w))) if logo_w else 1
    margin = round(base_w * (margin_pct / 100.0 if margin_pct is not None else MARGIN_RATIO))
    x, y = _anchor(position, base_w, base_h, target_w, target_h, margin)
    x += round(offset_x)
    y += round(offset_y)
    # Keep the logo fully on-canvas.
    x = min(max(x, 0), max(0, base_w - target_w))
    y = min(max(y, 0), max(0, base_h - target_h))
    return {"x": x, "y": y, "w": target_w, "h": target_h}


def composite_logo(base_png: bytes, logo_png: bytes, layout: dict | None = None) -> bytes:
    """Overlay ``logo_png`` onto ``base_png`` per the Stage-4 rules.

    ``layout`` (optional) carries the user's placement controls — ``position``,
    ``size_pct``, ``margin_pct``, ``offset_x``, ``offset_y``. Returns PNG bytes at
    the base image's exact dimensions; base pixels outside the logo bounding box
    are guaranteed identical to the input.
    """
    layout = layout or {}
    base = Image.open(BytesIO(base_png)).convert("RGBA")
    logo = Image.open(BytesIO(logo_png)).convert("RGBA")
    box = logo_placement(
        base.width, base.height, logo.width, logo.height,
        position=layout.get("position", DEFAULT_LOGO_POSITION),
        size_pct=layout.get("size_pct"),
        margin_pct=layout.get("margin_pct"),
        offset_x=int(layout.get("offset_x", 0) or 0),
        offset_y=int(layout.get("offset_y", 0) or 0),
    )
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
