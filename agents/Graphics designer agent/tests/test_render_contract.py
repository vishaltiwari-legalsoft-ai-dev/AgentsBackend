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


def _shape_layer():
    return {"type": "shape", "id": "s1", "kind": "rect", "x": 0.5, "y": 0.5,
            "w": 0.5, "h": 0.5, "anchor": "mc", "fill": "#FF0000", "stroke": None,
            "stroke_w": 0, "radius": 0, "icon": None, "text": "", "z": 5, "pinned": True}


def test_shape_layer_becomes_full_canvas_raster():
    req = render_contract.build_render_request(_png(64, 80), [_shape_layer()], 64, 80)
    entry = req["layers"][0]
    assert entry["type"] == "raster" and entry["group"] == "shape" and entry["z"] == 5
    img = Image.open(BytesIO(base64.b64decode(entry["png_b64"])))
    assert img.size == (64, 80) and img.mode == "RGBA"
    assert img.getpixel((32, 40))[:3] == (255, 0, 0)   # center: red fill, opaque
    assert img.getpixel((1, 1))[3] == 0                # corner: transparent


def test_icon_shape_rasterizes_via_icons_engine():
    layer = dict(_shape_layer(), kind="icon", icon="star", fill="#1746A2")
    req = render_contract.build_render_request(_png(64, 80), [layer], 64, 80)
    img = Image.open(BytesIO(base64.b64decode(req["layers"][0]["png_b64"])))
    assert img.getextrema()[3][1] == 255               # some opaque ink exists
