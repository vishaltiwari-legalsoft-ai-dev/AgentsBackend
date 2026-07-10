"""Placement brain — the Stage-3 vision micro-subagent.

Looks at the ACTUAL approved Stage-2 image (not just its metadata) and returns a
structured art-direction judgment for the studio's text overlay: which zone has
clean negative space, whether the copy should be dark or white there, and how
busy the image is (which drives a size step-down). The deterministic arranger
(`suggestions.suggest_placement`) turns that judgment into exact fractional
coordinates — the model never emits pixel positions, because vision models are
good at "where is the empty space" and bad at precise coordinates.

Never raises to the caller: any failure (no key, mock provider, timeout,
unparseable JSON) returns ``None`` and the arranger falls back to its existing
metadata heuristics, byte-identical to today's behaviour.
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import logging
import os
import re

logger = logging.getLogger("graphics_designer.stage3.placement_brain")

VALID_ZONES = ("left", "right", "center", "top", "bottom")
VALID_COLORS = ("dark", "white")
VALID_DENSITIES = ("clean", "moderate", "busy")

# Downscale before sending to the vision model — placement only needs the gist,
# and a ~768px image is far cheaper/faster than the native base.
_VISION_MAX_DIM = 768
# The background studio call must never hang the UI; give the model this long.
# Sized for the FAST vision model (gpt-4o-mini class) — the default reasoning
# model can take far longer than this with an image attached, which is why
# `_call_model` explicitly requests the vision model instead of the default.
_VISION_TIMEOUT_S = 45
# One retry on malformed JSON, then give up and fall back.
_MAX_ATTEMPTS = 2


def decide(
    image_bytes: bytes,
    *,
    headline: str = "",
    subheading_count: int = 0,
    cta: str = "",
    element_placement: str | None = None,
) -> dict | None:
    """Vision judgment for the Stage-3 copy stack on THIS image, or ``None``.

    Returns ``{"zone", "text_color", "density", "reason"}`` with every field
    validated against the enums above.
    """
    if not _vision_available():
        return None
    small = _downscale_png(image_bytes, _VISION_MAX_DIM)
    prompt = _build_prompt(headline, subheading_count, cta, element_placement)
    for attempt in range(_MAX_ATTEMPTS):
        try:
            out = _call_model(prompt, small)
        except Exception:
            logger.warning("vision placement call failed; falling back", exc_info=True)
            return None
        judgment = _parse(out)
        if judgment is not None:
            return judgment
        logger.info("vision placement returned malformed JSON (attempt %d)", attempt + 1)
    return None


def _vision_available() -> bool:
    """Vision is skipped outright on the offline mock provider or without a key,
    so tests and offline dev never touch the network."""
    if (os.environ.get("GD_IMAGE_PROVIDER") or "").strip().lower() == "mock":
        return False
    try:
        from ..providers import _openrouter_key_configured  # lazy — avoids import cycles
    except Exception:
        return False
    return _openrouter_key_configured()


def _call_model(prompt: str, image_png: bytes) -> str:
    """One vision request, bounded by ``_VISION_TIMEOUT_S``. Test seam: tests
    monkeypatch this function instead of the OpenRouter client.

    Explicitly requests the FAST vision model: ``analyze_images`` otherwise
    defaults to the heavyweight reasoning model, which regularly needs longer
    than any UI-friendly timeout when an image is attached — the judgment here
    (zone/colour/density) doesn't need reasoning-model quality."""
    from app.services import runtime_config  # lazy — works without app
    from app.services.openrouter import analyze_images

    model = runtime_config.get("openrouter_vision_model")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(analyze_images, prompt, [(image_png, "image/png")], model)
        return future.result(timeout=_VISION_TIMEOUT_S)


def _build_prompt(headline: str, subheading_count: int, cta: str,
                  element_placement: str | None) -> str:
    pieces = []
    if headline:
        pieces.append(f'a bold headline ("{headline[:80]}")')
    else:
        pieces.append("a bold headline")
    if subheading_count:
        pieces.append(f"{subheading_count} sub-heading line(s)")
    if cta:
        pieces.append(f'a CTA button pill ("{cta[:40]}")')
    block = ", ".join(pieces)
    prior = (
        f" The subject was requested at the '{element_placement}' cell, but trust "
        "what you actually SEE in the image over that metadata."
        if element_placement and element_placement != "auto" else ""
    )
    return (
        "You are a senior art director laying out a social-media ad. The attached "
        "image is the FINISHED background — a brand gradient with a subject "
        "(usually a person or object). A text block (" + block + ") must be placed "
        "in the CLEAN negative space so it NEVER overlaps the subject's face or "
        "body and the composition looks professionally balanced." + prior + "\n\n"
        "Decide:\n"
        "1. zone — where the text block should sit: left | right | center | top | bottom. "
        "Pick the zone with the most empty space.\n"
        "2. secondary_zone — your second-best zone, in case the first is unusable.\n"
        "3. text_color — dark (over a light area) or white (over a dark or busy "
        "area), for maximum legibility in your chosen zone.\n"
        "4. density — how visually busy the image is overall: clean | moderate | busy. "
        "A busy image needs smaller, tighter copy.\n"
        "5. reason — ONE short sentence explaining the placement, written for the "
        "designer (e.g. \"clean sky on the right keeps the headline off her face\").\n\n"
        'Reply with ONLY minified JSON: {"zone":"left|right|center|top|bottom",'
        '"secondary_zone":"...","text_color":"dark|white",'
        '"density":"clean|moderate|busy","reason":"..."}. No prose.'
    )


def _parse(raw: str) -> dict | None:
    """Validate the model's reply against the enums; None on anything off-spec."""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    zone = str(data.get("zone", "")).strip().lower()
    if zone not in VALID_ZONES:
        zone = str(data.get("secondary_zone", "")).strip().lower()
    if zone not in VALID_ZONES:
        return None
    color = str(data.get("text_color", "")).strip().lower()
    if color not in VALID_COLORS:
        # "light" is a common synonym the model may emit.
        color = "white" if color.startswith("l") else "dark"
    density = str(data.get("density", "")).strip().lower()
    if density not in VALID_DENSITIES:
        density = "moderate"
    reason = str(data.get("reason", "")).strip()[:200]
    return {"zone": zone, "text_color": color, "density": density, "reason": reason}


def _downscale_png(image_bytes: bytes, max_dim: int) -> bytes:
    """Shrink an image so its longest side is ``max_dim`` (cheaper vision call).
    Returns the original bytes unchanged if Pillow isn't available or it's small."""
    try:
        from PIL import Image
    except Exception:
        return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if max(img.size) <= max_dim:
            return image_bytes
        img = img.convert("RGB")
        img.thumbnail((max_dim, max_dim))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return image_bytes
