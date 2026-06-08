"""Logo rasterization for reference-image generation.

Converts a brand logo (SVG or raster) into clean PNG bytes that can be passed
to the image model as a reference so it composites the EXACT logo instead of
redrawing one.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger("agentos.imaging")


def to_png_logo(data: bytes, file_name: str = "", mime: str = "") -> bytes | None:
    """Return PNG bytes for a logo, or None if it can't be rendered.

    SVG is rasterized with svglib (pure-Python + cairo); raster formats are
    normalized to PNG via Pillow.
    """
    is_svg = mime == "image/svg+xml" or file_name.lower().endswith(".svg")
    try:
        if is_svg:
            from reportlab.graphics import renderPM
            from svglib.svglib import svg2rlg

            drawing = svg2rlg(io.BytesIO(data))
            if drawing is None:
                return None
            out = io.BytesIO()
            renderPM.drawToFile(drawing, out, fmt="PNG")
            return out.getvalue()

        from PIL import Image

        image = Image.open(io.BytesIO(data)).convert("RGBA")
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001 - logo is best-effort
        logger.warning("logo rasterization failed for %s: %s", file_name, exc)
        return None
