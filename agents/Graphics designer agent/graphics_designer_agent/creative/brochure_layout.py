# creative/brochure_layout.py
"""Brochure visual grammar — a column grid and the page templates that arrange
cards on it. Each template is a pure function (content + palette + grid →
primitive calls) returning a finished page PNG. No I/O, no provider.
"""

from __future__ import annotations

import io
from typing import Callable, Optional

from PIL import Image

from . import brochure_render as br


def _rgb(hex_str: str) -> br.RGB:
    v = (hex_str or "").lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        return (15, 15, 15)
    try:
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except ValueError:
        return (15, 15, 15)


class Grid:
    """A simple column grid over the page's content area (inside the margins)."""

    def __init__(self, w: int, h: int, *, margin: int = 80, cols: int = 12, gutter: int = 24):
        self.w, self.h, self.margin, self.cols, self.gutter = w, h, margin, cols, gutter
        self.content_w = w - 2 * margin
        self.col_w = (self.content_w - gutter * (cols - 1)) / cols

    def span(self, start: int, count: int) -> tuple[int, int]:
        left = self.margin + start * (self.col_w + self.gutter)
        width = count * self.col_w + (count - 1) * self.gutter
        return int(left), int(width)

    def columns(self, n: int) -> list[tuple[int, int]]:
        """``n`` equal columns across the content width, each ``(left_x, width)``."""
        gut = self.gutter
        width = (self.content_w - gut * (n - 1)) / n
        return [(int(self.margin + i * (width + gut)), int(width)) for i in range(n)]


class _Ctx:
    """Resolved drawing context handed to every template."""

    def __init__(self, canvas, grid, palette, font_loader, logo_png):
        self.c = canvas
        self.g = grid
        self.deep = _rgb(palette["deep"])
        self.accent = _rgb(palette["accent"])
        self.ink = _rgb(palette["text"])
        self.light = _rgb(palette["light"])
        self.font = font_loader
        self.logo = logo_png


# --------------------------------------------------------------------------- #
# Templates — each draws onto ctx.c and returns None.
# --------------------------------------------------------------------------- #

def _t_cover(p: dict, x: _Ctx) -> None:
    g = x.g
    title_f = x.font(int(g.w * 0.072), "Causten Bold")
    sub_f = x.font(int(g.w * 0.030), None)
    left, width = g.span(0, 12)
    y = int(g.h * 0.30)
    y = br.draw_heading(x.c, (left, y), p.get("heading", ""), title_f, x.deep,
                        max_w=width, highlight=p.get("highlight") or None,
                        highlight_color=x.accent)
    if p.get("subtitle"):
        br.draw_paragraph(x.c, (left, y + 24), p["subtitle"], sub_f, x.accent, max_w=width)


def _t_card_grid(p: dict, x: _Ctx) -> None:
    g = x.g
    head_f = x.font(int(g.w * 0.050), "Causten Bold")
    title_f = x.font(int(g.w * 0.026), "Causten Bold")
    bullet_f = x.font(int(g.w * 0.019), None)
    top = br.draw_heading(x.c, g.span(0, 12)[:1] + (int(g.h * 0.06),), p.get("heading", ""),
                          head_f, x.deep, max_w=g.span(0, 12)[1],
                          highlight=p.get("highlight") or None, highlight_color=x.accent)
    cards = p.get("cards") or []
    cols = g.columns(2)
    card_h = int(g.h * 0.20)
    pad = 28
    for i, card in enumerate(cards[:6]):
        col_x, col_w = cols[i % 2]
        row = i // 2
        cy = top + 40 + row * (card_h + 30)
        br.draw_card(x.c, (col_x, cy, col_x + col_w, cy + card_h))
        br.draw_circular(x.c, (col_x + pad + 34, cy + pad + 34), 34,
                         initials=card.get("initials", ""), fill=x.deep,
                         font=x.font(int(g.w * 0.022), "Causten Bold"))
        tx = col_x + pad + 88
        br.draw_pill(x.c, (tx, cy + pad), card.get("title", ""),
                     x.font(int(g.w * 0.017), "Causten Bold"), fill=x.accent)
        br.draw_bullets(x.c, (col_x + pad, cy + pad + 78), card.get("bullets") or [],
                        bullet_f, x.ink, accent=x.accent, max_w=col_w - 2 * pad)


def _t_steps(p: dict, x: _Ctx) -> None:
    g = x.g
    head_f = x.font(int(g.w * 0.050), "Causten Bold")
    title_f = x.font(int(g.w * 0.024), "Causten Bold")
    body_f = x.font(int(g.w * 0.018), None)
    top = br.draw_heading(x.c, (g.margin, int(g.h * 0.06)), p.get("heading", ""), head_f,
                          x.deep, max_w=g.content_w, highlight=p.get("highlight") or None,
                          highlight_color=x.accent)
    steps = (p.get("steps") or [])[:3]
    cols = g.columns(max(1, len(steps)))
    card_h = int(g.h * 0.34)
    cy = top + 50
    for i, step in enumerate(steps):
        col_x, col_w = cols[i]
        br.draw_card(x.c, (col_x, cy, col_x + col_w, cy + card_h))
        br.draw_circular(x.c, (col_x + 44, cy + 48), 30, initials=str(i + 1),
                         fill=x.accent, font=x.font(int(g.w * 0.026), "Causten Bold"))
        ty = br.draw_heading(x.c, (col_x + 28, cy + 100), step.get("title", ""), title_f,
                             x.deep, max_w=col_w - 56)
        br.draw_paragraph(x.c, (col_x + 28, ty + 12), step.get("desc", ""), body_f,
                          x.ink, max_w=col_w - 56)


def _t_cta_contact(p: dict, x: _Ctx) -> None:
    g = x.g
    head_f = x.font(int(g.w * 0.050), "Causten Bold")
    line_f = x.font(int(g.w * 0.026), "Causten Bold")
    if p.get("heading"):
        br.draw_heading(x.c, (g.margin, int(g.h * 0.10)), p["heading"], head_f, x.deep,
                        max_w=g.content_w, highlight=p.get("highlight") or None,
                        highlight_color=x.accent)
    # Solid brand CTA card with white contact lines.
    cy0 = int(g.h * 0.40)
    br.draw_card(x.c, (g.margin, cy0, g.w - g.margin, cy0 + int(g.h * 0.30)),
                 fill=x.deep, radius=36, shadow=True)
    contact = p.get("contact") or {}
    y = cy0 + 60
    for key in ("phone", "email", "website"):
        if contact.get(key):
            br.draw_paragraph(x.c, (g.margin + 60, y), str(contact[key]), line_f,
                              (255, 255, 255), max_w=g.content_w - 120)
            y += int(g.h * 0.06)


def _t_testimonial(p: dict, x: _Ctx) -> None:
    g = x.g
    quote_f = x.font(int(g.w * 0.034), None)
    name_f = x.font(int(g.w * 0.026), "Causten Bold")
    cy0 = int(g.h * 0.22)
    br.draw_card(x.c, (g.margin, cy0, g.w - g.margin, cy0 + int(g.h * 0.42)), radius=32)
    y = br.draw_paragraph(x.c, (g.margin + 60, cy0 + 70), '"' + p.get("quote", "") + '"',
                          quote_f, x.ink, max_w=g.content_w - 120, line_gap=1.5)
    br.draw_circular(x.c, (g.margin + 100, y + 80), 44,
                     initials="".join(w[0] for w in (p.get("author", "").split()[:2])).upper(),
                     fill=x.deep, font=x.font(int(g.w * 0.026), "Causten Bold"))
    br.draw_heading(x.c, (g.margin + 170, y + 60), p.get("author", ""), name_f, x.accent,
                    max_w=g.content_w - 220)


def _t_feature(p: dict, x: _Ctx) -> None:
    g = x.g
    head_f = x.font(int(g.w * 0.050), "Causten Bold")
    body_f = x.font(int(g.w * 0.022), None)
    bullet_f = x.font(int(g.w * 0.020), None)
    top = br.draw_heading(x.c, (g.margin, int(g.h * 0.08)), p.get("heading", ""), head_f,
                          x.deep, max_w=g.content_w, highlight=p.get("highlight") or None,
                          highlight_color=x.accent)
    cy0 = top + 50
    br.draw_card(x.c, (g.margin, cy0, g.w - g.margin, int(g.h * 0.80)), radius=32)
    y = cy0 + 50
    if p.get("body"):
        y = br.draw_paragraph(x.c, (g.margin + 50, y), p["body"], body_f, x.ink,
                              max_w=g.content_w - 100)
    if p.get("bullets"):
        br.draw_bullets(x.c, (g.margin + 50, y + 30), p["bullets"], bullet_f, x.ink,
                        accent=x.accent, max_w=g.content_w - 100)


_TEMPLATES: dict[str, Callable[[dict, _Ctx], None]] = {
    "cover": _t_cover,
    "card_grid": _t_card_grid,
    "steps": _t_steps,
    "cta_contact": _t_cta_contact,
    "testimonial": _t_testimonial,
    "feature": _t_feature,
}


def render_page(page: dict, *, size, palette: dict, font_loader, logo_png=None) -> bytes:
    """Render one brochure page to PNG bytes: calm background → template cards →
    optional logo corner. Unknown templates fall back to ``feature``."""
    bg = br.calm_background(size, _rgb(palette["light"]), _rgb(palette["deep"]))
    grid = Grid(size[0], size[1])
    ctx = _Ctx(bg, grid, palette, font_loader, logo_png)
    template = _TEMPLATES.get(page.get("template", ""), _t_feature)
    template(page, ctx)
    if logo_png:
        try:
            logo = Image.open(io.BytesIO(logo_png)).convert("RGBA")
            lw = int(size[0] * 0.18)
            logo = logo.resize((lw, int(lw * logo.height / logo.width)), Image.LANCZOS)
            bg.alpha_composite(logo, (size[0] - grid.margin - lw, grid.margin // 2))
        except Exception:  # noqa: BLE001 - logo is best-effort
            pass
    out = io.BytesIO()
    bg.convert("RGB").save(out, format="PNG")
    return out.getvalue()
