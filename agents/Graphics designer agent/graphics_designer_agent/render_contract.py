"""Serialize a ``layout.resolve_layers()`` draw list + base image into the JSON
request the Node/Konva renderer consumes. Python stays the source of truth for
config→layer resolution; this module only translates. Shape and element layers
are pre-rasterized here with the existing Pillow code (Task 2), so the Node
side only renders text/CTA and composites images."""
from __future__ import annotations

import base64

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
    raise NotImplementedError  # Task 2
