"""SVG logo rasterization must work without the native cairo library.

Regression: on hosts without libcairo (most Windows boxes), ``import
cairocffi`` raises OSError, which knocked out BOTH renderers — cairosvg
directly, and the svglib fallback indirectly (reportlab's renderPM imports
rlPyCairo, whose ``except ImportError`` guard doesn't catch cairocffi's
OSError, so it never fell back to the self-contained pycairo wheel). Stage 4
then had no logo at all and returned 400. ``app.services.imaging`` now poisons
the broken cairocffi module so rlPyCairo lands on pycairo.
"""

import io

from PIL import Image

from app.services import imaging

_SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
    b'<defs><linearGradient id="g"><stop offset="0" stop-color="#24B9CE"/>'
    b'<stop offset="1" stop-color="#137A9A"/></linearGradient></defs>'
    b'<rect x="10" y="10" width="180" height="80" fill="url(#g)"/>'
    b"</svg>"
)


def test_svg_logo_rasterizes_to_png_without_native_cairo():
    png = imaging.to_png_logo(_SVG, file_name="logo.svg", mime="image/svg+xml")
    assert png is not None, "SVG logo rasterization returned None"
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG"
    assert img.size[0] > 0 and img.size[1] > 0


def test_raster_logo_normalizes_to_png():
    buf = io.BytesIO()
    Image.new("RGBA", (64, 64), (23, 122, 154, 255)).save(buf, format="PNG")
    png = imaging.to_png_logo(buf.getvalue(), file_name="logo.png", mime="image/png")
    assert png is not None
    assert Image.open(io.BytesIO(png)).format == "PNG"


# --------------------------------------------------------------------------- #
# Decode-cost bounds (2026-09-25): a logo is validated on its header and every
# rasterisation path works at <= LOGO_WORK_PX a side. A 21 KB 9000x9000 1-bit
# PNG passed the byte-size cap and cost seconds of CPU per Stage-4 call.
# --------------------------------------------------------------------------- #

def _png(mode, size, fill=1) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, fill).save(buf, format="PNG")
    return buf.getvalue()


def test_logo_upload_validation_rejects_oversized_rasters_and_embedded_svg_images():
    import pytest

    bomb = _png("1", (9000, 9000))
    assert len(bomb) < 64 * 1024                       # passes any byte-size cap
    with pytest.raises(imaging.LogoRejected) as exc:
        imaging.validate_logo_upload(bomb, file_name="logo.png")
    assert exc.value.code == "image_too_large"
    with pytest.raises(imaging.LogoRejected) as exc:   # one pixel over, either axis
        imaging.validate_logo_upload(_png("1", (imaging.LOGO_MAX_SIDE_PX + 1, 2)), file_name="logo.png")
    assert exc.value.code == "image_too_large"
    imaging.validate_logo_upload(_png("1", (imaging.LOGO_MAX_SIDE_PX, 2)), file_name="logo.png")

    with pytest.raises(imaging.LogoRejected) as exc:
        imaging.validate_logo_upload(b"\x89PNG\r\n\x1a\nnot really", file_name="logo.png")
    assert exc.value.code == "unsupported_file_type"

    for svg in (
        b'<svg xmlns="http://www.w3.org/2000/svg"><image href="data:image/png;base64,AAAA"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><IMAGE xlink:href="x.png"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><style>.a{fill:url(data:x)}</style></svg>',
    ):
        with pytest.raises(imaging.LogoRejected) as exc:
            imaging.validate_logo_upload(svg, file_name="logo.svg")
        assert exc.value.code == "unsupported_file_type"
    imaging.validate_logo_upload(_SVG, file_name="logo.svg")           # a plain vector is fine
    imaging.validate_logo_upload(_SVG, mime="image/svg+xml")           # ...however it is labelled
    imaging.validate_logo_upload(_SVG)                                 # ...or sniffed from the bytes


def test_to_png_logo_works_at_the_bounded_size_whatever_the_upload():
    png = imaging.to_png_logo(_png("RGB", (3000, 3000), (255, 255, 255)), file_name="logo.png", mime="image/png")
    assert png is not None
    assert max(Image.open(io.BytesIO(png)).size) <= imaging.LOGO_WORK_PX
    png = imaging.to_png_logo(_png("1", (9000, 9000)), file_name="logo.png", mime="image/png")
    assert png is not None and max(Image.open(io.BytesIO(png)).size) == imaging.LOGO_WORK_PX
    small = imaging.to_png_logo(_png("RGB", (64, 32), (20, 80, 200)), file_name="logo.png", mime="image/png")
    assert Image.open(io.BytesIO(small)).size == (64, 32)             # never upscaled


def test_white_background_key_matches_the_pixel_flood_fill():
    """The numpy span fill must produce byte-identical alpha to the
    ``ImageDraw.floodfill(thresh=30)`` + per-pixel comprehension it replaced,
    including near-white noise, enclosed white and non-white corners."""
    import random
    import warnings

    from PIL import ImageDraw

    def reference(logo):
        rgba = logo.convert("RGBA")
        w, h = rgba.size
        px = rgba.load()
        white = [c for c in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
                 if all(v >= 240 for v in px[c][:3])]
        if not white:
            return rgba
        rgb = rgba.convert("RGB")
        for c in white:
            ImageDraw.floodfill(rgb, c, (255, 0, 255), thresh=30)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)   # getdata: the old code, verbatim
            keyed = [(r, g, b, 0) if (rr, gg, bb) == (255, 0, 255) else (r, g, b, a)
                     for (r, g, b, a), (rr, gg, bb) in zip(rgba.getdata(), rgb.getdata())]
        rgba.putdata(keyed)
        return rgba

    rng = random.Random(7)
    for _ in range(30):
        w, h = rng.randint(6, 40), rng.randint(6, 40)
        im = Image.new("RGB", (w, h), (255, 255, 255))
        d = ImageDraw.Draw(im)
        for _ in range(rng.randint(1, 6)):
            x0, y0 = rng.randint(0, w - 1), rng.randint(0, h - 1)
            box = [x0, y0, rng.randint(x0, w - 1), rng.randint(y0, h - 1)]
            fill = rng.choice([(0, 0, 0), (250, 250, 250), (240, 240, 240), (255, 255, 255), (20, 80, 200)])
            d.rectangle(box, fill=fill, outline=rng.choice([None, (0, 0, 0), (245, 245, 245)]))
        assert imaging._key_white_background(im).tobytes() == reference(im).tobytes()

    keyed = imaging._key_white_background(Image.new("RGBA", (4, 4), (255, 255, 255, 128)))
    assert keyed.getpixel((0, 0)) == (255, 255, 255, 128)            # real alpha is trusted
