import base64
import json
from io import BytesIO

from PIL import Image

from graphics_designer_agent import render_contract


def _png(w: int, h: int) -> bytes:
    out = BytesIO()
    Image.new("RGB", (w, h), (136, 170, 204)).save(out, format="PNG")
    return out.getvalue()


def _text_layer(**over):
    layer = {
        "type": "text", "id": "headline", "text": "Hello World", "highlight": "",
        "font": "Causten Bold", "size_pct": 8.0, "color": "dark",
        "highlight_color": "gradient", "placement": "left", "offset": (0, 0),
        "z": 10, "pinned": False, "x": 0.06, "y": 0.5, "w": 0.42, "anchor": "ml",
    }
    layer.update(over)
    return layer


def test_request_is_json_safe_and_resolves_font_file():
    png = _png(64, 80)
    req = render_contract.build_render_request(png, [_text_layer()], 64, 80)
    json.dumps(req)  # must not raise (tuples converted, bytes b64'd)
    assert req["v"] == 1 and req["base_w"] == 64 and req["base_h"] == 80
    layer = req["layers"][0]
    assert layer["font_file"] == "Causten-Bold.otf"
    assert layer["offset"] == [0, 0]
    assert base64.b64decode(req["base_png_b64"]) == png


def test_theme_defaults_to_locked_colors():
    req = render_contract.build_render_request(_png(8, 8), [], 8, 8)
    assert req["theme"] == {
        "dark": "#0F0F0F", "white": "#FFFFFF",
        "gradText": ["#86AFFE", "#2653AB"], "ctaGrad": ["#FF8A3D", "#F26A1A"],
    }


def test_original_layers_are_not_mutated():
    layers = [_text_layer()]
    render_contract.build_render_request(_png(8, 8), layers, 8, 8)
    assert layers[0]["offset"] == (0, 0)
    assert "font_file" not in layers[0]
