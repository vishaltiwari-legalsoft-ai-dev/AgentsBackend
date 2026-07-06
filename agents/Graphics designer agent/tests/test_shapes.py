"""Stage-3 2D shape + infographic primitives (deterministic PIL drawing)."""

from PIL import Image

from graphics_designer_agent.stage3_text import icons, shapes


def _canvas(w=100, h=100):
    return Image.new("RGBA", (w, h), (0, 0, 0, 0))


def _opaque_px(img):
    return sum(1 for p in img.getdata() if p[3] > 0)


def test_rect_fills_region():
    c = _canvas()
    shapes.draw_rect(c, (10, 10, 90, 90), fill=(255, 0, 0))
    assert c.getpixel((50, 50)) == (255, 0, 0, 255)
    assert c.getpixel((2, 2))[3] == 0  # outside untouched


def test_rounded_rect_clips_corners():
    sharp = _canvas(); shapes.draw_rect(sharp, (0, 0, 99, 99), fill=(0, 0, 0), radius=0)
    round_ = _canvas(); shapes.draw_rect(round_, (0, 0, 99, 99), fill=(0, 0, 0), radius=30)
    assert _opaque_px(round_) < _opaque_px(sharp)  # corners carved away


def test_ellipse_circle_and_triangle_draw():
    for fn in (shapes.draw_ellipse, shapes.draw_triangle):
        c = _canvas()
        fn(c, (10, 10, 90, 90), fill=(0, 128, 255))
        assert _opaque_px(c) > 0


def test_arrow_and_divider_draw():
    c = _canvas(); shapes.draw_arrow(c, (10, 40, 90, 60), fill=(0, 0, 0), stroke_w=6)
    assert _opaque_px(c) > 0
    d = _canvas(); shapes.draw_divider(d, (5, 50, 95, 50), fill=(0, 0, 0), stroke_w=4)
    assert _opaque_px(d) > 0


def test_callout_is_rounded_box():
    c = _canvas()
    shapes.draw_callout(c, (10, 10, 90, 60), fill=(240, 240, 240), stroke=(0, 0, 0), stroke_w=2, radius=12)
    assert c.getpixel((50, 35))[3] == 255


def test_icon_known_key_draws_unknown_is_noop():
    c = _canvas()
    icons.draw_icon(c, "check", (20, 20, 80, 80), (0, 0, 0))
    assert _opaque_px(c) > 0
    blank = _canvas()
    icons.draw_icon(blank, "does-not-exist", (20, 20, 80, 80), (0, 0, 0))
    assert _opaque_px(blank) == 0  # unknown key never crashes, draws nothing


def test_icon_keys_all_draw():
    for key in icons.ICON_KEYS:
        c = _canvas()
        icons.draw_icon(c, key, (20, 20, 80, 80), (10, 10, 10))
        assert _opaque_px(c) > 0, key
