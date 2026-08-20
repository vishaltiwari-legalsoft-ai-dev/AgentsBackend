"""Live premium model catalog for the Agent Configuration dropdowns.

Fetches OpenRouter's public model list (``GET /api/v1/models``), keeps only
premium model families (Anthropic / OpenAI / Google / xAI / DeepSeek / Mistral
for text, plus the top image providers), classifies each into a tier —
``flagship`` / ``balanced`` / ``fast`` — and routes it to the right dropdown by
modality (image-output → image, image-input text → vision, else text).

The curated lists in :mod:`app.services.agent_config` remain the source of the
``recommended`` flags and descriptions, and the full offline fallback when the
fetch fails. Results are cached in-process for an hour; ``clear_cache`` resets
(used by tests and available to a future admin refresh button).
"""

from __future__ import annotations

import logging
import re
import time

import httpx

from app.services import agent_config

logger = logging.getLogger("agentos.model_catalog")

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_CACHE_TTL_SECONDS = 3600.0

TIER_ORDER = {"flagship": 0, "balanced": 1, "fast": 2}
TIER_LABELS = {
    "flagship": "Flagship — top quality",
    "balanced": "Balanced — strong, cheaper",
    "fast": "Fast — cheapest",
}

# Variants that are never a sane choice for a production agent.
_EXCLUDE_TOKENS = (":free", "-exp", "distill", ":extended")

# Premium text/reasoning families → tier. First match wins; anything that
# matches nothing is dropped (that's the premium filter).
_TEXT_FAMILIES: tuple[tuple[str, str], ...] = (
    (r"^anthropic/claude-(opus|fable|mythos)", "flagship"),
    (r"^anthropic/claude-sonnet", "balanced"),
    (r"^anthropic/claude-haiku", "fast"),
    (r"^openai/gpt-[0-9][\w.]*-nano", "fast"),
    (r"^openai/gpt-[0-9][\w.]*-mini", "balanced"),
    (r"^openai/o[0-9]", "flagship"),
    (r"^openai/gpt-[0-9]", "flagship"),
    (r"^google/gemini-[\w.]*-pro", "flagship"),
    (r"^google/gemini-[\w.]*-flash-lite", "fast"),
    (r"^google/gemini-[\w.]*-flash", "balanced"),
    (r"^x-ai/grok-[\w.]*-mini", "balanced"),
    (r"^x-ai/grok", "flagship"),
    (r"^deepseek/deepseek-(r1|v3|chat|reasoner)", "balanced"),
    (r"^mistralai/mistral-large", "balanced"),
)

# Premium image-generation families → tier.
_IMAGE_FAMILIES: tuple[tuple[str, str], ...] = (
    (r"^google/gemini-[\w.]*-pro-image", "flagship"),
    (r"^google/gemini-[\w.]*-flash-image", "fast"),
    (r"^openai/gpt-[\w.]*image", "flagship"),
    (r"^black-forest-labs/flux[\w.]*-max", "flagship"),
    (r"^black-forest-labs/flux", "balanced"),
    (r"^recraft/", "balanced"),
    (r"^bytedance/seedream", "balanced"),
    (r"^ideogram/", "balanced"),
)

_PROVIDER_NAMES = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "x-ai": "xAI",
    "deepseek": "DeepSeek",
    "mistralai": "Mistral",
    "black-forest-labs": "Black Forest Labs",
    "recraft": "Recraft",
    "bytedance": "ByteDance",
    "ideogram": "Ideogram",
}

_cache: tuple[float, dict[str, list[dict]]] | None = None


def clear_cache() -> None:
    global _cache
    _cache = None


def fetch_openrouter_models() -> list[dict] | None:
    """The raw OpenRouter model list, or None when unreachable (offline dev,
    provider outage) — callers fall back to the curated catalog."""
    try:
        resp = httpx.get(OPENROUTER_MODELS_URL, timeout=10)
        resp.raise_for_status()
        models = resp.json().get("data")
        return models if isinstance(models, list) else None
    except Exception as exc:  # noqa: BLE001 - any failure degrades to curated
        logger.warning("OpenRouter model list unavailable: %s", exc)
        return None


def _classify(model_id: str, families: tuple[tuple[str, str], ...]) -> str | None:
    lowered = model_id.lower()
    if any(tok in lowered for tok in _EXCLUDE_TOKENS):
        return None
    for pattern, tier in families:
        if re.match(pattern, lowered):
            return tier
    return None


def _modalities(model: dict) -> tuple[set[str], set[str]]:
    arch = model.get("architecture") or {}
    inputs = set(arch.get("input_modalities") or [])
    outputs = set(arch.get("output_modalities") or [])
    if not inputs and not outputs:
        # Legacy "text+image->text" shape.
        modality = str(arch.get("modality") or "")
        if "->" in modality:
            left, _, right = modality.partition("->")
            inputs = set(left.split("+")) if left else set()
            outputs = set(right.split("+")) if right else set()
    return inputs, outputs


def _provider_of(model_id: str) -> str:
    slug = model_id.split("/", 1)[0]
    return _PROVIDER_NAMES.get(slug, slug.replace("-", " ").title())


def _display_name(model: dict) -> str:
    name = str(model.get("name") or model.get("id") or "")
    # OpenRouter names come as "Provider: Model" — the dropdown shows the
    # provider separately, so keep just the model part.
    return name.split(": ", 1)[-1] or str(model.get("id"))


def _option(model: dict, tier: str) -> dict:
    mid = str(model["id"])
    return {
        "id": mid,
        "name": _display_name(model),
        "provider": _provider_of(mid),
        "tier": tier,
    }


def _sorted(options: list[dict]) -> list[dict]:
    return sorted(
        options,
        key=lambda o: (TIER_ORDER.get(str(o.get("tier")), 1), str(o["provider"]), str(o["name"])),
    )


def _curated_tier(field: str, model_id: str) -> str:
    families = _IMAGE_FAMILIES if field == "openrouter_image_model" else _TEXT_FAMILIES
    return _classify(model_id, families) or "balanced"


def _merge_curated(field: str, live: list[dict]) -> list[dict]:
    """Overlay curated metadata (recommended flag, description, display name)
    onto the live options, and append curated entries the live list lacks so
    the vetted defaults are always offerable."""
    by_id = {o["id"]: o for o in live}
    for curated in agent_config.MODEL_CATALOG.get(field, []):
        cid = str(curated["id"])
        target = by_id.get(cid)
        if target is None:
            target = {
                "id": cid,
                "name": str(curated["name"]),
                "provider": str(curated["provider"]),
                "tier": _curated_tier(field, cid),
            }
            live.append(target)
            by_id[cid] = target
        else:
            target["name"] = str(curated["name"])
        if curated.get("description"):
            target["description"] = curated["description"]
        if curated.get("recommended"):
            target["recommended"] = True
    return live


def _build_catalog() -> dict[str, list[dict]]:
    models = fetch_openrouter_models()

    if models is None:
        text: list[dict] = []
        vision: list[dict] = []
        image: list[dict] = []
    else:
        text, vision, image = [], [], []
        for model in models:
            mid = str(model.get("id") or "")
            if not mid:
                continue
            inputs, outputs = _modalities(model)
            if "image" in outputs:
                tier = _classify(mid, _IMAGE_FAMILIES)
                if tier:
                    image.append(_option(model, tier))
                continue
            tier = _classify(mid, _TEXT_FAMILIES)
            if not tier:
                continue
            option = _option(model, tier)
            text.append(option)
            if "image" in inputs:
                vision.append(dict(option))

    catalog = {
        "openrouter_model": _merge_curated("openrouter_model", list(text)),
        "openrouter_fast_model": _merge_curated("openrouter_fast_model", [dict(o) for o in text]),
        "gd_planner_model": _merge_curated("gd_planner_model", [dict(o) for o in text]),
        # a11 Browser Agent planner. Was absent here, so allowed_ids() was
        # empty and POST /api/admin/agents/a11 rejected every planner model
        # as "not an allowed browser_planner_model" - a dead panel switch.
        "browser_planner_model": _merge_curated(
            "browser_planner_model", [dict(o) for o in text]
        ),
        "openrouter_vision_model": _merge_curated("openrouter_vision_model", vision),
        "openrouter_image_model": _merge_curated("openrouter_image_model", image),
        # GD Stage-3 polish fan-out: same image-model universe, own field so
        # the panel can point it at a premium edit model independently.
        "gd_polish_image_model": _merge_curated(
            "gd_polish_image_model", [dict(o) for o in image]
        ),
    }
    return {field: _sorted(options) for field, options in catalog.items()}


def get_catalog() -> dict[str, list[dict]]:
    """The tier-classified catalog per model field, cached for an hour."""
    global _cache
    now = time.monotonic()
    if _cache and (now - _cache[0]) < _CACHE_TTL_SECONDS:
        return _cache[1]
    catalog = _build_catalog()
    _cache = (now, catalog)
    return catalog


def allowed_ids(field: str) -> set[str]:
    return {str(o["id"]) for o in get_catalog().get(field, [])}
