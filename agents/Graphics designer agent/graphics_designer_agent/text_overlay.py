"""Stage 3 — deterministic text-overlay renderer (replaces the AI text prompt).

Draws the headline (with an inline highlight colour run), any number of
sub-heading lines, and the CTA pill directly onto the approved Stage-2 image
using the real Causten fonts. Because this is code — not a generative model — the
size, position and colour of every element are EXACT and reproducible, and every
base pixel outside the drawn text is preserved. Mirrors the deterministic
approach of ``compositor.py`` (the Stage-4 logo).

Sizes are a percentage of the canvas WIDTH; positions come from the same
placement keys the UI offers (``variants.TEXT_PLACEMENTS`` / ``CTA_PLACEMENTS``)
plus a per-element pixel nudge. Text elements that share a placement are stacked
in order (headline, then sub-headings) so the default layout reads naturally;
the per-element pixel nudge fine-tunes from there.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import elements as gd_elements
from . import icons, layout, shapes
from .variants import LOCKED_COLORS, font_file

FONT_DIR = Path(__file__).resolve().parents[1] / "Causten Font Family"


def _hex(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


@dataclass(frozen=True)
class _Theme:
    """Per-brand colours + font resolver the renderer draws with. Built from a
    BrandPack, or from the Legal Soft defaults when no pack is supplied."""
    dark: tuple
    white: tuple
    grad_text: tuple          # (start, end) for highlight text
    cta_grad: tuple           # (start, end) for the CTA pill
    fonts_dir: Path
    font_file: Callable[[str], str]


def _default_theme() -> _Theme:
    """Legal Soft theme (back-compat for callers that pass no pack — e.g. tests)."""
    return _Theme(
        dark=_hex(LOCKED_COLORS["text"]),
        white=(255, 255, 255),
        grad_text=(_hex(LOCKED_COLORS["headline_highlight"]["from"]),
                   _hex(LOCKED_COLORS["headline_highlight"]["to"])),
        cta_grad=(_hex(LOCKED_COLORS["cta"]["from"]), _hex(LOCKED_COLORS["cta"]["to"])),
        fonts_dir=FONT_DIR,
        font_file=font_file,
    )


def _theme_from_pack(pack) -> _Theme:
    lc = pack.locked_colors
    return _Theme(
        dark=_hex(lc["text"]),
        white=(255, 255, 255),
        grad_text=(_hex(lc["headline_highlight"]["from"]), _hex(lc["headline_highlight"]["to"])),
        cta_grad=(_hex(lc["cta"]["from"]), _hex(lc["cta"]["to"])),
        fonts_dir=pack.fonts_dir,
        font_file=pack.font_file,
    )


_SCRATCH = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def _font(name: str, px: float, theme: _Theme) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(theme.fonts_dir / theme.font_file(name)), max(6, int(round(px))))


def _resolve_color(key: str, theme: _Theme):
    """Map a colour key to a fill spec the renderer understands."""
    if key == "white":
        return ("solid", theme.white)
    if key == "gradient":
        return ("grad", theme.grad_text)
    return ("solid", theme.dark)


def _gradient(w: int, h: int, c0, c1) -> Image.Image:
    """A fast horizontal gradient (256-px strip resized to size)."""
    strip = Image.new("RGB", (256, 1))
    px = strip.load()
    for i in range(256):
        t = i / 255
        px[i, 0] = tuple(round(c0[k] + (c1[k] - c0[k]) * t) for k in range(3))
    return strip.resize((max(1, w), max(1, h)))


def _draw_run(canvas: Image.Image, x: int, y: int, text: str,
              font: ImageFont.FreeTypeFont, fill) -> None:
    """Draw one text run (solid colour or gradient-across-glyphs) onto canvas."""
    if not text:
        return
    asc, desc = font.getmetrics()
    w = max(1, int(round(font.getlength(text))))
    h = max(1, asc + desc)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((0, 0), text, font=font, fill=(255, 255, 255, 255))
    if fill[0] == "grad":
        color = _gradient(w, h, fill[1][0], fill[1][1]).convert("RGBA")
    else:
        color = Image.new("RGBA", (w, h), fill[1] + (255,))
    canvas.paste(color, (x, y), layer.split()[3])


def _zone(placement: str, base_w: int, base_h: int, mx: int):
    """(max_width, horizontal_align, vertical_align) for a placement key."""
    if placement == "right":
        return (int(0.42 * base_w), "right", "center")
    if placement == "center":
        return (int(0.60 * base_w), "center", "center")
    if placement == "top":
        return (base_w - 2 * mx, "center", "top")
    if placement == "bottom":
        return (base_w - 2 * mx, "center", "bottom")
    return (int(0.42 * base_w), "left", "center")  # left (default)


def _wrap(tokens, font, max_w):
    """Greedy word-wrap. ``tokens`` is a list of ``(word, is_highlight)``."""
    space = font.getlength(" ")
    lines, cur, curw = [], [], 0.0
    for tok in tokens:
        ww = font.getlength(tok[0])
        add = ww + (space if cur else 0)
        if cur and curw + add > max_w:
            lines.append(cur)
            cur, curw = [tok], ww
        else:
            cur.append(tok)
            curw += add
    if cur:
        lines.append(cur)
    return lines or [[("", False)]]


def _headline_tokens(text: str, highlight: str):
    """Split the headline into ``(word, is_highlight)`` runs."""
    if highlight and highlight in text:
        i = text.index(highlight)
        before = text[:i].split()
        hl = highlight.split()
        after = text[i + len(highlight):].split()
        return [(w, False) for w in before] + [(w, True) for w in hl] + [(w, False) for w in after]
    return [(w, False) for w in text.split()]


def _element_layout(elem: dict, base_w: int, base_h: int, mx: int, theme: _Theme):
    font = _font(elem["font"], elem["size_pct"] / 100 * base_w, theme)
    max_w, ha, va = _zone(elem["placement"], base_w, base_h, mx)
    lines = _wrap(elem["tokens"], font, max_w)
    asc, desc = font.getmetrics()
    lh = int((asc + desc) * elem.get("line_gap", 1.3))
    space = font.getlength(" ")
    widths = [sum(font.getlength(t[0]) for t in ln) + space * max(0, len(ln) - 1) for ln in lines]
    return {"font": font, "lines": lines, "lh": lh, "space": space, "ha": ha,
            "va": va, "widths": widths, "height": lh * len(lines)}


def _render_text_elements(canvas, elements, base_w, base_h, theme: _Theme, px_scale=1.0):
    """Place + draw the headline and sub-headings. Elements that share a
    placement are stacked in order so they never silently overlap; the
    per-element pixel nudge then fine-tunes each one.

    ``px_scale`` scales the per-element pixel nudges when the canvas is rendered
    larger than the 1080-px reference the user calibrated against (so a hi-res
    render keeps the same visual position as the preview)."""
    mx, my = int(0.06 * base_w), int(0.06 * base_h)
    gap = int(0.025 * base_h)

    groups: dict[str, list] = {}
    for elem in elements:
        lay = _element_layout(elem, base_w, base_h, mx, theme)
        groups.setdefault(elem["placement"], []).append((elem, lay))

    for items in groups.values():
        va = items[0][1]["va"]
        total = sum(l["height"] for _, l in items) + gap * max(0, len(items) - 1)
        if va == "top":
            y = my
        elif va == "bottom":
            y = base_h - my - total
        else:
            y = (base_h - total) // 2
        for elem, lay in items:
            ey = y + round(elem["offset"][1] * px_scale)
            for i, ln in enumerate(lay["lines"]):
                lw = lay["widths"][i]
                if lay["ha"] == "right":
                    x = base_w - mx - lw
                elif lay["ha"] == "center":
                    x = (base_w - lw) / 2
                else:
                    x = mx
                x += round(elem["offset"][0] * px_scale)
                yy = ey + i * lay["lh"]
                cx = x
                for tok in ln:
                    _draw_run(canvas, int(cx), int(yy), tok[0], lay["font"], elem["color_for"](tok))
                    cx += lay["font"].getlength(tok[0]) + lay["space"]
            y += lay["height"] + gap


def _draw_cta(canvas, cta: dict, base_w, base_h, theme: _Theme, px_scale=1.0, coords=None):
    label = cta["text"].rstrip() + "  →"
    font = _font(cta["font"], cta["size_pct"] / 100 * base_w, theme)
    asc, desc = font.getmetrics()
    th = asc + desc
    tw = int(round(font.getlength(label)))
    pad_x, pad_y = int(th * 0.9), int(th * 0.55)
    pw, ph = tw + 2 * pad_x, th + 2 * pad_y
    radius = ph // 2
    mx, my = int(0.06 * base_w), int(0.06 * base_h)

    if coords is not None:  # pinned → absolute anchor placement
        x, y = layout.anchor_to_xy(coords["x"], coords["y"], pw, ph, coords["anchor"], base_w, base_h)
    else:
        place = cta["placement"]
        if place == "left":
            x = mx
        elif place == "right":
            x = base_w - mx - pw
        else:  # center / top / bottom → horizontally centered
            x = (base_w - pw) // 2
        y = my if place == "top" else base_h - my - ph  # default sits low
    x += round(cta["offset"][0] * px_scale)
    y += round(cta["offset"][1] * px_scale)

    # Pill fill: the "cta" token keeps the locked brand gradient; a solid token
    # ("white"/"dark") or a #RRGGBB hex paints a solid pill. The shadow tints to
    # the fill's representative colour so any colour reads as one coherent button.
    fill = layout.resolve_color(cta.get("color", "cta"), _theme_dict(theme), "cta")
    if fill[0] == "grad":
        c0, c1 = fill[1]
        body = _gradient(pw, ph, c0, c1).convert("RGBA")
        shadow_rgb = c1
    else:
        shadow_rgb = fill[1]
        body = Image.new("RGBA", (pw, ph), shadow_rgb + (255,))

    # Soft drop shadow (blurred rounded rect, slight downward offset). The
    # absolute padding/blur/offset scale with the canvas so the shadow keeps the
    # same softness on a hi-res render as in the 1080-px preview.
    pad = max(1, round(40 * px_scale))
    blur = max(1, round(14 * px_scale))
    drop = max(1, round(10 * px_scale))
    shadow = Image.new("RGBA", (pw + 2 * pad, ph + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [pad, pad, pad + pw, pad + ph], radius=radius, fill=shadow_rgb + (90,))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(shadow, (x - pad, y - pad + drop))

    # Filled pill (gradient or solid), clipped to the rounded-rect mask.
    mask = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, pw - 1, ph - 1], radius=radius, fill=255)
    canvas.paste(body, (x, y), mask)

    _draw_run(canvas, x + pad_x, y + pad_y, label, font, ("solid", theme.white))


def _theme_dict(theme: _Theme) -> dict:
    """View of a ``_Theme`` for ``layout.resolve_color`` (named tokens + hex)."""
    return {"dark": theme.dark, "white": theme.white,
            "grad": theme.grad_text, "cta": theme.cta_grad}


def _headline_element(h: dict, theme: _Theme) -> dict:
    """Legacy element dict (with ``color_for``) for the headline."""
    td = _theme_dict(theme)
    main = layout.resolve_color(h.get("color", "dark"), td, "dark")
    hl = layout.resolve_color(h.get("highlight_color", "gradient"), td, "gradient")
    return {
        "tokens": _headline_tokens(h["text"], h.get("highlight", "")),
        "font": h["font"], "size_pct": h["size_pct"],
        "placement": h.get("placement", "left"), "offset": h.get("offset", (0, 0)),
        "line_gap": 1.15, "color_for": (lambda t, m=main, g=hl: g if t[1] else m),
    }


def _sub_element(sh: dict, theme: _Theme) -> dict:
    """Legacy element dict (with ``color_for``) for one sub-heading."""
    td = _theme_dict(theme)
    fill = layout.resolve_color(sh.get("color", "dark"), td, "dark")
    return {
        "tokens": [(w, False) for w in sh["text"].split()] or [("", False)],
        "font": sh["font"], "size_pct": sh["size_pct"],
        "placement": sh.get("placement", "left"), "offset": sh.get("offset", (0, 0)),
        "line_gap": 1.4, "color_for": (lambda t, f=fill: f),
    }


def _render_parts(canvas, headline, subheadings, cta, base_w, base_h, theme: _Theme, px_scale):
    """Render the (optional) headline + sub-headings via the legacy zone+stack
    path, then the (optional) CTA. Shared by ``render_overlay`` and the auto path
    of ``render_layers`` so both produce identical pixels for the same input."""
    elements = []
    if headline is not None:
        elements.append(_headline_element(headline, theme))
    for sh in subheadings:
        elements.append(_sub_element(sh, theme))
    if elements:
        _render_text_elements(canvas, elements, base_w, base_h, theme, px_scale)
    if cta and cta.get("text"):
        _draw_cta(canvas, cta, base_w, base_h, theme, px_scale)


def _base_canvas(base_png: bytes, base_w: int, base_h: int) -> Image.Image:
    base = Image.open(BytesIO(base_png)).convert("RGBA")
    if base.size != (base_w, base_h):
        base = base.resize((base_w, base_h), Image.LANCZOS)
    return base.copy()


def render_overlay(base_png: bytes, spec: dict, base_w: int, base_h: int,
                   *, px_scale: float = 1.0, pack=None) -> bytes:
    """Render the Stage-3 text overlay onto the base image and return PNG bytes.

    Legacy ``spec`` API (headline / subheadings / cta dicts). Kept intact and
    delegating to the shared part renderer; ``render_layers`` is the coordinate
    (free-drag) API built on the same primitives.
    """
    theme = _theme_from_pack(pack) if pack is not None else _default_theme()
    canvas = _base_canvas(base_png, base_w, base_h)
    _render_parts(canvas, spec["headline"], spec.get("subheadings", []),
                  spec.get("cta"), base_w, base_h, theme, px_scale)
    out = BytesIO()
    canvas.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def _draw_text_abs(canvas, layer: dict, base_w, base_h, theme: _Theme, px_scale):
    """Draw a PINNED text layer at its absolute anchor coords. Honors ``\\n`` as
    hard line breaks (multi-line) and word-wraps each segment to ``w``."""
    td = _theme_dict(theme)
    font = _font(layer["font"], layer["size_pct"] / 100 * base_w, theme)
    max_w = max(1, int(layer["w"] * base_w))
    is_head = layer["id"] == "headline"
    main = layout.resolve_color(layer.get("color", "dark"), td, "dark")
    hl = layout.resolve_color(layer.get("highlight_color", "gradient"), td, "gradient")
    asc, desc = font.getmetrics()
    space = font.getlength(" ")
    lh = int((asc + desc) * (1.15 if is_head else 1.4))

    lines: list = []
    for seg in (layer["text"] or "").split("\n"):
        if is_head and layer.get("highlight"):
            toks = _headline_tokens(seg, layer["highlight"])
        else:
            toks = [(w, False) for w in seg.split()]
        lines.extend(_wrap(toks, font, max_w) if toks else [[("", False)]])

    widths = [sum(font.getlength(t[0]) for t in ln) + space * max(0, len(ln) - 1) for ln in lines]
    box_w = max(widths) if widths else 1
    box_h = lh * len(lines)
    left, top = layout.anchor_to_xy(layer["x"], layer["y"], box_w, box_h, layer["anchor"], base_w, base_h)
    left += round(layer["offset"][0] * px_scale)
    top += round(layer["offset"][1] * px_scale)
    for i, ln in enumerate(lines):
        cx, yy = left, top + i * lh
        for tok in ln:
            fill = hl if (is_head and tok[1]) else main
            _draw_run(canvas, int(cx), int(yy), tok[0], font, fill)
            cx += font.getlength(tok[0]) + space


def _layers_from_spec(spec: dict) -> list[dict]:
    """All-``auto`` layers from a legacy spec (parity helper for render_layers)."""
    layers = []
    h = spec["headline"]
    layers.append({"type": "text", "id": "headline", "text": h["text"],
                   "highlight": h.get("highlight", ""), "font": h["font"],
                   "size_pct": h["size_pct"], "color": h.get("color", "dark"),
                   "highlight_color": h.get("highlight_color", "gradient"),
                   "placement": h.get("placement", "left"), "offset": h.get("offset", (0, 0)),
                   "z": 10, "pinned": False, **layout.default_coords(h.get("placement", "left"), "text")})
    for i, sh in enumerate(spec.get("subheadings", [])):
        layers.append({"type": "text", "id": f"subheading-{i}", "text": sh["text"],
                       "highlight": "", "font": sh["font"], "size_pct": sh["size_pct"],
                       "color": sh.get("color", "dark"), "highlight_color": "gradient",
                       "placement": sh.get("placement", "left"), "offset": sh.get("offset", (0, 0)),
                       "z": 11 + i, "pinned": False,
                       **layout.default_coords(sh.get("placement", "left"), "text")})
    c = spec.get("cta") or {}
    if c.get("text"):
        layers.append({"type": "cta", "id": "cta", "text": c["text"], "font": c["font"],
                       "size_pct": c["size_pct"], "color": c.get("color", "cta"),
                       "placement": c.get("placement", "bottom"), "offset": c.get("offset", (0, 0)),
                       "z": 20, "pinned": False, **layout.default_coords(c.get("placement", "bottom"), "cta")})
    return layers


def _parts_from_layers(layers: list[dict]):
    """Reconstruct (headline|None, subheadings, cta|None) from auto text/cta layers."""
    head = next((l for l in layers if l["id"] == "headline" and l["type"] == "text"), None)
    subs = [l for l in layers if l["type"] == "text" and l["id"].startswith("subheading-")]
    cta = next((l for l in layers if l["type"] == "cta"), None)
    h = None
    if head:
        h = {"text": head["text"], "highlight": head.get("highlight", ""), "font": head["font"],
             "size_pct": head["size_pct"], "color": head["color"],
             "highlight_color": head.get("highlight_color", "gradient"),
             "placement": head["placement"], "offset": head["offset"]}
    sh = [{"text": s["text"], "font": s["font"], "size_pct": s["size_pct"], "color": s["color"],
           "placement": s["placement"], "offset": s["offset"]} for s in subs]
    c = None
    if cta:
        c = {"text": cta["text"], "font": cta["font"], "size_pct": cta["size_pct"],
             "placement": cta["placement"], "offset": cta["offset"]}
    return h, sh, c


def _solid_rgb(fill):
    """Representative RGB for a renderer fill (gradient → its start colour)."""
    return fill[1][0] if fill[0] == "grad" else fill[1]


def _draw_callout_text(canvas, text: str, box, theme: _Theme):
    """Single line of dark text centered in a callout box (best-effort)."""
    x0, y0, x1, y1 = box
    ph = y1 - y0
    try:
        font = _font("Causten Bold", ph * 0.4, theme)
    except Exception:  # noqa: BLE001 - brand without that face → skip the text
        return
    tw = font.getlength(text)
    asc, desc = font.getmetrics()
    cx = x0 + ((x1 - x0) - tw) / 2
    cy = y0 + (ph - (asc + desc)) / 2
    _draw_run(canvas, int(cx), int(cy), text, font, ("solid", theme.dark))


def _draw_shape(canvas, l: dict, base_w, base_h, theme: _Theme, px_scale):
    """Draw a PINNED shape / icon / divider / callout layer at its anchor box."""
    td = _theme_dict(theme)
    pw = max(1, int(l["w"] * base_w))
    ph = max(1, int(l["h"] * base_h))
    left, top = layout.anchor_to_xy(l["x"], l["y"], pw, ph, l["anchor"], base_w, base_h)
    box = (left, top, left + pw, top + ph)
    fill = _solid_rgb(layout.resolve_color(l.get("fill", "#FFFFFF"), td, "white"))
    stroke = _solid_rgb(layout.resolve_color(l["stroke"], td, "dark")) if l.get("stroke") else None
    sw = round((l.get("stroke_w") or 0) * px_scale)
    if l["kind"] == "icon":
        icons.draw_icon(canvas, l.get("icon") or "dot", box, fill)
    else:
        shapes.draw(canvas, l["kind"], box, fill=fill, stroke=stroke, stroke_w=sw,
                    radius=round(l.get("radius", 0) * px_scale))
        if l["kind"] == "callout" and (l.get("text") or "").strip():
            _draw_callout_text(canvas, l["text"], box, theme)


def render_layers(base_png: bytes, layers: list[dict], base_w: int, base_h: int,
                  *, px_scale: float = 1.0, pack=None, image_loader=None) -> bytes:
    """Render Stage-3 from coordinate layers. Draw order: shapes and elements
    (behind, by z), then ``auto`` text via the legacy zone+stack path
    (byte-identical to ``render_overlay``), then ``pinned`` text/cta at
    absolute coords by z."""
    theme = _theme_from_pack(pack) if pack is not None else _default_theme()
    canvas = _base_canvas(base_png, base_w, base_h)

    shape_layers = [l for l in layers if l.get("type") == "shape"]
    element_layers = [l for l in layers if l.get("type") == "element"]
    text_layers = [l for l in layers if l.get("type") not in ("shape", "element")]
    for l in sorted(shape_layers, key=lambda x: x.get("z", 0)):
        _draw_shape(canvas, l, base_w, base_h, theme, px_scale)
    for l in sorted(element_layers, key=lambda x: x.get("z", 0)):
        gd_elements.draw_element(canvas, l, base_w, base_h, image_loader=image_loader)

    auto = [l for l in text_layers if not l.get("pinned")]
    pinned = [l for l in text_layers if l.get("pinned")]
    if auto:
        h, sh, c = _parts_from_layers(auto)
        _render_parts(canvas, h, sh, c, base_w, base_h, theme, px_scale)
    for l in sorted(pinned, key=lambda x: x.get("z", 0)):
        if l["type"] == "cta":
            _draw_cta(canvas, {"text": l["text"], "font": l["font"], "size_pct": l["size_pct"],
                               "placement": l["placement"], "offset": l["offset"]},
                      base_w, base_h, theme, px_scale,
                      coords={"x": l["x"], "y": l["y"], "anchor": l["anchor"]})
        else:
            _draw_text_abs(canvas, l, base_w, base_h, theme, px_scale)
    out = BytesIO()
    canvas.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def overlay_spec_summary(spec: dict) -> str:
    """Human-readable layout summary for the prompt-audit panel (no LLM prompt
    exists for the deterministic Stage-3 path)."""
    lines = ["DETERMINISTIC TEXT OVERLAY (rendered with Causten fonts — exact size & position)\n"]
    h = spec["headline"]
    lines.append(
        f"HEADLINE  “{h['text']}”\n"
        f"  font {h['font']} · size {h['size_pct']}% · colour {h.get('color')} · "
        f"placement {h.get('placement')} · nudge {tuple(h.get('offset', (0, 0)))}"
    )
    if h.get("highlight"):
        lines.append(f"  highlight “{h['highlight']}” · colour {h.get('highlight_color')}")
    for i, sh in enumerate(spec.get("subheadings", []), 1):
        lines.append(
            f"SUB-HEADING {i}  “{sh['text']}”\n"
            f"  font {sh['font']} · size {sh['size_pct']}% · colour {sh.get('color')} · "
            f"placement {sh.get('placement')} · nudge {tuple(sh.get('offset', (0, 0)))}"
        )
    c = spec.get("cta", {})
    if c.get("text"):
        lines.append(
            f"CTA  “{c['text']}”\n"
            f"  font {c['font']} · size {c['size_pct']}% · placement {c.get('placement')} · "
            f"nudge {tuple(c.get('offset', (0, 0)))} · locked orange pill"
        )
    return "\n".join(lines)
