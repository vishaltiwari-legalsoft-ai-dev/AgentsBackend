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


def render_layers(base_png: bytes, layers: list, base_w: int, base_h: int,
                  *, px_scale: float = 1.0, pack=None, image_loader=None) -> bytes:
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
