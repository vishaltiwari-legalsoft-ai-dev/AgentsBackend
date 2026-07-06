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


ASSET_DIR = Path(__file__).resolve().parents[2] / "assets"  # <agent root>/assets
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


_ICON_DIR = ASSET_DIR / "icons"
_STICKER_DIR = ASSET_DIR / "stickers"


@lru_cache(maxsize=1)
def icon_catalog() -> list[str]:
    if not _ICON_DIR.exists():
        return []
    return sorted(p.stem for p in _ICON_DIR.glob("*.svg"))


@lru_cache(maxsize=1)
def sticker_catalog() -> list[str]:
    if not _STICKER_DIR.exists():
        return []
    return sorted(p.stem for p in _STICKER_DIR.glob("*.svg"))


def _icon_svg_path(key: str):
    p = _ICON_DIR / f"{key}.svg"
    return p if p.exists() else None


def _sticker_svg_path(sid: str):
    p = _STICKER_DIR / f"{sid}.svg"
    return p if p.exists() else None


from io import BytesIO

from PIL import Image


def _hex_rgb(value: str):
    v = (value or "").lstrip("#")
    if len(v) != 6:
        return (23, 70, 162)
    return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))


def _raster_svg(path, px_w: int, px_h: int, tint=None):
    """Rasterize an SVG to an RGBA image at px size, optional solid tint.
    Uses cairosvg when available; falls back to None (caller no-ops)."""
    try:
        import cairosvg
    except Exception:
        return None
    try:
        png = cairosvg.svg2png(url=str(path), output_width=px_w, output_height=px_h)
        img = Image.open(BytesIO(png)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None
    if tint is not None:
        r, g, b = _hex_rgb(tint) if isinstance(tint, str) else tint
        solid = Image.new("RGBA", img.size, (r, g, b, 0))
        solid.putalpha(img.split()[3])  # keep the SVG's alpha, replace colour
        return solid
    return img


def _load_element_image(layer: dict, px_w: int, px_h: int, image_loader):
    kind = layer["kind"]
    if kind == "emoji":
        p = _emoji_png_path(layer["ref"])
        if not p:
            return None
        return Image.open(p).convert("RGBA").resize((px_w, px_h), Image.LANCZOS)
    if kind == "icon":
        p = _icon_svg_path(layer["ref"])
        return _raster_svg(p, px_w, px_h, tint=layer.get("fill")) if p else None
    if kind == "sticker":
        p = _sticker_svg_path(layer["ref"])
        return _raster_svg(p, px_w, px_h) if p else None
    if kind == "image":
        if not image_loader:
            return None
        data = image_loader(layer["ref"])
        if not data:
            return None
        return Image.open(BytesIO(data)).convert("RGBA").resize((px_w, px_h), Image.LANCZOS)
    return None


def draw_element(canvas, layer: dict, base_w: int, base_h: int, *, image_loader=None) -> None:
    """Draw one element layer onto the RGBA canvas. Fully best-effort — any failure
    (missing asset, no rasterizer, bad bytes) is a silent no-op so a stale element
    can never crash a render."""
    try:
        pw = max(1, int(layer["w"] * base_w))
        ph = max(1, int(layer["h"] * base_h))
        img = _load_element_image(layer, pw, ph, image_loader)
        if img is None:
            return
        opacity = float(layer.get("opacity", 1.0))
        if opacity < 1.0:
            a = img.split()[3].point(lambda v: int(v * opacity))
            img.putalpha(a)
        rot = float(layer.get("rotation", 0.0))
        if rot:
            img = img.rotate(-rot, expand=True, resample=Image.BICUBIC)
        left, top = layout.anchor_to_xy(layer["x"], layer["y"], img.width, img.height,
                                        layer.get("anchor", "mc"), base_w, base_h)
        canvas.alpha_composite(img, (int(left), int(top)))
    except Exception:  # noqa: BLE001 - an element never breaks the whole render
        return
