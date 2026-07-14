"""Engine dispatch for Stage-3 rendering. ``GD_RENDERER=pillow`` (default)
keeps the byte-identical legacy path; ``GD_RENDERER=konva`` + ``GD_RENDERER_URL``
posts the render contract to the Node/Konva service, falling back to Pillow on
ANY failure so a renderer outage can never break generation."""
from __future__ import annotations

import json
import logging
import os
import urllib.request

from . import render_contract, text_overlay

log = logging.getLogger(__name__)


def _service_render(req: dict, url: str) -> bytes:
    body = json.dumps(req).encode("utf-8")
    http_req = urllib.request.Request(
        url.rstrip("/") + "/render", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(http_req, timeout=120) as resp:
        return resp.read()


# Text sizes are calibrated as % of canvas WIDTH on portrait/square social
# canvases. On a LANDSCAPE canvas the width far exceeds the height, so
# %-of-width text blows up vertically (a 16:9 headline at 8% of width is ~14%
# of the height PER LINE) — stacks overflow the canvas and collide with the
# CTA. The dispatch normalizes text/CTA sizes by the aspect factor so the same
# config reads the same on any AR; min(1, …) keeps every portrait/square
# render byte-identical, and both engines (Pillow + Konva) inherit it.
_LANDSCAPE_TEXT_NORM = 1.2


def _normalize_text_sizes(layers: list, base_w: int, base_h: int) -> list:
    factor = min(1.0, _LANDSCAPE_TEXT_NORM * base_h / max(1, base_w))
    if factor >= 1.0:
        return layers
    return [
        {**layer, "size_pct": round(float(layer["size_pct"]) * factor, 3)}
        if layer.get("type") in ("text", "cta") and "size_pct" in layer else layer
        for layer in layers
    ]


def render_layers(base_png: bytes, layers: list, base_w: int, base_h: int,
                  *, px_scale: float = 1.0, pack=None, image_loader=None) -> bytes:
    layers = _normalize_text_sizes(layers, base_w, base_h)
    engine = os.environ.get("GD_RENDERER", "pillow").strip().lower()
    url = os.environ.get("GD_RENDERER_URL", "").strip()
    if engine == "konva" and url:
        try:
            req = render_contract.build_render_request(
                base_png, layers, base_w, base_h,
                px_scale=px_scale, pack=pack, image_loader=image_loader)
            return _service_render(req, url)
        except Exception:  # noqa: BLE001 - any renderer failure → safe fallback
            log.exception("konva renderer failed — falling back to Pillow")
    return text_overlay.render_layers(base_png, layers, base_w, base_h,
                                      px_scale=px_scale, pack=pack,
                                      image_loader=image_loader)
