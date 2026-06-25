"""The layout brain must keep text OFF the subject: pick the calm/empty zone and
a legible colour. The vision path needs a network model, so these tests pin the
deterministic pixel fallback (always available, used in tests + offline)."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from graphics_designer_agent.creative import layout_brain


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _busy_left() -> bytes:
    """A clean light field with a busy 'subject' on the LEFT half."""
    img = Image.new("RGB", (512, 512), (232, 236, 246))
    d = ImageDraw.Draw(img)
    for x in range(0, 256, 6):
        d.line([(x, 0), (x, 512)], fill=(18, 28, 64), width=2)
    return _png(img)


def test_places_text_away_from_a_left_subject():
    out = layout_brain._pixel_placement(_busy_left())
    assert out["placement"] == "right"  # the empty side
    assert out["source"] == "fallback"


def test_places_text_away_from_a_right_subject():
    img = Image.new("RGB", (512, 512), (232, 236, 246))
    d = ImageDraw.Draw(img)
    for x in range(256, 512, 6):  # subject clutter on the RIGHT
        d.line([(x, 0), (x, 512)], fill=(18, 28, 64), width=2)
    out = layout_brain._pixel_placement(_png(img))
    assert out["placement"] == "left"


def test_dark_background_gets_white_text():
    dark = Image.new("RGB", (400, 400), (14, 20, 40))
    assert layout_brain._pixel_placement(_png(dark))["color"] == "white"


def test_light_background_gets_dark_text():
    light = Image.new("RGB", (400, 400), (240, 244, 250))
    assert layout_brain._pixel_placement(_png(light))["color"] == "dark"


def test_decide_placement_always_returns_a_valid_zone():
    out = layout_brain.decide_placement(_busy_left(), headline="Hi", body="there")
    assert out["placement"] in layout_brain._VALID_PLACEMENTS
    assert out["color"] in ("dark", "white")


def test_unreadable_bytes_degrade_to_a_safe_default():
    out = layout_brain._pixel_placement(b"not an image")
    assert out["placement"] == "left" and out["color"] == "dark"
