"""Stage-3 Element Library — deterministic emoji / rich-icon / sticker / image
layers. Pure Pillow drawing, no model calls. Shared by social Step 3 and (later)
every multi-frame creative type.

An element is a positioned, scaled, z-ordered layer:
  {id, kind, x, y, w, h, anchor, z, rotation, opacity, ref, fill}
where ``ref`` is the emoji char / icon key / sticker id / uploaded artifact path,
and ``fill`` tints icons (named token or #RRGGBB; ignored by other kinds).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

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


ASSET_DIR = Path(__file__).resolve().parents[1] / "assets"
_EMOJI_DIR = ASSET_DIR / "emoji" / "apple"


def _char_to_codepoints(char: str) -> str:
    """Unified codepoint filename stem for an emoji char, e.g. '😀' -> '1f600'.
    Strips the FE0F variation selector to match the datasource filenames."""
    cps = [f"{ord(c):x}" for c in char if ord(c) != 0xFE0F]
    return "-".join(cps)


def _emoji_png_path(char: str):
    stem = _char_to_codepoints(char)
    p = _EMOJI_DIR / f"{stem}.png"
    return p if p.exists() else None


# A small curated, always-present starter set so the picker is useful even before
# a full catalog file is generated. Extend freely; the picker reads emoji_catalog().
_STARTER_EMOJI = [
    ("😀", "grinning", "smileys"), ("😂", "joy", "smileys"), ("😍", "heart-eyes", "smileys"),
    ("👍", "thumbs-up", "people"), ("🙏", "pray", "people"), ("💪", "muscle", "people"),
    ("🔥", "fire", "symbols"), ("✅", "check", "symbols"), ("⭐", "star", "symbols"),
    ("🚀", "rocket", "travel"), ("💡", "bulb", "objects"), ("📈", "chart-up", "objects"),
    ("❤️", "heart", "symbols"), ("🎯", "target", "activities"), ("💯", "hundred", "symbols"),
]


@lru_cache(maxsize=1)
def emoji_catalog() -> list[dict]:
    """The emoji the picker offers. Reads assets/emoji/catalog.json when present
    (full set), else falls back to the curated starter set. Only lists emoji whose
    PNG is actually vendored, so the picker never shows a broken tile."""
    cat_file = ASSET_DIR / "emoji" / "catalog.json"
    rows: list[dict] = []
    raw = None
    if cat_file.exists():
        try:
            raw = json.loads(cat_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            raw = None
    if raw:
        for r in raw:
            ch = r.get("char", "")
            if _emoji_png_path(ch):
                rows.append({"char": ch, "name": r.get("name", ""),
                             "category": r.get("category", "other"),
                             "file": f"{_char_to_codepoints(ch)}.png"})
    if not rows:
        for ch, name, cat in _STARTER_EMOJI:
            if _emoji_png_path(ch):
                rows.append({"char": ch, "name": name, "category": cat,
                             "file": f"{_char_to_codepoints(ch)}.png"})
    return rows
