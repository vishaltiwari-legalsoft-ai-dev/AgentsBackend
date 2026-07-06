"""§9.5 — deterministic Stage 4 leaves base pixels outside the logo identical."""

from io import BytesIO

from PIL import Image

from graphics_designer_agent.stage4_logo.compositor import (
    composite_logo,
    logo_placement,
    pixels_identical_outside_box,
)


def _png(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_base_pixels_outside_logo_are_identical():
    base = Image.new("RGBA", (400, 500), (10, 20, 30, 255))
    for x in range(400):  # add variation so a naive re-encode would be caught
        base.putpixel((x, 0), (x % 256, (x * 3) % 256, 0, 255))
    logo = Image.new("RGBA", (100, 100), (255, 80, 20, 220))
    base_png, logo_png = _png(base), _png(logo)

    out = composite_logo(base_png, logo_png)
    box = logo_placement(400, 500, 100, 100)

    assert pixels_identical_outside_box(base_png, out, box)
    assert Image.open(BytesIO(out)).size == (400, 500)  # dimensions preserved


def test_width_ratios_follow_logo_aspect():
    assert logo_placement(1000, 1000, 200, 100)["w"] == 200   # normal → 20%
    assert logo_placement(1000, 1000, 400, 100)["w"] == 250   # wide (>3:1) → 25%
    assert logo_placement(1000, 1000, 100, 400)["w"] == 150   # tall (>1:2) → 15%
    assert logo_placement(1000, 1000, 200, 100)["x"] == 40    # 4% margin
