"""Serialize a ``layout.resolve_layers()`` draw list + base image into the JSON
request the Node/Konva renderer consumes. Python stays the source of truth for
config→layer resolution; this module only translates. Shape and element layers
are pre-rasterized here with the existing Pillow code (Task 2), so the Node
side only renders text/CTA and composites images."""
from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image

from . import elements as gd_elements
from . import text_overlay
from .variants import LOCKED_COLORS, font_file as default_font_file

CONTRACT_VERSION = 1


def _theme_payload(pack=None) -> dict:
    lc = pack.locked_colors if pack is not None else LOCKED_COLORS
    return {
        "dark": lc["text"],
        "white": "#FFFFFF",
        "gradText": [lc["headline_highlight"]["from"], lc["headline_highlight"]["to"]],
        "ctaGrad": [lc["cta"]["from"], lc["cta"]["to"]],
    }


def build_render_request(base_png: bytes, layers: list, base_w: int, base_h: int,
                         *, px_scale: float = 1.0, pack=None, image_loader=None) -> dict:
    resolve_file = pack.font_file if pack is not None else default_font_file
    out_layers = []
    for layer in layers:
        kind = layer.get("type")
        if kind in ("shape", "element"):
            out_layers.append(_raster_entry(layer, base_w, base_h, px_scale,
                                            pack, image_loader))
        else:
            entry = dict(layer)
            entry["offset"] = list(layer.get("offset", (0, 0)))
            entry["font_file"] = resolve_file(layer["font"])
            out_layers.append(entry)
    return {
        "v": CONTRACT_VERSION, "base_w": base_w, "base_h": base_h,
        "px_scale": px_scale, "theme": _theme_payload(pack),
        "base_png_b64": base64.b64encode(base_png).decode("ascii"),
        "layers": out_layers,
    }


def _raster_entry(layer, base_w, base_h, px_scale, pack, image_loader) -> dict:
    canvas = Image.new("RGBA", (base_w, base_h), (0, 0, 0, 0))
    if layer["type"] == "shape":
        theme = text_overlay.theme_for_pack(pack)
        text_overlay.draw_shape_layer(canvas, layer, base_w, base_h, theme, px_scale)
    else:
        gd_elements.draw_element(canvas, layer, base_w, base_h, image_loader=image_loader)
    out = BytesIO()
    canvas.save(out, format="PNG")
    return {"type": "raster", "group": layer["type"], "z": int(layer.get("z", 0)),
            "png_b64": base64.b64encode(out.getvalue()).decode("ascii")}
