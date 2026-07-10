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
