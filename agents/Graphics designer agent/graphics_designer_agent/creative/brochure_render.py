# creative/brochure_render.py
"""Brochure render primitives — the visual vocabulary of a real brochure.

Pure Pillow drawing: a calm gradient background and the card/pill/bullet/avatar
shapes that carry the design. No BrandPack, no image provider — callers pass
resolved RGB colors and a font-loader, so every helper is deterministic and
unit-testable offline. Templates (``brochure_layout``) compose these into pages.
"""

from __future__ import annotations

import io
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

RGB = tuple[int, int, int]
FontLoader = Callable[[int, Optional[str]], ImageFont.FreeTypeFont]

# Portrait 4:5 page at ~print resolution. One source of truth for the page size.
_BROCHURE_PAGE: tuple[int, int] = (1240, 1550)


def _lerp(a: RGB, b: RGB, t: float) -> RGB:
    return (round(a[0] + (b[0] - a[0]) * t),
            round(a[1] + (b[1] - a[1]) * t),
            round(a[2] + (b[2] - a[2]) * t))


def calm_background(size: tuple[int, int], light: RGB, deep: RGB, *,
                    motif_png: Optional[bytes] = None,
                    motif_opacity: float = 0.12) -> Image.Image:
    """A calm vertical gradient from a near-white top to a softly brand-tinted
    bottom (never a busy photo). ``motif_png`` is composited faintly if supplied —
    the imagery becomes a ghosted backdrop, never the bed the text sits on."""
    w, h = size
    # Keep the bed light: bottom is a 18% mix toward the deep brand colour.
    top = _lerp(light, (255, 255, 255), 0.35)
    bottom = _lerp(light, deep, 0.18)
    bg = Image.new("RGB", size, top)
    draw = ImageDraw.Draw(bg)
    for y in range(h):
        draw.line([(0, y), (w, y)], fill=_lerp(top, bottom, y / max(1, h - 1)))
    bg = bg.convert("RGBA")
    if motif_png:
        try:
            motif = Image.open(io.BytesIO(motif_png)).convert("RGBA")
            motif = motif.resize(size, Image.LANCZOS)
            alpha = motif.split()[3].point(lambda a: int(a * max(0.0, min(1.0, motif_opacity))))
            motif.putalpha(alpha)
            bg.alpha_composite(motif)
        except Exception:  # noqa: BLE001 - motif is decorative; never block a page
            pass
    return bg


def draw_card(canvas: Image.Image, box, *, fill: RGB = (255, 255, 255),
              radius: int = 28, shadow: bool = True, shadow_opacity: int = 45,
              stroke: Optional[RGB] = None, stroke_w: int = 0) -> None:
    """A rounded-rectangle card with a soft drop shadow — the legibility surface
    text sits on. Shadow is a blurred dark rounded rect, offset down/right."""
    x0, y0, x1, y1 = [int(v) for v in box]
    if shadow:
        blur, drop = 18, 10
        sh = Image.new("RGBA", (canvas.width, canvas.height), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle(
            [x0, y0 + drop, x1, y1 + drop], radius=radius,
            fill=(20, 25, 40, max(0, min(255, shadow_opacity))))
        canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(blur)))
    d = ImageDraw.Draw(canvas, "RGBA")
    d.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill + (255,),
                        outline=(stroke + (255,)) if stroke else None,
                        width=max(0, int(stroke_w)))


def draw_pill(canvas: Image.Image, origin, label: str, font, *, fill: RGB,
              text_color: RGB = (255, 255, 255), pad_x: int = 22, pad_y: int = 12,
              radius: Optional[int] = None) -> tuple[int, int]:
    """A small solid rounded label (the colored title chip). Returns its (w, h)."""
    x, y = int(origin[0]), int(origin[1])
    asc, desc = font.getmetrics()
    tw = int(font.getlength(label))
    th = asc + desc
    pw, ph = tw + 2 * pad_x, th + 2 * pad_y
    r = ph // 2 if radius is None else radius
    d = ImageDraw.Draw(canvas, "RGBA")
    d.rounded_rectangle([x, y, x + pw, y + ph], radius=r, fill=fill + (255,))
    d.text((x + pad_x, y + pad_y), label, font=font, fill=text_color + (255,))
    return pw, ph


def _wrap(font, text: str, max_w: int) -> list[str]:
    words, lines, cur = (text or "").split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if font.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def draw_heading(canvas: Image.Image, origin, text: str, font, color: RGB, *,
                 max_w: int, highlight: Optional[str] = None,
                 highlight_color: Optional[RGB] = None, line_gap: float = 1.12) -> int:
    """A bold section heading; the ``highlight`` substring (if present) is drawn in
    ``highlight_color``. Returns the bottom y."""
    x, y = int(origin[0]), int(origin[1])
    asc, desc = font.getmetrics()
    lh = int((asc + desc) * line_gap)
    d = ImageDraw.Draw(canvas, "RGBA")
    for line in _wrap(font, text, max_w):
        cx = x
        if highlight and highlight in line:
            i = line.index(highlight)
            for seg, col in ((line[:i], color), (highlight, highlight_color or color),
                             (line[i + len(highlight):], color)):
                if not seg:
                    continue
                d.text((cx, y), seg, font=font, fill=col + (255,))
                cx += int(font.getlength(seg))
        else:
            d.text((cx, y), line, font=font, fill=color + (255,))
        y += lh
    return y


def draw_paragraph(canvas: Image.Image, origin, text: str, font, color: RGB, *,
                   max_w: int, line_gap: float = 1.4) -> int:
    x, y = int(origin[0]), int(origin[1])
    asc, desc = font.getmetrics()
    lh = int((asc + desc) * line_gap)
    d = ImageDraw.Draw(canvas, "RGBA")
    for line in _wrap(font, text, max_w):
        d.text((x, y), line, font=font, fill=color + (255,))
        y += lh
    return y


def draw_bullets(canvas: Image.Image, origin, items, font, color: RGB, *,
                 accent: RGB, max_w: int, line_gap: float = 1.45, dot_r: int = 6) -> int:
    """Accent dot + wrapped text per item. Returns the bottom y."""
    x, y = int(origin[0]), int(origin[1])
    asc, desc = font.getmetrics()
    lh = int((asc + desc) * line_gap)
    text_x = x + dot_r * 2 + 14
    d = ImageDraw.Draw(canvas, "RGBA")
    for item in items or []:
        lines = _wrap(font, item, max_w - (text_x - x))
        cy = y + (asc + desc) // 2
        d.ellipse([x, cy - dot_r, x + 2 * dot_r, cy + dot_r], fill=accent + (255,))
        for line in lines:
            d.text((text_x, y), line, font=font, fill=color + (255,))
            y += lh
    return y


def draw_circular(canvas: Image.Image, center, radius: int, *,
                  image_png: Optional[bytes] = None, initials: str = "",
                  fill: RGB = (23, 70, 162), text_color: RGB = (255, 255, 255),
                  font=None) -> None:
    """A circular avatar: a photo masked to a circle if ``image_png`` is given,
    else a filled circle with centered initials (the offline placeholder)."""
    cx, cy = int(center[0]), int(center[1])
    box = [cx - radius, cy - radius, cx + radius, cy + radius]
    if image_png:
        try:
            mask = Image.new("L", (2 * radius, 2 * radius), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, 2 * radius - 1, 2 * radius - 1], fill=255)
            im = Image.open(io.BytesIO(image_png)).convert("RGBA").resize(
                (2 * radius, 2 * radius), Image.LANCZOS)
            canvas.paste(im, (cx - radius, cy - radius), mask)
            return
        except Exception:  # noqa: BLE001 - fall through to the initials placeholder
            pass
    ImageDraw.Draw(canvas, "RGBA").ellipse(box, fill=fill + (255,))
    if initials and font is not None:
        asc, desc = font.getmetrics()
        tw = int(font.getlength(initials))
        ImageDraw.Draw(canvas, "RGBA").text(
            (cx - tw // 2, cy - (asc + desc) // 2), initials, font=font,
            fill=text_color + (255,))
