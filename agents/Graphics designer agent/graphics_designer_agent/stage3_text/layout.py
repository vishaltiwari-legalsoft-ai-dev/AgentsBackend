"""Stage-3 absolute-coordinate layout model: anchor math, color resolution
(named tokens + arbitrary hex), legacy placement-key → coordinate conversion,
and the ``resolve_layers`` builder that turns a run's config into an ordered
list of draw layers for ``text_overlay.render_layers``.

This keeps ``text_overlay.py`` focused on drawing pixels. Backward-compat law:
an element with NO explicit coords in ``cfg["layout"]`` is marked ``pinned:
False`` and the renderer positions it via the legacy zone+stack path, so a run
the user never drags renders byte-identically to before.

Coordinate convention: x/y ∈ [0,1] are the fractional position of an element's
ANCHOR point on the canvas; w ∈ (0,1] is max width as a fraction of canvas
width; anchor ∈ ANCHORS.
"""
from __future__ import annotations

import re

from .. import registry
from . import style_options
from .icons import ICON_KEYS
from .shapes import SHAPE_KINDS
from ..tokens import DEFAULT_CTA_PLACEMENT, DEFAULT_TEXT_PLACEMENT

_ALL_SHAPE_KINDS = set(SHAPE_KINDS) | {"icon"}
MAX_SHAPES = 30

ANCHORS = ("tl", "tc", "tr", "ml", "mc", "mr", "bl", "bc", "br")
# Named colour tokens the renderer understands besides arbitrary #RRGGBB hex.
COLOR_TOKENS = ("dark", "white", "gradient", "cta")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def is_valid_color(value) -> bool:
    """True if ``value`` is a known named token or a ``#RRGGBB`` hex string."""
    return isinstance(value, str) and (value in COLOR_TOKENS or bool(_HEX_RE.match(value)))
_HX = {"l": 0.0, "c": 0.5, "r": 1.0, "t": 0.0, "m": 0.5, "b": 1.0}

# Mirrors text_overlay._zone(): 6% margin; left/right zones 42% wide & vertically
# centered; center 60% wide centered; top/bottom span the width.
_MARGIN = 0.06


def anchor_to_xy(x, y, w, h, anchor, cw, ch):
    """Top-left pixel for a ``w``×``h`` box whose ``anchor`` point sits at
    fractional position (``x``, ``y``) on a ``cw``×``ch`` canvas."""
    a = anchor if anchor in ANCHORS else "mc"
    ax = _HX[a[1]]  # horizontal fraction within the box
    ay = _HX[a[0]]  # vertical fraction within the box
    return (round(x * cw - ax * w), round(y * ch - ay * h))


def _hex(h: str):
    h = h.lstrip("#")
    if len(h) != 6:
        raise ValueError(h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def resolve_color(spec, theme: dict, default):
    """Resolve a color spec to a renderer fill.

    ``spec`` is a named token (``dark``/``white``/``gradient``/``cta``) or a
    ``#RRGGBB`` hex string. Invalid/unknown input falls back to ``default``
    (and ``default`` itself falls back to dark) — never raises.
    """
    try:
        if isinstance(spec, str) and spec.startswith("#"):
            return ("solid", _hex(spec))
        if spec == "white":
            return ("solid", theme["white"])
        if spec == "dark":
            return ("solid", theme["dark"])
        if spec == "gradient":
            return ("grad", theme["grad"])
        if spec == "cta":
            return ("grad", theme["cta"])
    except Exception:  # noqa: BLE001 - malformed colour → default, never crash
        pass
    if spec != default:
        return resolve_color(default, theme, "dark")
    return ("solid", theme["dark"])


def default_coords(placement, kind):
    """Fractional coords reproducing the legacy ``_zone`` anchor for a placement
    key. Used for ``auto`` elements (reference + as a starting point for drag)."""
    p = placement or (DEFAULT_CTA_PLACEMENT if kind == "cta" else DEFAULT_TEXT_PLACEMENT)
    if p == "right":
        return {"x": 1 - _MARGIN, "y": 0.5, "w": 0.42, "anchor": "mr"}
    if p == "center":
        return {"x": 0.5, "y": 0.5, "w": 0.60, "anchor": "mc"}
    if p == "top":
        return {"x": 0.5, "y": _MARGIN, "w": 1 - 2 * _MARGIN, "anchor": "tc"}
    if p == "bottom":
        return {"x": 0.5, "y": 1 - _MARGIN, "w": 1 - 2 * _MARGIN, "anchor": "bc"}
    return {"x": _MARGIN, "y": 0.5, "w": 0.42, "anchor": "ml"}  # left (default)


def _coords_for(cfg_layout: dict, elem_id: str, placement: str, kind: str):
    """Return (coords, pinned). Explicit coords in cfg['layout'][id] win and mark
    the element pinned (absolute); otherwise legacy default_coords + pinned=False."""
    entry = (cfg_layout or {}).get(elem_id)
    if isinstance(entry, dict) and all(k in entry for k in ("x", "y")):
        base = default_coords(placement, kind)
        return ({
            "x": float(entry["x"]), "y": float(entry["y"]),
            "w": float(entry.get("w", base["w"])),
            "anchor": entry.get("anchor", base["anchor"]),
        }, True)
    return (default_coords(placement, kind), False)


def clamp_entry(entry: dict) -> dict:
    """Sanitize a user-supplied layout coord entry to safe ranges so a dragged
    element can never push the renderer out of bounds. x/y → [0,1]; w → (0,1];
    anchor → a known anchor (else 'mc')."""
    def f(v, d):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    x = min(1.0, max(0.0, f(entry.get("x"), 0.5)))
    y = min(1.0, max(0.0, f(entry.get("y"), 0.5)))
    w = f(entry.get("w"), 0.42)
    if not (0.0 < w <= 1.0):
        w = 0.42
    a = entry.get("anchor")
    return {"x": x, "y": y, "w": w, "anchor": a if a in ANCHORS else "mc"}


def _clamp01(v, d):
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return d


def sanitize_shapes(items, max_n: int = MAX_SHAPES) -> list[dict]:
    """Validate + normalize the Stage-3 shapes list. Coords clamped; colours
    validated (invalid → safe default, never crash a render); kind must be known
    (else ValueError → the router maps it to a 400); list capped at ``max_n``."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError("shapes must be a list")
    out: list[dict] = []
    for i, sh in enumerate(items[:max_n]):
        if not isinstance(sh, dict):
            raise ValueError("each shape must be an object")
        kind = sh.get("kind", "rect")
        if kind not in _ALL_SHAPE_KINDS:
            raise ValueError(f"unknown shape kind '{kind}'")
        fill = sh.get("fill", "#FFFFFF")
        if not is_valid_color(fill):
            fill = "#FFFFFF"
        stroke = sh.get("stroke")
        if stroke is not None and not is_valid_color(stroke):
            stroke = None
        w = _clamp01(sh.get("w"), 0.3) or 0.3
        h = _clamp01(sh.get("h"), 0.12) or 0.12
        try:
            sw = max(0, min(200, int(sh.get("stroke_w", 0) or 0)))
        except (TypeError, ValueError):
            sw = 0
        try:
            rad = max(0, min(400, int(sh.get("radius", 0) or 0)))
        except (TypeError, ValueError):
            rad = 0
        try:
            z = int(sh.get("z", 5))
        except (TypeError, ValueError):
            z = 5
        icon = sh.get("icon")
        out.append({
            "id": str(sh.get("id") or f"shape-{i}"), "kind": kind,
            "x": _clamp01(sh.get("x"), 0.5), "y": _clamp01(sh.get("y"), 0.5),
            "w": w if 0 < w <= 1 else 0.3, "h": h if 0 < h <= 1 else 0.12,
            "anchor": sh.get("anchor") if sh.get("anchor") in ANCHORS else "mc",
            "fill": fill, "stroke": stroke, "stroke_w": sw, "radius": rad,
            "icon": (icon if icon in ICON_KEYS else "dot") if kind == "icon" else None,
            "text": str(sh.get("text", ""))[:120], "z": z,
        })
    return out


def resolve_layers(run: dict) -> list[dict]:
    """Build the ordered Stage-3 draw layers from a run's config.

    Each text layer carries content + style + coords + ``pinned`` + ``z``. The
    renderer draws ``auto`` (pinned=False) layers via the legacy stacked path and
    ``pinned`` layers at their absolute anchor coords. Shapes (Phase C) append
    with higher ids but lower default z so they sit behind text.
    """
    cfg = run["config"]
    pack = registry.get_pack(run.get("brand_id"))
    tk = cfg.get("tokens") or {}
    styles = cfg.get("element_styles") or {}
    cfg_layout = cfg.get("layout") or {}
    base_font = cfg.get("font") or pack.default_font
    sizes = style_options.DEFAULT_TEXT_SIZE_PCT

    def off(s: dict):
        return (int(s.get("offset_x", 0) or 0), int(s.get("offset_y", 0) or 0))

    hs = styles.get("headline") or {}
    his = styles.get("highlight") or {}
    cs = styles.get("cta") or {}

    layers: list[dict] = []

    coords, pinned = _coords_for(cfg_layout, "headline",
                                 hs.get("placement", DEFAULT_TEXT_PLACEMENT), "text")
    layers.append({
        "type": "text", "id": "headline", "text": tk.get("headline", ""),
        "highlight": tk.get("highlight", ""),
        "font": hs.get("font") or base_font,
        "size_pct": float(hs.get("size_pct", sizes["headline"])),
        "color": hs.get("color", "dark"),
        "highlight_color": his.get("color", "gradient"),
        "placement": hs.get("placement", DEFAULT_TEXT_PLACEMENT),
        "offset": off(hs), "z": 10, "pinned": pinned, **coords,
    })

    raw_subs = cfg.get("subheadings")
    if raw_subs is None:  # legacy run — fall back to the old two subtext tokens
        raw_subs = [{"text": tk.get("subtext1", "")}, {"text": tk.get("subtext2", "")}]
    si = 0
    for s in raw_subs:
        if not (s.get("text") or "").strip():
            continue
        sid = f"subheading-{si}"
        place = s.get("placement", DEFAULT_TEXT_PLACEMENT)
        coords, pinned = _coords_for(cfg_layout, sid, place, "text")
        layers.append({
            "type": "text", "id": sid, "text": s["text"], "highlight": "",
            "font": s.get("font") or base_font,
            "size_pct": float(s.get("size_pct", sizes["subheading"])),
            "color": s.get("color", "dark"), "highlight_color": "gradient",
            "placement": place, "offset": off(s), "z": 11 + si,
            "pinned": pinned, **coords,
        })
        si += 1

    if (tk.get("cta") or "").strip():
        place = cs.get("placement", DEFAULT_CTA_PLACEMENT)
        coords, pinned = _coords_for(cfg_layout, "cta", place, "cta")
        layers.append({
            "type": "cta", "id": "cta", "text": tk.get("cta", ""),
            "font": cs.get("font") or base_font,
            "size_pct": float(cs.get("size_pct", sizes["cta"])),
            "color": cs.get("color", "cta"),
            "placement": place, "offset": off(cs), "z": 20,
            "pinned": pinned, **coords,
        })

    # Optional detail fields → small text layers, only when filled. Default to the
    # bottom corners so they read as a footer; draggable like any text element.
    for fid, dc in (("venue", {"x": 0.06, "y": 0.965, "w": 0.5, "anchor": "bl"}),
                    ("website", {"x": 0.94, "y": 0.965, "w": 0.5, "anchor": "br"})):
        if not (tk.get(fid) or "").strip():
            continue
        coords, pinned = _coords_for(cfg_layout, fid, "bottom", "text")
        if not pinned:
            coords = dc
        layers.append({
            "type": "text", "id": fid, "text": tk.get(fid, ""), "highlight": "",
            "font": base_font, "size_pct": 2.2, "color": "dark",
            "highlight_color": "gradient", "placement": "bottom", "offset": (0, 0),
            "z": 30 if fid == "venue" else 31, "pinned": pinned, **coords,
        })

    # Shapes + infographic elements (always absolute; drawn behind text by z).
    for i, sh in enumerate(cfg.get("shapes") or []):
        layers.append({
            "type": "shape", "id": sh.get("id", f"shape-{i}"),
            "kind": sh.get("kind", "rect"),
            "x": float(sh.get("x", 0.5)), "y": float(sh.get("y", 0.5)),
            "w": float(sh.get("w", 0.3)), "h": float(sh.get("h", 0.12)),
            "anchor": sh.get("anchor", "mc"),
            "fill": sh.get("fill", "#FFFFFF"), "stroke": sh.get("stroke"),
            "stroke_w": int(sh.get("stroke_w", 0) or 0),
            "radius": int(sh.get("radius", 0) or 0),
            "icon": sh.get("icon"), "text": sh.get("text", ""),
            "z": int(sh.get("z", 5)), "pinned": True,
        })

    # Rich elements (emoji / icon / sticker / image) — absolute, drawn by z like
    # shapes. Additive: a run with no ``elements`` is unaffected (backward-compat).
    for i, el in enumerate(cfg.get("elements") or []):
        layers.append({
            "type": "element", "id": el.get("id", f"el-{i}"),
            "kind": el.get("kind", "emoji"),
            "x": float(el.get("x", 0.5)), "y": float(el.get("y", 0.5)),
            "w": float(el.get("w", 0.18)), "h": float(el.get("h", 0.18)),
            "anchor": el.get("anchor", "mc"),
            "z": int(el.get("z", 5) or 5),
            "rotation": float(el.get("rotation", 0.0) or 0.0),
            "opacity": float(el.get("opacity", 1.0) or 1.0),
            "ref": el.get("ref", ""), "fill": el.get("fill", "#1746A2"),
            "pinned": True,
        })

    return layers
