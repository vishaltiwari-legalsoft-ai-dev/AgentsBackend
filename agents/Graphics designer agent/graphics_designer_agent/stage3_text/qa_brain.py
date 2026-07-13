"""QA brain — the Text Optimizer's preservation verifier (spec 2026-07-14).

Compares the deterministic composite (ground truth) against a polished result
and answers ONE question: did the polish pass violate the preservation rules
(text verbatim + font shapes, existing elements, gradient, photo)?

Never raises: any failure (no key, mock provider, timeout, malformed JSON
twice) returns ``None``, which callers treat as "QA unavailable" — the polished
image ships badged ``qa: skipped`` rather than being blocked (honest, not
fake-strict).
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import logging
import os
import re

logger = logging.getLogger("graphics_designer.stage3.qa_brain")

_VISION_MAX_DIM = 768
_VISION_TIMEOUT_S = 45
_MAX_ATTEMPTS = 2  # one retry on malformed JSON, then give up

_CHECKS = ("text_ok", "elements_ok", "gradient_ok", "photo_ok")


def check(composite_png: bytes, polished_png: bytes, layout_desc: str) -> dict | None:
    """``{"passed": bool, "violations": [str]}`` or ``None`` when unavailable."""
    if not _vision_available():
        return None
    images = [_downscale_png(composite_png, _VISION_MAX_DIM),
              _downscale_png(polished_png, _VISION_MAX_DIM)]
    prompt = _build_prompt(layout_desc)
    for attempt in range(_MAX_ATTEMPTS):
        try:
            out = _call_model(prompt, images)
        except Exception:
            logger.warning("vision QA call failed; treating as unavailable", exc_info=True)
            return None
        verdict = _parse(out)
        if verdict is not None:
            return verdict
        logger.info("vision QA returned malformed JSON (attempt %d)", attempt + 1)
    return None


def _vision_available() -> bool:
    """Skipped outright on the offline mock provider or without a key, so tests
    and offline dev never touch the network (same rule as placement_brain)."""
    if (os.environ.get("GD_IMAGE_PROVIDER") or "").strip().lower() == "mock":
        return False
    try:
        from ..providers import _openrouter_key_configured  # lazy — avoids import cycles
    except Exception:
        return False
    return _openrouter_key_configured()


def _call_model(prompt: str, images_png: list[bytes]) -> str:
    """One two-image vision request, bounded by ``_VISION_TIMEOUT_S``. Test seam:
    tests monkeypatch this function instead of the OpenRouter client."""
    from app.services import runtime_config  # lazy — works without app
    from app.services.openrouter import analyze_images

    model = runtime_config.get("openrouter_vision_model")
    pairs = [(png, "image/png") for png in images_png]
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(analyze_images, prompt, pairs, model)
        return future.result(timeout=_VISION_TIMEOUT_S)


def _build_prompt(layout_desc: str) -> str:
    return (
        "You are a meticulous brand QA reviewer. Image 1 is the ORIGINAL composite "
        "(ground truth). Image 2 is a polished version that was ONLY allowed to refine "
        "lighting, sharpness and integration — additions of subtle emphasis are "
        "allowed, but nothing that already existed may change.\n\n"
        "The overlay should contain exactly this content:\n" + layout_desc + "\n\n"
        "Check image 2 against image 1:\n"
        "1. text_ok — every word identical, same font shapes and weights, fully legible\n"
        "2. elements_ok — every pre-existing shape/icon/button unchanged (nothing "
        "moved, removed or recolored; NEW additions are acceptable)\n"
        "3. gradient_ok — background gradient colours and direction unchanged\n"
        "4. photo_ok — the photo/subject content unchanged\n\n"
        'Reply with ONLY minified JSON: {"text_ok":true,"elements_ok":true,'
        '"gradient_ok":true,"photo_ok":true,"violations":["one short reason per '
        'failed check"]}. No prose.'
    )


def _parse(raw: str) -> dict | None:
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except Exception:
        return None
    if not isinstance(data, dict) or not all(k in data for k in _CHECKS):
        return None
    passed = all(bool(data[k]) for k in _CHECKS)
    raw_violations = data.get("violations") or []
    if not isinstance(raw_violations, list):
        raw_violations = [str(raw_violations)]
    violations = [str(v)[:160] for v in raw_violations[:4]]
    if not passed and not violations:
        violations = [f"{k} check failed" for k in _CHECKS if not data[k]]
    return {"passed": passed, "violations": [] if passed else violations}


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
