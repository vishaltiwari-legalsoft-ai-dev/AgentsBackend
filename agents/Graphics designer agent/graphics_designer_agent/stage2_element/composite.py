"""Deterministic pixel compositor for uploaded Stage-2 subjects.

Active ONLY when Stage 2 is generated with variant ``"UPLOAD"`` (which requires
``config["subject_asset_ref"]``). Every other variant keeps the byte-identical
AI-generation path — this module is never imported on that path.

The subject is contain-fit into a box that spans ``SUBJECT_WIDTH_FRAC`` of the
base image and anchored to one of the 9 placement cells (the same keys the
prompt-steered ``element_placement`` grid already uses). ``auto`` and unknown
keys fall back to bottom-center — generation can never fail because of the
placement value.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

# Fraction of the base's width/height the subject may occupy (contain-fit).
SUBJECT_WIDTH_FRAC = 0.55
# Breathing room between the subject and the canvas edge, per axis.
MARGIN_FRAC = 0.04

# placement key -> (col, row) on the 3x3 grid; matches STAGE2_PLACEMENTS keys.
_CELL: dict[str, tuple[int, int]] = {
    "top-left": (0, 0), "top-center": (1, 0), "top-right": (2, 0),
    "middle-left": (0, 1), "middle-center": (1, 1), "middle-right": (2, 1),
    "bottom-left": (0, 2), "bottom-center": (1, 2), "bottom-right": (2, 2),
}
_DEFAULT_CELL = (1, 2)  # bottom-center, mirroring the prompt-steered default


def paste_subject(base_png: bytes, subject_bytes: bytes, placement: str | None) -> bytes:
    """Composite ``subject_bytes`` onto ``base_png`` and return PNG bytes."""
    base = Image.open(BytesIO(base_png)).convert("RGBA")
    subject = Image.open(BytesIO(subject_bytes)).convert("RGBA")

    bw, bh = base.size
    max_w = max(1, round(bw * SUBJECT_WIDTH_FRAC))
    max_h = max(1, round(bh * SUBJECT_WIDTH_FRAC))
    scale = min(max_w / subject.width, max_h / subject.height)
    sw = max(1, round(subject.width * scale))
    sh = max(1, round(subject.height * scale))
    subject = subject.resize((sw, sh), Image.LANCZOS)

    col, row = _CELL.get((placement or "").strip().lower(), _DEFAULT_CELL)
    mx = round(bw * MARGIN_FRAC)
    my = round(bh * MARGIN_FRAC)
    x = mx if col == 0 else (bw - sw) // 2 if col == 1 else bw - sw - mx
    y = my if row == 0 else (bh - sh) // 2 if row == 1 else bh - sh - my

    base.alpha_composite(subject, (max(0, x), max(0, y)))
    out = BytesIO()
    base.convert("RGB").save(out, format="PNG")
    return out.getvalue()
