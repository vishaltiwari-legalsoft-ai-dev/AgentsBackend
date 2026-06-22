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


def _draw_cta(canvas, cta: dict, base_w, base_h, theme: _Theme, px_scale=1.0):
    label = cta["text"].rstrip() + "  →"
    font = _font(cta["font"], cta["size_pct"] / 100 * base_w, theme)
    asc, desc = font.getmetrics()
    th = asc + desc
    tw = int(round(font.getlength(label)))
    pad_x, pad_y = int(th * 0.9), int(th * 0.55)
    pw, ph = tw + 2 * pad_x, th + 2 * pad_y
    radius = ph // 2
    mx, my = int(0.06 * base_w), int(0.06 * base_h)

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

    # Soft orange-tinted drop shadow (blurred rounded rect, slight downward offset).
    # The absolute padding/blur/offset scale with the canvas so the shadow keeps
    # the same softness on a hi-res render as in the 1080-px preview.
    pad = max(1, round(40 * px_scale))
    blur = max(1, round(14 * px_scale))
    drop = max(1, round(10 * px_scale))
    shadow = Image.new("RGBA", (pw + 2 * pad, ph + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [pad, pad, pad + pw, pad + ph], radius=radius, fill=theme.cta_grad[1] + (90,))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(shadow, (x - pad, y - pad + drop))

    # Gradient-filled pill.
    grad = _gradient(pw, ph, theme.cta_grad[0], theme.cta_grad[1]).convert("RGBA")
    mask = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, pw - 1, ph - 1], radius=radius, fill=255)
    canvas.paste(grad, (x, y), mask)

    _draw_run(canvas, x + pad_x, y + pad_y, label, font, ("solid", theme.white))


def render_overlay(base_png: bytes, spec: dict, base_w: int, base_h: int,
                   *, px_scale: float = 1.0, pack=None) -> bytes:
    """Render the Stage-3 text overlay onto the base image and return PNG bytes.

    ``spec`` shape::

        {
          "headline":  {"text","highlight","font","size_pct","color",
                        "highlight_color","placement","offset":(x,y)},
          "subheadings":[{"text","font","size_pct","color","placement","offset":(x,y)}],
          "cta":       {"text","font","size_pct","placement","offset":(x,y)},
        }

    ``px_scale`` (>1 when ``base_w/base_h`` exceed the 1080-px UI reference) scales
    the user's pixel nudges + the CTA shadow so a hi-res render matches the preview.
    Font sizes and margins are already percentages of the width, so they scale on
    their own; only the absolute pixel values need ``px_scale``.
    """
    theme = _theme_from_pack(pack) if pack is not None else _default_theme()
    base = Image.open(BytesIO(base_png)).convert("RGBA")
    if base.size != (base_w, base_h):
        base = base.resize((base_w, base_h), Image.LANCZOS)
    canvas = base.copy()

    h = spec["headline"]
    head_fill = _resolve_color(h.get("color", "dark"), theme)
    hl_fill = _resolve_color(h.get("highlight_color", "gradient"), theme)
    elements = [{
        "tokens": _headline_tokens(h["text"], h.get("highlight", "")),
        "font": h["font"], "size_pct": h["size_pct"], "placement": h.get("placement", "left"),
        "offset": h.get("offset", (0, 0)), "line_gap": 1.15,
        "color_for": (lambda t: hl_fill if t[1] else head_fill),
    }]
    for sh in spec.get("subheadings", []):
        fill = _resolve_color(sh.get("color", "dark"), theme)
        elements.append({
            "tokens": [(w, False) for w in sh["text"].split()] or [("", False)],
            "font": sh["font"], "size_pct": sh["size_pct"],
            "placement": sh.get("placement", "left"), "offset": sh.get("offset", (0, 0)),
            "line_gap": 1.4, "color_for": (lambda t, f=fill: f),
        })

    _render_text_elements(canvas, elements, base_w, base_h, theme, px_scale)
    if spec.get("cta", {}).get("text"):
        _draw_cta(canvas, spec["cta"], base_w, base_h, theme, px_scale)

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
