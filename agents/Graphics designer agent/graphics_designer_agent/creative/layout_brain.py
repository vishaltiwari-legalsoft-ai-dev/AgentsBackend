"""Layout brain — decide WHERE a slide's text block should sit, per image.

The carousel generates a distinct image per slide, so the subject lands in a
different place each time. A fixed text placement therefore collides with the
subject on some slides (text over a face/body). This module looks at the finished
slide and picks the placement that keeps the text in the clean negative space.

Strict (never decided here): the brand font and the highlight gradient — those
stay locked. The brain only chooses, per slide:

- ``placement`` — which zone the text occupies (one of the overlay's placement
  keys: ``left`` / ``right`` / ``top`` / ``bottom``), away from the subject.
- ``color``     — ``"dark"`` text on a light area, ``"white"`` on a dark/busy area,
  so the copy stays legible.

A vision model makes the call (it actually "sees" where the subject is). If no
OpenRouter key is configured, or the call fails, a deterministic pixel analysis
(edge-density negative-space finder + brightness) takes over, so a placement is
always produced and generation never breaks.
"""

from __future__ import annotations

import io
import json
import logging
import re
from typing import Optional

logger = logging.getLogger("graphics_designer.creative.layout_brain")

# Zones the overlay renderer understands. The vision model is steered toward the
# four edge zones (center overlaps the subject); ``center`` stays valid for the
# fallback's completeness only.
_VALID_PLACEMENTS = ("left", "right", "top", "bottom", "center")
# Downscale before sending to the vision model — placement only needs the gist,
# and a ~768px image is far cheaper/faster than the native 4K base.
_VISION_MAX_DIM = 768


def decide_placement(image_bytes: bytes, *, headline: str = "", body: str = "",
                     has_cta: bool = False) -> dict:
    """Return ``{"placement", "color", "source"}`` for the text on THIS image.

    Tries the vision model first; falls back to deterministic pixel analysis."""
    return (
        _vision_placement(image_bytes, headline, body, has_cta)
        or _pixel_placement(image_bytes)
    )


def _vision_placement(image_bytes: bytes, headline: str, body: str,
                      has_cta: bool) -> Optional[dict]:
    try:
        from app.services.openrouter import analyze_images  # lazy — works without app
    except Exception:
        logger.debug("OpenRouter not importable; using pixel placement")
        return None

    small = _downscale_png(image_bytes, _VISION_MAX_DIM)
    pieces = ["a bold headline"]
    if body:
        pieces.append("body copy")
    if has_cta:
        pieces.append("a CTA button")
    block = ", ".join(pieces)
    prompt = (
        "You are a senior art director laying out a square (1:1) social ad. The "
        "attached image is the FINISHED background — a brand gradient with a "
        "subject (usually a person or object). A text block (" + block + ") must "
        "be placed in the CLEAN negative space so it NEVER overlaps the subject's "
        "face or body and the composition looks professionally balanced.\n\n"
        "Pick the zone with the most empty space, and decide whether the text "
        "should be dark (over a light area) or light (over a dark or busy area) "
        "for maximum legibility.\n\n"
        'Reply with ONLY minified JSON: {"zone":"left|right|top|bottom",'
        '"color":"dark|light"}. No prose.'
    )
    try:
        out = analyze_images(prompt, [(small, "image/png")])
        match = re.search(r"\{.*\}", out, re.S)
        if not match:
            return None
        data = json.loads(match.group(0))
        zone = str(data.get("zone", "")).strip().lower()
        if zone not in _VALID_PLACEMENTS:
            return None
        color = "white" if str(data.get("color", "")).strip().lower().startswith("l") else "dark"
        return {"placement": zone, "color": color, "source": "vision"}
    except Exception:
        logger.warning("vision placement failed; using pixel fallback", exc_info=True)
        return None


def _pixel_placement(image_bytes: bytes) -> dict:
    """Deterministic negative-space finder. Splits the image into left/right (and
    top/bottom) regions, scores each by edge density (the subject is busy, empty
    space is calm), and places the text in the calmest edge zone. Text colour is
    chosen from that zone's mean brightness."""
    try:
        from PIL import Image, ImageFilter, ImageStat
    except Exception:
        return {"placement": "left", "color": "dark", "source": "fallback"}
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return {"placement": "left", "color": "dark", "source": "fallback"}

    w, h = img.size
    edges = img.convert("L").filter(ImageFilter.FIND_EDGES)

    def busy(box):
        return ImageStat.Stat(edges.crop(box)).mean[0]

    candidates = {
        "left": busy((0, 0, w // 2, h)),
        "right": busy((w // 2, 0, w, h)),
        "top": busy((0, 0, w, h // 2)),
        "bottom": busy((0, h // 2, w, h)),
    }
    placement = min(candidates, key=candidates.get)  # the calmest (emptiest) zone
    box = {
        "left": (0, 0, w // 2, h),
        "right": (w // 2, 0, w, h),
        "top": (0, 0, w, h // 2),
        "bottom": (0, h // 2, w, h),
    }[placement]
    brightness = ImageStat.Stat(img.crop(box).convert("L")).mean[0]
    color = "dark" if brightness >= 140 else "white"
    return {"placement": placement, "color": color, "source": "fallback"}


def _downscale_png(image_bytes: bytes, max_dim: int) -> bytes:
    """Shrink an image so its longest side is ``max_dim`` (cheaper vision call).
    Returns the original bytes unchanged if Pillow isn't available or it's small."""
    try:
        from PIL import Image
    except Exception:
        return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if max(img.size) <= max_dim:
            return image_bytes
        img = img.convert("RGB")
        img.thumbnail((max_dim, max_dim))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return image_bytes
