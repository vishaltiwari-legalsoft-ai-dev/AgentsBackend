# creative/brochure_layout.py
"""Brochure visual grammar — a column grid and the page templates that arrange
cards on it. Each template is a pure function (content + palette + grid →
primitive calls) returning a finished page PNG. No I/O, no provider.
"""

from __future__ import annotations

import io
from typing import Callable

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


# Templates that carry a STRONG photographic background (hero pages); everything
# else gets the soft texture treatment so white cards stay crisp.
_PHOTO_STRONG = {"cover", "cta_contact"}


class _Ctx:
    """Resolved drawing context handed to every template."""

    def __init__(self, canvas, grid, palette, font_loader, logo_png, on_photo=False):
        self.c = canvas
        self.g = grid
        self.deep = _rgb(palette["deep"])
        self.accent = _rgb(palette["accent"])
        self.ink = _rgb(palette["text"])
        self.light = _rgb(palette["light"])
        self.font = font_loader
        self.logo = logo_png
        self.on_photo = on_photo


# --------------------------------------------------------------------------- #
# Layout measurement helpers (so content blocks can be vertically centered in
# their cards / the page, instead of clustering at the top with empty space
# below). They re-use the renderer's own word-wrap so the estimate matches what
# actually gets drawn.
# --------------------------------------------------------------------------- #

def _line_h(font, gap: float = 1.4) -> int:
    asc, desc = font.getmetrics()
    return int((asc + desc) * gap)


def _text_h(font, text: str, max_w: int, gap: float) -> int:
    """Estimated drawn height of ``text`` wrapped to ``max_w`` at ``gap`` spacing."""
    lines = br._wrap(font, text or "", max_w)
    return _line_h(font, gap) * max(1, len(lines))


# --------------------------------------------------------------------------- #
# Templates — each draws onto ctx.c and returns None.
# --------------------------------------------------------------------------- #

def _t_cover(p: dict, x: _Ctx) -> None:
    g = x.g
    title_f = x.font(int(g.w * 0.078), "Causten Bold")
    sub_f = x.font(int(g.w * 0.032), None)
    left, width = g.span(0, 12)
    head_h = _text_h(title_f, p.get("heading", ""), width, 1.12)
    sub_h = _text_h(sub_f, p.get("subtitle", ""), width, 1.4) if p.get("subtitle") else 0
    block_h = head_h + 38 + (64 + sub_h if sub_h else 0)
    # Center the title block vertically so the page doesn't read top-heavy.
    y = max(int(g.h * 0.28), (g.h - block_h) // 2)
    title_color = (255, 255, 255) if x.on_photo else x.deep
    sub_color = (255, 255, 255) if x.on_photo else x.accent
    y = br.draw_heading(x.c, (left, y), p.get("heading", ""), title_f, title_color,
                        max_w=width, highlight=p.get("highlight") or None,
                        highlight_color=x.accent)
    # A short accent rule under the title (a small brand flourish).
    br.draw_card(x.c, (left, y + 26, left + int(width * 0.16), y + 38),
                 fill=x.accent, radius=6, shadow=False)
    if p.get("subtitle"):
        br.draw_paragraph(x.c, (left, y + 64), p["subtitle"], sub_f, sub_color, max_w=width)


def _t_card_grid(p: dict, x: _Ctx) -> None:
    g = x.g
    head_f = x.font(int(g.w * 0.050), "Causten Bold")
    pill_f = x.font(int(g.w * 0.017), "Causten Bold")
    bullet_f = x.font(int(g.w * 0.0195), None)
    av_f = x.font(int(g.w * 0.022), "Causten Bold")
    left, width = g.span(0, 12)
    top = br.draw_heading(x.c, (left, int(g.h * 0.06)), p.get("heading", ""),
                          head_f, x.deep, max_w=width,
                          highlight=p.get("highlight") or None, highlight_color=x.accent)
    cards = (p.get("cards") or [])[:6]
    cols = g.columns(2)
    rows = max(1, (len(cards) + 1) // 2)
    gap = 30
    card_h = int(g.h * 0.205)
    band_top, band_bottom = top + 44, g.h - g.margin
    total = rows * card_h + (rows - 1) * gap
    grid_top = band_top + max(0, (band_bottom - band_top - total) // 2)
    pad = 34
    for i, card in enumerate(cards):
        col_x, col_w = cols[i % 2]
        cy = grid_top + (i // 2) * (card_h + gap)
        br.draw_card(x.c, (col_x, cy, col_x + col_w, cy + card_h))
        bullets = card.get("bullets") or []
        block_h = 72 + 16 + _line_h(bullet_f, 1.45) * max(1, len(bullets))
        bt = cy + max(pad, (card_h - block_h) // 2)
        br.draw_circular(x.c, (col_x + pad + 30, bt + 30), 30,
                         initials=card.get("initials", ""), fill=x.deep, font=av_f)
        br.draw_pill(x.c, (col_x + pad + 76, bt + 6), card.get("title", ""), pill_f, fill=x.accent)
        br.draw_bullets(x.c, (col_x + pad, bt + 88), bullets, bullet_f, x.ink,
                        accent=x.accent, max_w=col_w - 2 * pad)


def _t_steps(p: dict, x: _Ctx) -> None:
    g = x.g
    head_f = x.font(int(g.w * 0.050), "Causten Bold")
    title_f = x.font(int(g.w * 0.024), "Causten Bold")
    body_f = x.font(int(g.w * 0.018), None)
    badge_f = x.font(int(g.w * 0.026), "Causten Bold")
    top = br.draw_heading(x.c, (g.margin, int(g.h * 0.06)), p.get("heading", ""), head_f,
                          x.deep, max_w=g.content_w, highlight=p.get("highlight") or None,
                          highlight_color=x.accent)
    steps = (p.get("steps") or [])[:3]
    cols = g.columns(max(1, len(steps)))
    card_h = int(g.h * 0.30)
    band_top, band_bottom = top + 44, g.h - g.margin
    cy = band_top + max(0, (band_bottom - band_top - card_h) // 2)
    for i, step in enumerate(steps):
        col_x, col_w = cols[i]
        inner = col_w - 56
        block_h = (60 + 22 + _text_h(title_f, step.get("title", ""), inner, 1.15)
                   + 12 + _text_h(body_f, step.get("desc", ""), inner, 1.4))
        bt = cy + max(40, (card_h - block_h) // 2)
        br.draw_card(x.c, (col_x, cy, col_x + col_w, cy + card_h))
        br.draw_circular(x.c, (col_x + 58, bt + 30), 30, initials=str(i + 1),
                         fill=x.accent, font=badge_f)
        ty = br.draw_heading(x.c, (col_x + 28, bt + 82), step.get("title", ""), title_f,
                             x.deep, max_w=inner)
        br.draw_paragraph(x.c, (col_x + 28, ty + 12), step.get("desc", ""), body_f,
                          x.ink, max_w=inner)


def _t_cta_contact(p: dict, x: _Ctx) -> None:
    g = x.g
    head_f = x.font(int(g.w * 0.050), "Causten Bold")
    msg_f = x.font(int(g.w * 0.030), "Causten Bold")
    line_f = x.font(int(g.w * 0.026), None)
    if p.get("heading"):
        head_color = (255, 255, 255) if x.on_photo else x.deep
        br.draw_heading(x.c, (g.margin, int(g.h * 0.10)), p["heading"], head_f, head_color,
                        max_w=g.content_w, highlight=p.get("highlight") or None,
                        highlight_color=x.accent)
    cy0, panel_h = int(g.h * 0.34), int(g.h * 0.42)
    br.draw_card(x.c, (g.margin, cy0, g.w - g.margin, cy0 + panel_h),
                 fill=x.deep, radius=36, shadow=True)
    inner = g.content_w - 120
    msg = p.get("body") or ("Ready to streamline your firm with expert virtual "
                            "legal staff? Contact us today.")
    contact = p.get("contact") or {}
    lines = [str(contact[k]) for k in ("phone", "email", "website") if contact.get(k)]
    line_step = _line_h(line_f, 1.0) + 24
    block_h = _text_h(msg_f, msg, inner, 1.3) + (44 + line_step * len(lines) if lines else 0)
    y = cy0 + max(50, (panel_h - block_h) // 2)
    y = br.draw_paragraph(x.c, (g.margin + 60, y), msg, msg_f, (255, 255, 255),
                          max_w=inner, line_gap=1.3)
    y += 44
    for ln in lines:
        br.draw_paragraph(x.c, (g.margin + 60, y), ln, line_f, (255, 255, 255), max_w=inner)
        y += line_step


def _t_testimonial(p: dict, x: _Ctx) -> None:
    g = x.g
    quote_f = x.font(int(g.w * 0.034), None)
    name_f = x.font(int(g.w * 0.026), "Causten Bold")
    cy0, card_h = int(g.h * 0.22), int(g.h * 0.44)
    br.draw_card(x.c, (g.margin, cy0, g.w - g.margin, cy0 + card_h), radius=32)
    inner = g.content_w - 120
    quote = '"' + p.get("quote", "") + '"'
    block_h = _text_h(quote_f, quote, inner, 1.5) + 80 + 88
    y = cy0 + max(60, (card_h - block_h) // 2)
    y = br.draw_paragraph(x.c, (g.margin + 60, y), quote, quote_f, x.ink,
                          max_w=inner, line_gap=1.5)
    av_y = y + 70
    br.draw_circular(x.c, (g.margin + 100, av_y), 44,
                     initials="".join(w[0] for w in (p.get("author", "").split()[:2])).upper(),
                     fill=x.deep, font=x.font(int(g.w * 0.026), "Causten Bold"))
    br.draw_heading(x.c, (g.margin + 170, av_y - 20), p.get("author", ""), name_f, x.accent,
                    max_w=g.content_w - 220)


def _t_feature(p: dict, x: _Ctx) -> None:
    g = x.g
    head_f = x.font(int(g.w * 0.050), "Causten Bold")
    body_f = x.font(int(g.w * 0.022), None)
    bullet_f = x.font(int(g.w * 0.020), None)
    top = br.draw_heading(x.c, (g.margin, int(g.h * 0.08)), p.get("heading", ""), head_f,
                          x.deep, max_w=g.content_w, highlight=p.get("highlight") or None,
                          highlight_color=x.accent)
    cy0, cy1 = top + 50, int(g.h * 0.86)
    br.draw_card(x.c, (g.margin, cy0, g.w - g.margin, cy1), radius=32)
    inner = g.content_w - 100
    y = cy0 + 60
    if p.get("body"):
        y = br.draw_paragraph(x.c, (g.margin + 50, y), p["body"], body_f, x.ink, max_w=inner)
    if p.get("bullets"):
        br.draw_bullets(x.c, (g.margin + 50, y + 30), p["bullets"], bullet_f, x.ink,
                        accent=x.accent, max_w=inner)


_TEMPLATES: dict[str, Callable[[dict, _Ctx], None]] = {
    "cover": _t_cover,
    "card_grid": _t_card_grid,
    "steps": _t_steps,
    "cta_contact": _t_cta_contact,
    "testimonial": _t_testimonial,
    "feature": _t_feature,
}


def render_page(page: dict, *, size, palette: dict, font_loader, logo_png=None,
                bg_png: bytes | None = None) -> bytes:
    """Render one brochure page to PNG bytes: photographic background (with a
    legibility treatment) when ``bg_png`` is given, else the calm gradient →
    template cards → optional logo corner. Unknown templates fall back to
    ``feature``."""
    template_name = page.get("template", "")
    bg = None
    if bg_png:
        mode = "strong" if template_name in _PHOTO_STRONG else "soft"
        bg = br.photo_background(size, bg_png, mode=mode,
                                 light=_rgb(palette["light"]), deep=_rgb(palette["deep"]))
    on_photo = bg is not None and template_name in _PHOTO_STRONG
    if bg is None:
        bg = br.calm_background(size, _rgb(palette["light"]), _rgb(palette["deep"]))
    grid = Grid(size[0], size[1])
    ctx = _Ctx(bg, grid, palette, font_loader, logo_png, on_photo=on_photo)
    template = _TEMPLATES.get(template_name, _t_feature)
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


# --- appended: plan → pages composer ---

_CONTACT_HINTS = ("contact", "get in touch", "reach", "email", "call", "phone")
_QUOTE_HINTS = ("client", "testimonial", "say", "review", "quote")


def _infer_template(section: dict) -> str:
    """Pick a template for a legacy/hint-less section by its content shape."""
    if section.get("steps"):
        return "steps"
    if section.get("cards"):
        return "card_grid"
    if section.get("quote") or section.get("author"):
        return "testimonial"
    if section.get("contact"):
        return "cta_contact"
    blob = " ".join(str(section.get(k, "")) for k in ("heading", "body")).lower()
    if any(h in blob for h in _QUOTE_HINTS):
        return "testimonial"
    if any(h in blob for h in _CONTACT_HINTS):
        return "cta_contact"
    # A section that is mostly a list of short items reads best as cards.
    bullets = section.get("bullets") or []
    if len(bullets) >= 3:
        return "card_grid"
    return "feature"


def _page_text_lines(page: dict) -> list[str]:
    """Flatten a page's copy for the invisible, selectable PDF text layer."""
    lines: list[str] = []
    for key in ("heading", "subtitle", "body", "quote", "author"):
        if page.get(key):
            lines.append(str(page[key]))
    for card in page.get("cards") or []:
        lines.append(str(card.get("title", "")))
        lines.extend(str(b) for b in (card.get("bullets") or []))
    for step in page.get("steps") or []:
        lines.append(f"{step.get('title', '')} — {step.get('desc', '')}")
    lines.extend(str(b) for b in (page.get("bullets") or []))
    contact = page.get("contact") or {}
    lines.extend(str(v) for v in contact.values() if v)
    return [ln for ln in lines if ln.strip()]


def _cards_from_bullets(section: dict) -> list[dict]:
    """A legacy section's bullets become simple titled cards."""
    out = []
    for b in section.get("bullets") or []:
        title = str(b).split("—")[0].split(":")[0].strip()[:28] or "Detail"
        out.append({"title": title, "bullets": [str(b)],
                    "initials": "".join(w[0] for w in title.split()[:2]).upper()})
    return out


def compose_brochure(plan: dict) -> list[dict]:
    """Map a brochure plan to renderable page dicts (cover first). Accepts the new
    ``pages`` shape or the legacy ``sections`` shape."""
    cover_src = plan.get("cover") or {}
    pages: list[dict] = [{
        "template": "cover",
        "heading": cover_src.get("title", ""),
        "highlight": cover_src.get("highlight", ""),
        "subtitle": cover_src.get("subtitle", ""),
    }]

    if plan.get("pages"):
        for pg in plan["pages"]:
            page = dict(pg)
            page.setdefault("template", _infer_template(page))
            pages.append(page)
    else:
        for section in plan.get("sections", []) or []:
            template = _infer_template(section)
            page = {"template": template, "heading": section.get("heading", ""),
                    "highlight": section.get("highlight", ""), "body": section.get("body", ""),
                    "bullets": section.get("bullets") or []}
            if template == "card_grid":
                page["cards"] = section.get("cards") or _cards_from_bullets(section)
            pages.append(page)
        contact = plan.get("contact") or {}
        if contact.get("line"):
            pages.append({"template": "cta_contact", "heading": "Get in touch",
                          "contact": {"website": contact["line"]}})

    for page in pages:
        page["text_lines"] = _page_text_lines(page)
    return pages
