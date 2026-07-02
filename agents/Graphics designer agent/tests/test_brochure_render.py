# tests/test_brochure_render.py
from PIL import Image, ImageFont
from graphics_designer_agent.creative import brochure_render as br


def _font(size=40, name=None):
    return ImageFont.load_default()


def _canvas(w=400, h=400):
    return Image.new("RGBA", (w, h), (255, 255, 255, 255))


def test_page_size_is_portrait_4x5():
    w, h = br._BROCHURE_PAGE
    assert (w, h) == (1240, 1550)
    assert h > w


def test_calm_background_is_full_size_rgba():
    bg = br.calm_background((300, 500), (240, 246, 255), (23, 70, 162))
    assert bg.size == (300, 500)
    assert bg.mode == "RGBA"
    # top is lighter than bottom (a calm vertical gradient)
    top = bg.convert("RGB").getpixel((150, 2))
    bot = bg.convert("RGB").getpixel((150, 498))
    assert sum(top) > sum(bot)


def test_draw_card_paints_a_shadow_outside_the_fill():
    c = _canvas()
    before = c.convert("RGB").getpixel((30, 210))  # just below the card box
    br.draw_card(c, (40, 40, 360, 200), fill=(255, 255, 255), shadow=True)
    after = c.convert("RGB").getpixel((30, 210))
    # the soft shadow darkens pixels just outside the card
    assert sum(after) < sum(before)


def test_draw_pill_returns_size_and_paints_fill():
    c = _canvas()
    w, h = br.draw_pill(c, (20, 20), "INTAKE", _font(), fill=(220, 90, 30))
    assert w > 0 and h > 0
    px = c.convert("RGB").getpixel((20 + w // 2, 20 + h // 2))
    assert px != (255, 255, 255)  # something was drawn


def test_draw_bullets_advances_y_per_item():
    c = _canvas()
    end_one = br.draw_bullets(c, (20, 20), ["one"], _font(), (15, 15, 15),
                              accent=(220, 90, 30), max_w=300)
    end_three = br.draw_bullets(c, (20, 20), ["one", "two", "three"], _font(),
                                (15, 15, 15), accent=(220, 90, 30), max_w=300)
    assert end_three > end_one > 20


def test_draw_circular_with_initials_paints_inside_circle():
    c = _canvas()
    br.draw_circular(c, (200, 200), 60, initials="VT", fill=(23, 70, 162),
                     text_color=(255, 255, 255), font=_font())
    inside = c.convert("RGB").getpixel((200, 200))
    corner = c.convert("RGB").getpixel((5, 5))
    assert inside != corner  # circle drew over the white background


import io


def _photo_bytes(w=200, h=200, color=(200, 60, 60)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def test_photo_background_strong_is_page_size_rgba():
    bg = br.photo_background((300, 500), _photo_bytes(), mode="strong",
                             light=(240, 246, 255), deep=(23, 70, 162))
    assert bg is not None
    assert bg.size == (300, 500)
    assert bg.mode == "RGBA"


def test_photo_background_strong_scrim_is_bottom_heavy():
    # A flat red photo under a deep-blue bottom-heavy scrim: the bottom pixel
    # must be pulled toward the deep colour more than the top pixel.
    bg = br.photo_background((300, 500), _photo_bytes(), mode="strong",
                             light=(240, 246, 255), deep=(23, 70, 162))
    top = bg.convert("RGB").getpixel((150, 5))
    bot = bg.convert("RGB").getpixel((150, 495))
    assert bot[2] > top[2]      # more blue (deep) at the bottom
    assert bot[0] < top[0]      # less of the photo's red at the bottom


def test_photo_background_soft_is_lighter_than_strong():
    strong = br.photo_background((300, 500), _photo_bytes(), mode="strong",
                                 light=(240, 246, 255), deep=(23, 70, 162))
    soft = br.photo_background((300, 500), _photo_bytes(), mode="soft",
                               light=(240, 246, 255), deep=(23, 70, 162))
    s_px = soft.convert("RGB").getpixel((150, 250))
    st_px = strong.convert("RGB").getpixel((150, 250))
    assert sum(s_px) > sum(st_px)   # the light wash brightens the mid-page


def test_photo_background_bad_bytes_returns_none():
    assert br.photo_background((300, 500), b"not-a-png", mode="strong",
                               light=(240, 246, 255), deep=(23, 70, 162)) is None


def test_photo_background_cover_crops_wide_image():
    # A very wide photo must fill a portrait page edge-to-edge (no letterbox).
    bg = br.photo_background((300, 500), _photo_bytes(w=800, h=100), mode="soft",
                             light=(240, 246, 255), deep=(23, 70, 162))
    assert bg.size == (300, 500)
