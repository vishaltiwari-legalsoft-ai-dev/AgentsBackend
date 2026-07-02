# tests/test_brochure_layout.py
import io
from PIL import Image, ImageFont
from graphics_designer_agent.creative import brochure_layout as bl
from graphics_designer_agent.creative import brochure_render as br

PAL = {"gradient": ["#EAF1FF", "#1746A2"], "light": "#EAF1FF", "deep": "#1746A2",
       "accent": "#E45A1E", "text": "#0F0F0F", "cta_from": "#E45A1E", "cta_to": "#1746A2"}


def _fonts(size, name=None):
    return ImageFont.load_default()


def _png_size(data):
    return Image.open(io.BytesIO(data)).size


def test_grid_columns_are_within_margins_and_non_overlapping():
    g = bl.Grid(1240, 1550, margin=80)
    cols = g.columns(3)
    assert len(cols) == 3
    assert cols[0][0] >= 80                       # first col starts after the margin
    last_x, last_w = cols[-1]
    assert last_x + last_w <= 1240 - 80 + 1       # last col ends before the right margin
    for (x0, w0), (x1, _w1) in zip(cols, cols[1:]):
        assert x0 + w0 <= x1                       # no overlap


def test_render_page_returns_full_size_png_for_every_template():
    pages = {
        "cover": {"template": "cover", "heading": "Legal Soft", "subtitle": "A brochure"},
        "card_grid": {"template": "card_grid", "heading": "Roles",
                      "cards": [{"title": "Intake", "bullets": ["a", "b"], "initials": "IN"},
                                {"title": "Paralegal", "bullets": ["c"], "initials": "PA"}]},
        "steps": {"template": "steps", "heading": "How it works",
                  "steps": [{"title": "Sign up", "desc": "x"}, {"title": "Build", "desc": "y"},
                            {"title": "Delegate", "desc": "z"}]},
        "cta_contact": {"template": "cta_contact", "heading": "Contact",
                        "contact": {"phone": "424-341-4917", "email": "a@b.com",
                                    "website": "legalsoft.com"}},
        "testimonial": {"template": "testimonial", "quote": "Great team.", "author": "Sharon"},
        "feature": {"template": "feature", "heading": "Why us", "body": "Because.",
                    "bullets": ["one", "two"]},
    }
    for page in pages.values():
        data = render = bl.render_page(page, size=br._BROCHURE_PAGE, palette=PAL,
                                       font_loader=_fonts, logo_png=None)
        assert _png_size(data) == br._BROCHURE_PAGE


def test_unknown_template_falls_back_to_feature_and_still_renders():
    data = bl.render_page({"template": "nope", "heading": "X"},
                          size=br._BROCHURE_PAGE, palette=PAL, font_loader=_fonts)
    assert _png_size(data) == br._BROCHURE_PAGE


def _bg_bytes(color=(180, 40, 40)):
    buf = io.BytesIO()
    Image.new("RGB", (200, 250), color).save(buf, format="PNG")
    return buf.getvalue()


_PAGE = {"template": "cover", "heading": "Hello World", "subtitle": "Sub"}
_PALETTE = {"deep": "#1746A2", "accent": "#5B8DEF", "text": "#0F0F0F", "light": "#F0F6FF"}


def _fl(size, name=None):
    return ImageFont.load_default()


def test_render_page_with_bg_differs_from_without():
    plain = bl.render_page(_PAGE, size=(310, 388), palette=_PALETTE, font_loader=_fl)
    photo = bl.render_page(_PAGE, size=(310, 388), palette=_PALETTE,
                                        font_loader=_fl, bg_png=_bg_bytes())
    assert photo != plain


def test_render_page_bad_bg_falls_back_byte_identical():
    plain = bl.render_page(_PAGE, size=(310, 388), palette=_PALETTE, font_loader=_fl)
    broken = bl.render_page(_PAGE, size=(310, 388), palette=_PALETTE,
                                         font_loader=_fl, bg_png=b"garbage")
    assert broken == plain


def test_strong_templates_are_cover_and_cta():
    assert bl._PHOTO_STRONG == {"cover", "cta_contact"}


def test_content_page_gets_soft_treatment_pixels():
    # card_grid page over a red photo: after the ~80% light wash the corner pixel
    # must be much lighter than the raw photo colour.
    page = {"template": "card_grid", "heading": "Grid", "cards": []}
    png = bl.render_page(page, size=(310, 388), palette=_PALETTE,
                                      font_loader=_fl, bg_png=_bg_bytes())
    im = Image.open(io.BytesIO(png)).convert("RGB")
    assert sum(im.getpixel((5, 380))) > sum((180, 40, 40))
