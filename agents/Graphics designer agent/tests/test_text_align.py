"""Per-element text alignment (left/center/right) in the deterministic renderer.

Byte-identical law: ``align`` absent → exactly the old code path (zone default
for auto text, left-start for pinned boxes). These tests pin that alignment
actually moves pixels and that omission changes nothing.
"""

from io import BytesIO

from PIL import Image

from graphics_designer_agent import pipeline
from graphics_designer_agent.runs import create_run
from graphics_designer_agent.stage3_text import text_overlay


def _base(w=640, h=640) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (w, h), (16, 32, 72)).save(buf, format="PNG")
    return buf.getvalue()


def _spec(align=None):
    return {
        "headline": {
            "text": "Steady counsel every single day", "highlight": "",
            "font": "Causten Bold", "size_pct": 6.0, "color": "white",
            "highlight_color": "gradient", "align": align,
            "placement": "top", "offset": (0, 0),
        },
        "subheadings": [],
        "cta": {"text": "", "font": "Causten Bold", "size_pct": 3.0,
                "placement": "bottom", "offset": (0, 0)},
    }


def test_align_moves_pixels():
    base = _base()
    left = text_overlay.render_overlay(base, _spec("left"), 640, 640)
    right = text_overlay.render_overlay(base, _spec("right"), 640, 640)
    center = text_overlay.render_overlay(base, _spec("center"), 640, 640)
    assert left != right
    assert left != center


def test_align_absent_matches_none():
    base = _base()
    a = text_overlay.render_overlay(base, _spec(None), 640, 640)
    spec = _spec(None)
    del spec["headline"]["align"]
    b = text_overlay.render_overlay(base, spec, 640, 640)
    assert a == b  # omitted key and explicit None are the same (old) path


def test_align_survives_config_roundtrip():
    run = create_run("align-user")
    run["config"]["element_styles"]["headline"]["align"] = "right"
    run["config"]["subheadings"] = [{"text": "Fine print, handled", "align": "center"}]
    spec = pipeline._resolve_overlay_spec(run)
    assert spec["headline"]["align"] == "right"
    assert spec["subheadings"][0]["align"] == "center"
