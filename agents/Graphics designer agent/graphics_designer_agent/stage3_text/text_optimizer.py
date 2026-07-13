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
