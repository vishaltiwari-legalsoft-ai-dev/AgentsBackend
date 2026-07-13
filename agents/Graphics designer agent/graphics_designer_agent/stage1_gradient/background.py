"""Deterministic cover-fit for uploaded Stage-1 backgrounds (variant ``UPLOAD``).

Active ONLY when Stage 1 is generated with variant ``"UPLOAD"`` (which requires
``config["background_asset_ref"]``). Every other variant keeps the byte-identical
AI-generation path — this module is never imported on that path.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image


def cover_fit(image_bytes: bytes, canvas_w: int, canvas_h: int, *, max_width: int = 4096) -> bytes:
    """Fit an uploaded photo to the run's canvas shape: scale-to-cover, then
    center-crop. The output keeps the canvas aspect ratio at the source's
    native width (never below the canvas preset, bounded by ``max_width``) so
    a high-res photo keeps its resolution through Stages 2-4."""
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    scale = min(max(1.0, img.width / canvas_w), max_width / canvas_w)
    tw, th = round(canvas_w * scale), round(canvas_h * scale)
    s = max(tw / img.width, th / img.height)
    rw, rh = max(1, round(img.width * s)), max(1, round(img.height * s))
    img = img.resize((rw, rh), Image.LANCZOS)
    left, top = (rw - tw) // 2, (rh - th) // 2
    img = img.crop((left, top, left + tw, top + th))
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
