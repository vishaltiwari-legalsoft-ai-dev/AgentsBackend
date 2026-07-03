"""Cross-engine parity: Pillow vs the Node/Konva renderer. Opt-in — start the
renderer (`cd backend/renderer && npm start`) and run with
GD_RENDERER_URL=http://localhost:8090. Tolerance-based: fonts rasterize
differently across engines, so we bound the mean channel difference instead of
asserting bytes."""
import os
from io import BytesIO

import pytest
from PIL import Image, ImageChops, ImageStat

from graphics_designer_agent import render, render_contract, text_overlay

URL = os.environ.get("GD_RENDERER_URL", "").strip()
pytestmark = pytest.mark.skipif(not URL, reason="GD_RENDERER_URL not set")

MEAN_DIFF_LIMIT = 12.0  # of 255 per channel, averaged over the full image


def _base(w=400, h=500) -> bytes:
    img = Image.new("RGB", (w, h))
    for y in range(h):
        img.paste((255 - y % 128, 170, 204), (0, y, w, y + 1))
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _both(layers, w=400, h=500):
    base = _base(w, h)
    pillow = text_overlay.render_layers(base, layers, w, h)
    req = render_contract.build_render_request(base, layers, w, h)
    konva = render._service_render(req, URL)
    return pillow, konva


def _mean_diff(a: bytes, b: bytes) -> float:
    ia = Image.open(BytesIO(a)).convert("RGB")
    ib = Image.open(BytesIO(b)).convert("RGB")
    assert ia.size == ib.size, f"size mismatch {ia.size} vs {ib.size}"
    stat = ImageStat.Stat(ImageChops.difference(ia, ib))
    return sum(stat.mean) / 3


def _text(id_, text, **over):
    layer = {"type": "text", "id": id_, "text": text, "highlight": "",
             "font": "Causten Bold", "size_pct": 8.0, "color": "dark",
             "highlight_color": "gradient", "placement": "left", "offset": (0, 0),
             "z": 10, "pinned": False, "x": 0.06, "y": 0.5, "w": 0.42, "anchor": "ml"}
    layer.update(over)
    return layer


def test_auto_headline_with_highlight_parity():
    layers = [_text("headline", "Grow Your Firm Fast", highlight="Your Firm")]
    p, k = _both(layers)
    assert _mean_diff(p, k) < MEAN_DIFF_LIMIT


def test_pinned_multiline_parity():
    layers = [_text("subheading-0", "line one\nline two", size_pct=3.0,
                    pinned=True, x=0.5, y=0.4, anchor="mc", z=11)]
    p, k = _both(layers)
    assert _mean_diff(p, k) < MEAN_DIFF_LIMIT


def test_cta_parity():
    layers = [{"type": "cta", "id": "cta", "text": "Book a call",
               "font": "Causten Bold", "size_pct": 3.4, "color": "cta",
               "placement": "bottom", "offset": (0, 0), "z": 20, "pinned": False,
               "x": 0.5, "y": 0.94, "w": 0.88, "anchor": "bc"}]
    p, k = _both(layers)
    assert _mean_diff(p, k) < MEAN_DIFF_LIMIT


def test_shape_raster_is_near_exact():
    layers = [{"type": "shape", "id": "s1", "kind": "rounded-rect", "x": 0.5,
               "y": 0.5, "w": 0.5, "h": 0.3, "anchor": "mc", "fill": "#1746A2",
               "stroke": None, "stroke_w": 0, "radius": 24, "icon": None,
               "text": "", "z": 5, "pinned": True}]
    p, k = _both(layers)
    assert _mean_diff(p, k) < 2.0  # same Pillow pixels, only recompressed
