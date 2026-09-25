"""Logo rasterization for reference-image generation.

Converts a brand logo (SVG or raster) into clean PNG bytes that can be passed
to the image model as a reference so it composites the EXACT logo instead of
redrawing one.
"""

from __future__ import annotations

import io
import logging
import re
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

logger = logging.getLogger("agentos.imaging")

# Optional, best-quality SVG renderer (handles gradients). Unavailable on hosts
# without the native cairo library (e.g. most Windows boxes), in which case we
# fall back to svglib with gradient flattening below.
try:  # pragma: no cover - depends on host libs
    import cairosvg as _cairosvg
except Exception:  # noqa: BLE001
    _cairosvg = None

# When the native cairo DLL is missing, ``import cairocffi`` raises OSError —
# not ImportError — which breaks reportlab's renderPM backend (rlPyCairo does
# ``import cairocffi`` guarded only by ``except ImportError`` before trying the
# self-contained pycairo wheel). Poison the module so downstream imports get a
# plain ImportError and rlPyCairo falls back to pycairo, keeping the svglib
# fallback below functional on hosts without native cairo.
try:  # pragma: no cover - depends on host libs
    import cairocffi as _cairocffi  # noqa: F401
except ImportError:
    pass
except Exception:  # noqa: BLE001 - OSError from a failed dlopen
    sys.modules["cairocffi"] = None  # type: ignore[assignment]

_NAMED_COLORS = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "red": (255, 0, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
}

_GRAD_BLOCK_RE = re.compile(
    r"<(?:linearGradient|radialGradient)\b[^>]*\bid=[\"']([^\"']+)[\"'][^>]*>(.*?)"
    r"</(?:linearGradient|radialGradient)>",
    re.DOTALL | re.IGNORECASE,
)
_STOP_COLOR_RE = re.compile(
    r"stop-color\s*[:=]\s*[\"']?\s*(#[0-9a-fA-F]{3,8}|rgb\([^)]*\)|[a-zA-Z]+)",
    re.IGNORECASE,
)
_URL_REF_RE = re.compile(r"url\(\s*#([^)\s]+)\s*\)")


def _color_to_rgb(value: str) -> tuple[int, int, int] | None:
    value = value.strip().lower()
    if value.startswith("#"):
        hexv = value[1:]
        if len(hexv) in (3, 4):
            hexv = "".join(ch * 2 for ch in hexv[:3])
        if len(hexv) >= 6:
            try:
                return int(hexv[0:2], 16), int(hexv[2:4], 16), int(hexv[4:6], 16)
            except ValueError:
                return None
    m = re.match(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", value)
    if m:
        return tuple(min(255, int(x)) for x in m.groups())  # type: ignore[return-value]
    return _NAMED_COLORS.get(value)


def _flatten_svg_gradients(data: bytes) -> bytes:
    """Replace `url(#gradient)` fills with a representative solid color.

    svglib/reportlab cannot resolve SVG gradients (logs "Can't handle color:
    url(#...)") and drops those fills, which yields a blank/partial logo. We map
    each gradient id to the average of its stop colors and substitute solids so
    the logo renders with its real colors.
    """
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return data

    grad_color: dict[str, str] = {}
    all_rgbs: list[tuple[int, int, int]] = []
    for match in _GRAD_BLOCK_RE.finditer(text):
        gid, body = match.group(1), match.group(2)
        rgbs = [rgb for c in _STOP_COLOR_RE.findall(body) if (rgb := _color_to_rgb(c))]
        if rgbs:
            r = sum(c[0] for c in rgbs) // len(rgbs)
            g = sum(c[1] for c in rgbs) // len(rgbs)
            b = sum(c[2] for c in rgbs) // len(rgbs)
            grad_color[gid] = f"#{r:02x}{g:02x}{b:02x}"
            all_rgbs.extend(rgbs)

    if not grad_color:
        return data

    fallback = "#444444"
    if all_rgbs:
        fr = sum(c[0] for c in all_rgbs) // len(all_rgbs)
        fg = sum(c[1] for c in all_rgbs) // len(all_rgbs)
        fb = sum(c[2] for c in all_rgbs) // len(all_rgbs)
        fallback = f"#{fr:02x}{fg:02x}{fb:02x}"

    return _URL_REF_RE.sub(
        lambda m: grad_color.get(m.group(1), fallback), text
    ).encode("utf-8")


def _svg_to_png(data: bytes, file_name: str) -> bytes | None:
    # Best quality when the native cairo lib is present (renders gradients).
    # Rasterize a vector logo at a high width so it stays crisp when composited
    # onto a 4K creative (a 1000-px raster looked soft once the canvas grew).
    if _cairosvg is not None:
        try:
            return _cairosvg.svg2png(bytestring=data, output_width=2048)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cairosvg failed for %s, falling back: %s", file_name, exc)

    from reportlab.graphics import renderPM
    from svglib.svglib import svg2rlg

    drawing = svg2rlg(io.BytesIO(_flatten_svg_gradients(data)))
    if drawing is None:
        return None
    # renderPM paints the SVG's declared size; a viewBox of 100000 units would
    # allocate a 100000-px canvas. Scale the drawing down to the working size.
    longest = max(float(drawing.width or 0), float(drawing.height or 0))
    if longest > LOGO_WORK_PX:
        factor = LOGO_WORK_PX / longest
        drawing.scale(factor, factor)
        drawing.width, drawing.height = drawing.width * factor, drawing.height * factor
    out = io.BytesIO()
    renderPM.drawToFile(drawing, out, fmt="PNG")
    return out.getvalue()


#: Largest raster side accepted at logo upload. A 4096² logo is already far
#: beyond any composite; past it the only thing that grows is decode cost.
LOGO_MAX_SIDE_PX = 4096
#: Working size every logo is reduced to BEFORE keying/compositing, so no path
#: ever touches more than ~4M pixels whatever the upload's dimensions.
LOGO_WORK_PX = 2048
#: Flood-fill tolerance for the white-background key (1-norm over RGB), the
#: value ``ImageDraw.floodfill(thresh=30)`` used before the fill moved to numpy.
_KEY_THRESH = 30

_SVG_EMBEDDED_RE = re.compile(rb"<image\b|data:", re.IGNORECASE)


class LogoRejected(ValueError):
    """A logo upload the service refuses; ``code`` is the stable client code
    (``image_too_large`` / ``unsupported_file_type``)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _is_svg(data: bytes, file_name: str = "", mime: str = "") -> bool:
    if mime == "image/svg+xml" or file_name.lower().endswith(".svg"):
        return True
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n")
    return (head.startswith(b"<?xml") or head.startswith(b"<svg")) and b"<svg" in data[:65536]


def validate_logo_upload(data: bytes, *, file_name: str = "", mime: str = "") -> None:
    """Refuse a logo before any decode work happens; raises :class:`LogoRejected`.

    Rasters: the header alone (no pixel decode) must say at most
    ``LOGO_MAX_SIDE_PX`` per side — a 21 KB 9000×9000 1-bit PNG passes every
    byte-size check and would otherwise cost seconds of CPU per Stage-4 call.
    SVGs: no ``<image>`` element and no ``data:`` URI — those smuggle a raster
    (or worse) past the size cap through the vector renderer.
    """
    if _is_svg(data, file_name, mime):
        if _SVG_EMBEDDED_RE.search(data):
            raise LogoRejected("unsupported_file_type")
        return
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:  # header only; nothing is decoded
            width, height = im.size
    except Image.DecompressionBombError as exc:
        raise LogoRejected("image_too_large") from exc
    except Exception as exc:  # noqa: BLE001 - magic bytes matched, image did not
        raise LogoRejected("unsupported_file_type") from exc
    if width > LOGO_MAX_SIDE_PX or height > LOGO_MAX_SIDE_PX:
        raise LogoRejected("image_too_large")


def to_png_logo(data: bytes, file_name: str = "", mime: str = "") -> bytes | None:
    """Return PNG bytes for a logo, or None if it can't be rendered.

    SVG is rasterized with cairosvg when available, else svglib with gradient
    flattening; raster formats are normalized to PNG via Pillow. Every path
    downscales to ``LOGO_WORK_PX`` before the white-background key runs, so
    the cost is bounded by the working size, never by the upload.
    """
    try:
        if _is_svg(data, file_name, mime):
            return _svg_to_png(data, file_name)

        from PIL import Image

        image = Image.open(io.BytesIO(data))
        # Downscale FIRST (on the source mode, cheap in C) — converting a
        # 9000×9000 upload to RGBA before shrinking is the decode bomb.
        image.thumbnail((LOGO_WORK_PX, LOGO_WORK_PX))
        image = image.convert("RGBA")
        # Raster logos are frequently delivered on a solid white box (JPEG/PNG
        # without alpha). Knock that background out so Stage 4 doesn't composite
        # an ugly white rectangle behind the mark. Safe: _key_white_background
        # no-ops when the logo already has real transparency or non-white corners.
        image = _key_white_background(image)
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001 - logo is best-effort
        logger.warning("logo rasterization failed for %s: %s", file_name, exc)
        return None


def _background_mask(rgb, seeds: list[tuple[int, int]], thresh: int):
    """4-connected region reachable from each seed through pixels whose 1-norm
    distance from THAT seed's colour is ≤ ``thresh`` — exactly what
    ``ImageDraw.floodfill(thresh=...)`` fills, computed as row spans on a numpy
    array instead of one Python set operation per pixel (43 s → ~50 ms on a
    2048² white-background logo)."""
    import numpy as np

    h, w, _ = rgb.shape
    out = np.zeros((h, w), dtype=bool)
    for x0, y0 in seeds:
        if out[y0, x0]:
            continue  # already inside an earlier seed's fill
        # An earlier seed's fill is a wall for this one (Pillow had already
        # painted it the sentinel colour), so seed order changes nothing.
        near = (np.abs(rgb.astype(np.int16) - rgb[y0, x0].astype(np.int16)).sum(axis=2) <= thresh) & ~out
        filled = np.zeros((h, w), dtype=bool)
        stack = [(x0, y0)]
        while stack:
            x, y = stack.pop()
            if filled[y, x] or not near[y, x]:
                continue
            row = near[y]
            blocked = np.flatnonzero(~row[:x])
            left = int(blocked[-1]) + 1 if blocked.size else 0
            blocked = np.flatnonzero(~row[x + 1:])
            right = x + 1 + int(blocked[0]) if blocked.size else w   # exclusive
            filled[y, left:right] = True
            for ny in (y - 1, y + 1):
                if 0 <= ny < h:
                    idx = np.flatnonzero(near[ny, left:right] & ~filled[ny, left:right])
                    if idx.size:
                        starts = idx[np.concatenate(([True], np.diff(idx) > 1))]
                        stack.extend((left + int(s), ny) for s in starts)
        out |= filled
    return out


def _key_white_background(logo: "Image.Image") -> "Image.Image":
    """Make a logo's white background transparent (only when it clearly has one).

    SVGs rasterized by renderPM (and many raster logos) come on a solid white
    box. Pasting that over a colored creative would show an ugly white rectangle.
    We flood-fill from the corners so only the connected background is removed —
    white *inside* the mark is preserved. If the logo already has real
    transparency, or its corners aren't white, we leave it untouched.
    """
    import numpy as np
    from PIL import Image

    rgba = logo.convert("RGBA")
    # Already has meaningful transparency -> trust the source alpha.
    if rgba.getchannel("A").getextrema()[0] < 255:
        return rgba

    w, h = rgba.size
    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    px = rgba.load()

    def near_white(xy: tuple[int, int]) -> bool:
        r, g, b, _ = px[xy]
        return r >= 240 and g >= 240 and b >= 240

    white_corners = [c for c in corners if near_white(c)]
    if not white_corners:
        return rgba  # logo sits on a colored bg -> don't risk keying

    arr = np.array(rgba)                       # (h, w, 4) uint8 copy
    background = _background_mask(arr[:, :, :3], white_corners, _KEY_THRESH)
    arr[:, :, 3][background] = 0
    return Image.fromarray(arr, "RGBA")


# Must match the reserved zone in prompts.with_reserved_logo_space_instruction().
RESERVED_ZONE_WIDTH_RATIO = 0.28
RESERVED_ZONE_HEIGHT_RATIO = 0.14


def _reserved_zone(bw: int, bh: int, pad: int) -> tuple[int, int]:
    """Usable logo area inside the reserved corner, with sane minimums.

    Subtracting padding from the zone can collapse it to ~0 on wide canvases
    (e.g. 1.91:1 banners), which previously shrank the logo to a single pixel —
    an invisible logo. Floors keep it readable on every aspect ratio.
    """
    zone_w = max(int(bw * RESERVED_ZONE_WIDTH_RATIO) - pad, int(bw * 0.14))
    zone_h = max(int(bh * RESERVED_ZONE_HEIGHT_RATIO) - pad // 2, int(bh * 0.08))
    return max(zone_w, 1), max(zone_h, 1)


def _fit_logo_to_zone(logo: "Image.Image", zone_w: int, zone_h: int) -> "Image.Image":
    """Scale a logo to fit inside the reserved top-left zone."""
    from PIL import Image

    avail_w = max(1, int(zone_w * 0.82))
    avail_h = max(1, int(zone_h * 0.82))
    scale = min(avail_w / logo.width, avail_h / logo.height)
    target_w = max(1, int(logo.width * scale))
    target_h = max(1, int(logo.height * scale))
    return logo.resize((target_w, target_h), Image.LANCZOS)


def _top_left_reserved_position(
    base_w: int,
    base_h: int,
    logo_w: int,
    logo_h: int,
    pad: int,
) -> tuple[int, int]:
    """Anchor the logo inside the calm top-left zone the model was asked to keep empty."""
    zone_h = int(base_h * RESERVED_ZONE_HEIGHT_RATIO)
    x = pad
    y = pad + max(0, (zone_h - logo_h) // 2)
    return x, y


def _cleanest_corner(
    base: "Image.Image", logo_w: int, logo_h: int, pad: int
) -> tuple[int, int]:
    """Pick the corner whose pixels are most uniform (least content) for the logo.

    The image model is asked to keep the top-left clear, but it sometimes puts
    the headline there anyway; blindly pasting then covers text. We score each
    corner by the luminance spread of the area the logo would occupy (plus a
    small margin) and paste into the calmest one. Top-left wins ties via a
    penalty on the alternatives so placement stays predictable.
    """
    from PIL import ImageStat

    bw, bh = base.size
    margin = max(4, pad // 2)
    candidates: list[tuple[float, tuple[int, int]]] = []
    positions = {
        (pad, pad): 0.0,                                  # top-left (preferred)
        (bw - pad - logo_w, pad): 4.0,                    # top-right
        (pad, bh - pad - logo_h): 8.0,                    # bottom-left
        (bw - pad - logo_w, bh - pad - logo_h): 8.0,      # bottom-right
    }
    gray = base.convert("L")
    for (x, y), penalty in positions.items():
        if x < 0 or y < 0 or x + logo_w > bw or y + logo_h > bh:
            continue
        box = (
            max(0, x - margin),
            max(0, y - margin),
            min(bw, x + logo_w + margin),
            min(bh, y + logo_h + margin),
        )
        stat = ImageStat.Stat(gray.crop(box))
        spread = stat.stddev[0] + (stat.extrema[0][1] - stat.extrema[0][0]) * 0.1
        candidates.append((spread + penalty, (x, y)))

    if not candidates:
        return pad, pad
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _prepare_logo(logo_png: bytes) -> "Image.Image | None":
    """Key the background to transparency and trim. None if nothing remains."""
    from PIL import Image

    try:
        logo = _key_white_background(Image.open(io.BytesIO(logo_png)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not open logo candidate: %s", exc)
        return None
    bbox = logo.getbbox()
    if not bbox:
        return None  # fully transparent after keying (e.g. white-on-white)
    logo = logo.crop(bbox)
    if logo.width < 2 or logo.height < 2:
        return None
    return logo


def _logo_visibility(logo: "Image.Image") -> tuple[float, float]:
    """Return (opaque_ratio, mean_luminance_of_opaque_pixels)."""
    from PIL import ImageStat

    alpha = logo.getchannel("A")
    mask = alpha.point(lambda a: 255 if a > 40 else 0)
    stat = ImageStat.Stat(logo.convert("L"), mask=mask)
    if not stat.count or stat.count[0] == 0:
        return 0.0, 0.0
    return stat.count[0] / float(logo.width * logo.height), stat.mean[0]


# A logo must differ from its backdrop by at least this much luminance to read.
_MIN_LOGO_CONTRAST = 45.0
# Opaque pixels must cover at least this share of the trimmed logo box.
_MIN_OPAQUE_RATIO = 0.02


def composite_best_logo(
    base_png: bytes,
    logo_pngs: list[bytes],
    *,
    padding_ratio: float = 0.04,
) -> tuple[bytes | None, int | None]:
    """Overlay the best library logo onto the creative (Step: final assembly).

    Candidates are tried in priority order. Each one is keyed to a transparent
    background, checked for actual visible content (a white logo rasterized on
    white keys away to nothing), and scored by luminance contrast against the
    exact corner area it would sit on — so a white logo wins on dark art and a
    colored/dark logo wins on light art. The first candidate with readable
    contrast is composited; otherwise the highest-contrast visible one is used.

    Returns (final_png, chosen_index) or (None, None) when no candidate is
    visibly usable.
    """
    from PIL import Image, ImageStat

    base = Image.open(io.BytesIO(base_png)).convert("RGBA")
    bw, bh = base.size
    pad = int(bw * padding_ratio)
    gray_base = base.convert("L")

    zone_w, zone_h = _reserved_zone(bw, bh, pad)

    best: tuple[float, "Image.Image", tuple[int, int], int] | None = None
    for index, raw in enumerate(logo_pngs):
        logo = _prepare_logo(raw)
        if logo is None:
            continue
        logo = _fit_logo_to_zone(logo, zone_w, zone_h)
        opaque_ratio, logo_lum = _logo_visibility(logo)
        if opaque_ratio < _MIN_OPAQUE_RATIO:
            continue

        position = _top_left_reserved_position(bw, bh, logo.width, logo.height, pad)
        region = gray_base.crop(
            (position[0], position[1], position[0] + logo.width, position[1] + logo.height)
        )
        corner_lum = ImageStat.Stat(region).mean[0]
        contrast = abs(logo_lum - corner_lum)

        if best is None or contrast > best[0]:
            best = (contrast, logo, position, index)
        if contrast >= _MIN_LOGO_CONTRAST:
            break  # first candidate (highest library priority) that reads well

    if best is None:
        return None, None

    contrast, logo, position, index = best
    logger.info(
        "composite_best_logo: candidate #%d wins (contrast %.0f)", index, contrast
    )
    base.alpha_composite(logo, position)
    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG")
    return out.getvalue(), index


def composite_logo(
    base_png: bytes,
    logo_png: bytes,
    *,
    placement: str = "top_left_reserved",
    width_ratio: float = 0.14,
    max_height_ratio: float = 0.10,
    padding_ratio: float = 0.04,
) -> bytes:
    """Overlay the REAL logo onto a generated creative, returning PNG bytes.

    Single-candidate variant kept for compatibility; prefer
    `composite_best_logo` which validates visibility and contrast.
    """
    from PIL import Image

    base = Image.open(io.BytesIO(base_png)).convert("RGBA")
    logo = _prepare_logo(logo_png)
    if logo is None:
        return base_png

    bw, bh = base.size
    pad = int(bw * padding_ratio)

    if placement == "top_left_reserved":
        zone_w, zone_h = _reserved_zone(bw, bh, pad)
        logo = _fit_logo_to_zone(logo, zone_w, zone_h)
        position = _top_left_reserved_position(bw, bh, logo.width, logo.height, pad)
    else:
        target_w = max(1, int(bw * width_ratio))
        scale = target_w / logo.width
        target_h = int(logo.height * scale)

        max_h = int(bh * max_height_ratio)
        if target_h > max_h:
            scale = max_h / logo.height
            target_w = max(1, int(logo.width * scale))
            target_h = max_h

        logo = logo.resize((max(1, target_w), max(1, target_h)), Image.LANCZOS)
        position = _cleanest_corner(base, logo.width, logo.height, pad)

    base.alpha_composite(logo, position)

    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG")
    return out.getvalue()
