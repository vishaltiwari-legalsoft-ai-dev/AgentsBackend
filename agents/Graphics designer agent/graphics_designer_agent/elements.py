"""Stage-3 Element Library — deterministic emoji / rich-icon / sticker / image
layers. Pure Pillow drawing, no model calls. Shared by social Step 3 and (later)
every multi-frame creative type.

An element is a positioned, scaled, z-ordered layer:
  {id, kind, x, y, w, h, anchor, z, rotation, opacity, ref, fill}
where ``ref`` is the emoji char / icon key / sticker id / uploaded artifact path,
and ``fill`` tints icons (named token or #RRGGBB; ignored by other kinds).
"""
from __future__ import annotations

from . import layout

ELEMENT_KINDS = ("emoji", "icon", "sticker", "image")
MAX_ELEMENTS = 30
_DEFAULT_ICON_FILL = "#1746A2"  # brand blue


def sanitize_elements(items, max_n: int = MAX_ELEMENTS) -> list[dict]:
    """Validate + normalize the Stage-3 elements list. Coords clamped; kind must be
    known and ``ref`` present (else ValueError → router maps to 400). Capped."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError("elements must be a list")
    out: list[dict] = []
    for i, el in enumerate(items[:max_n]):
        if not isinstance(el, dict):
            raise ValueError("each element must be an object")
        kind = el.get("kind")
        if kind not in ELEMENT_KINDS:
            raise ValueError(f"unknown element kind '{kind}'")
        ref = el.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"element {i} missing 'ref'")
        w = layout._clamp01(el.get("w"), 0.18) or 0.18
        h = layout._clamp01(el.get("h"), 0.18) or 0.18
        fill = el.get("fill", _DEFAULT_ICON_FILL)
        if not layout.is_valid_color(fill):
            fill = _DEFAULT_ICON_FILL
        try:
            z = int(el.get("z", 5))
        except (TypeError, ValueError):
            z = 5
        try:
            rot = float(el.get("rotation", 0.0))
        except (TypeError, ValueError):
            rot = 0.0
        opacity = layout._clamp01(el.get("opacity"), 1.0)
        out.append({
            "id": str(el.get("id") or f"el-{i}"),
            "kind": kind,
            "x": layout._clamp01(el.get("x"), 0.5),
            "y": layout._clamp01(el.get("y"), 0.5),
            "w": w if 0 < w <= 1 else 0.18,
            "h": h if 0 < h <= 1 else 0.18,
            "anchor": el.get("anchor") if el.get("anchor") in layout.ANCHORS else "mc",
            "z": z,
            "rotation": max(-180.0, min(180.0, rot)),
            "opacity": opacity if 0 <= opacity <= 1 else 1.0,
            "ref": ref.strip(),
            "fill": fill,
        })
    return out
