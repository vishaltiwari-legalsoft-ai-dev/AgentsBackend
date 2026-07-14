"""Text Optimizer — the Stage-3 agent (spec 2026-07-14).

Font resolution: an element whose font is the literal ``"auto"`` gets a weight
picked from the brand's font pool (``pack.font_variants`` — variants within the
locked family) by role, stepped down when the vision judgment says the image is
busy. Explicit user fonts always win, and the family can never change by
construction. Resolution builds a VIEW of the run; the stored config is never
mutated — chosen fonts are recorded on the attempt instead.

``optimize`` is the 3-style polish fan-out: the deterministic composite is sent
to the image model once per style recipe, each result is QA-gated, and any
result that isn't genuinely the model's output ships as the untouched composite
badged ``ai: False`` + ``fallback_reason`` — never a fake-AI badge.
"""

from __future__ import annotations

import concurrent.futures
import copy
import logging

from . import polish_prompts, qa_brain

logger = logging.getLogger("graphics_designer.stage3.text_optimizer")

AUTO_FONT = "auto"

# Role → preferred weight inside the brand family. A busy image steps the
# headline down one notch so the copy doesn't shout over a full scene.
_ROLE_WEIGHT = {"headline": 800, "highlight": 800, "cta": 600, "subheading": 400}
_BUSY_HEADLINE_WEIGHT = 700

_MAX_POLISH_ATTEMPTS = 2  # first try + one retry with violations fed back

# Minimum WCAG-style contrast between the highlight's LIGHT gradient stop and
# the background it sits on; below this the guard swaps to the dark stop.
_MIN_HL_CONTRAST = 2.5


def _rel_lum(rgb: tuple[int, int, int]) -> float:
    def lin(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(rgb: tuple[int, int, int], bg_lum: float) -> float:
    la = _rel_lum(rgb)
    hi, lo = max(la, bg_lum), min(la, bg_lum)
    return (hi + 0.05) / (lo + 0.05)


def _hex_rgb(value: str) -> tuple[int, int, int]:
    h = value.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def ensure_highlight_contrast(layers: list[dict], base_png: bytes, pack) -> dict | None:
    """Deterministic legibility guard for the headline highlight (optimizer path
    only — the flag-off render stays byte-identical). Samples the base image
    behind the headline layer; when the brand gradient's LIGHT stop lacks
    contrast there, the highlight renders as the gradient's dark stop instead
    (or plain dark/white as a last resort). Mutates the layer in place and
    returns an honest ``{"from","to","reason"}`` record, or ``None`` when the
    gradient is already legible. Never raises."""
    head = next((l for l in layers
                 if l.get("id") == "headline" and l.get("type") == "text"), None)
    if not head or head.get("highlight_color") != "gradient":
        return None
    if not str(head.get("highlight") or "").strip():
        return None
    try:
        hl = pack.locked_colors["headline_highlight"]
        light_stop, dark_stop = _hex_rgb(hl["from"]), _hex_rgb(hl["to"])
    except Exception:  # noqa: BLE001 - pack without a gradient → nothing to guard
        return None
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(base_png)).convert("RGB")
        cw, ch = img.size
        x, y, w = float(head.get("x", 0.5)), float(head.get("y", 0.5)), float(head.get("w", 0.5))
        box = (max(0, int((x - w / 2) * cw)), max(0, int((y - 0.2) * ch)),
               min(cw, int((x + w / 2) * cw) or cw), min(ch, int((y + 0.2) * ch) or ch))
        region = img.crop(box) if box[0] < box[2] and box[1] < box[3] else img
        region.thumbnail((32, 32))
        px = list(region.getdata())
        mean = tuple(sum(c[i] for c in px) // len(px) for i in range(3))
        bg_lum = _rel_lum(mean)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - unreadable base → never block generation
        return None
    if _contrast(light_stop, bg_lum) >= _MIN_HL_CONTRAST:
        return None
    if _contrast(dark_stop, bg_lum) >= _MIN_HL_CONTRAST:
        new_color = pack.locked_colors["headline_highlight"]["to"]
        reason = "brand gradient's light stop is illegible on this background — using its dark stop"
    else:
        new_color = "dark" if bg_lum > 0.5 else "white"
        reason = "neither gradient stop is legible on this background"
    head["highlight_color"] = new_color
    logger.info("highlight contrast guard: %s", reason)
    return {"from": "gradient", "to": new_color, "reason": reason}


def pick_variant(font_variants: list[dict], weight: int) -> str:
    """Name of the upright variant closest to ``weight`` (obliques excluded)."""
    upright = [v for v in font_variants if (v.get("style") or "normal") == "normal"]
    pool = upright or font_variants
    best = min(pool, key=lambda v: abs(int(v.get("weight", 400)) - int(weight)))
    return best["name"]


def _role_weight(role: str, busy: bool) -> int:
    if role in ("headline", "highlight") and busy:
        return _BUSY_HEADLINE_WEIGHT
    return _ROLE_WEIGHT.get(role, _ROLE_WEIGHT["subheading"])


def resolve_fonts(run: dict, pack, judgment: dict | None = None) -> dict[str, str]:
    """element-key → concrete brand font name, for every element set to AUTO."""
    cfg = run.get("config") or {}
    styles = cfg.get("element_styles") or {}
    busy = bool(judgment) and judgment.get("density") == "busy"
    out: dict[str, str] = {}
    for key in ("headline", "highlight", "cta"):
        if (styles.get(key) or {}).get("font") == AUTO_FONT:
            out[key] = pick_variant(pack.font_variants, _role_weight(key, busy))
    for i, s in enumerate(cfg.get("subheadings") or []):
        if s.get("font") == AUTO_FONT:
            out[f"subheading-{i}"] = pick_variant(pack.font_variants, _ROLE_WEIGHT["subheading"])
    if cfg.get("font") == AUTO_FONT:
        out["font"] = pick_variant(pack.font_variants, _ROLE_WEIGHT["subheading"])
    return out


def resolved_fonts_view(run: dict, pack, judgment: dict | None = None) -> tuple[dict, dict[str, str]]:
    """A run VIEW with every AUTO font replaced by a concrete brand variant.

    Returns ``(view, chosen)``. When nothing is AUTO the original run object is
    returned untouched (identity), so the common path costs nothing."""
    chosen = resolve_fonts(run, pack, judgment)
    if not chosen:
        return run, {}
    cfg = copy.deepcopy(run["config"])
    styles = cfg.get("element_styles") or {}
    for key in ("headline", "highlight", "cta"):
        if key in chosen and key in styles:
            styles[key]["font"] = chosen[key]
    for i, s in enumerate(cfg.get("subheadings") or []):
        name = chosen.get(f"subheading-{i}")
        if name:
            s["font"] = name
    if "font" in chosen:
        cfg["font"] = chosen["font"]
    return {**run, "config": cfg}, chosen


def optimize(*, composite_png: bytes, layers: list[dict], provider, notes: str = "",
             width: int = 1080, height: int = 1350,
             aspect_ratio: str | None = None, image_size: str | None = None) -> list[dict]:
    """Fan the composite out to the three style recipes, QA-gate each result.

    Returns one result per recipe (order = STYLE_RECIPES):
    ``{"style","label","png","ai","fallback_reason","qa","prompt"}``. Honest by
    construction: a result that isn't genuinely the model's output has
    ``ai: False`` + a ``fallback_reason``, and its ``png`` is the untouched
    deterministic composite. Never raises."""
    layout_desc = polish_prompts.describe_layout(layers)

    def one(recipe: dict) -> dict:
        base_prompt = polish_prompts.build_polish_prompt(recipe["key"], layout_desc, notes)
        result = {"style": recipe["key"], "label": recipe["label"], "prompt": base_prompt}
        violations: list[str] = []
        for _ in range(_MAX_POLISH_ATTEMPTS):
            prompt = base_prompt if not violations else (
                base_prompt
                + "\n\nYOUR PREVIOUS ATTEMPT VIOLATED THESE RULES — fix them:\n- "
                + "\n- ".join(violations)
            )
            try:
                png, _mime = provider.generate(
                    prompt,
                    reference_images=[(composite_png, "image/png")],
                    width=width, height=height,
                    aspect_ratio=aspect_ratio, image_size=image_size,
                )
            except Exception:
                logger.warning("polish call failed for %s", recipe["key"], exc_info=True)
                return {**result, "png": composite_png, "ai": False,
                        "fallback_reason": "image model call failed", "qa": "not_run"}
            verdict = qa_brain.check(composite_png, png, layout_desc)
            if verdict is None:
                return {**result, "png": png, "ai": True, "fallback_reason": None,
                        "qa": "skipped", "prompt": prompt}
            if verdict["passed"]:
                return {**result, "png": png, "ai": True, "fallback_reason": None,
                        "qa": "passed", "prompt": prompt}
            violations = verdict["violations"] or ["preservation check failed"]
        return {**result, "png": composite_png, "ai": False,
                "fallback_reason": ("QA kept failing: " + "; ".join(violations))[:300],
                "qa": "failed"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(polish_prompts.STYLE_RECIPES)) as pool:
        return list(pool.map(one, polish_prompts.STYLE_RECIPES))
