"""Graphic Designer agent settings exposed to the console UI."""

from __future__ import annotations

from app.services import runtime_config

IMAGE_MODELS: list[dict[str, str | bool]] = [
    {
        "id": "google/gemini-3-pro-image-preview",
        "name": "Gemini 3 Pro Image",
        "provider": "Google",
        "description": "Best for on-brand creatives with logo and kit references.",
        "recommended": True,
    },
    {
        "id": "google/gemini-2.5-flash-image",
        "name": "Gemini 2.5 Flash Image",
        "provider": "Google",
        "description": "Fast image generation with text and image output.",
    },
    {
        "id": "black-forest-labs/flux.2-max",
        "name": "Flux 2 Max",
        "provider": "Black Forest Labs",
        "description": "High-quality scenes and backgrounds.",
    },
    {
        "id": "black-forest-labs/flux.2-pro",
        "name": "Flux 2 Pro",
        "provider": "Black Forest Labs",
        "description": "Professional-grade image generation.",
    },
    {
        "id": "openai/gpt-5-image",
        "name": "GPT-5 Image",
        "provider": "OpenAI",
        "description": "OpenAI image generation with strong prompt following.",
    },
    {
        "id": "openai/gpt-4o",
        "name": "GPT-4o",
        "provider": "OpenAI",
        "description": "Multimodal model with image output support.",
    },
    {
        "id": "recraft/recraft-v3",
        "name": "Recraft V3",
        "provider": "Recraft",
        "description": "Vector-style graphics and brand visuals.",
    },
]

# Curated text/reasoning + vision model choices for the creator's Agent
# Configuration panel. These power dropdowns (instead of free-text model ids) so
# a typo can't silently break generation. Ids are OpenRouter model slugs.
TEXT_MODELS: list[dict[str, str | bool]] = [
    {
        "id": "anthropic/claude-opus-4.6",
        "name": "Claude Opus 4.6",
        "provider": "Anthropic",
        "description": "Top-tier reasoning — best for brand synthesis and art direction.",
        "recommended": True,
    },
    {
        "id": "anthropic/claude-sonnet-4.5",
        "name": "Claude Sonnet 4.5",
        "provider": "Anthropic",
        "description": "Fast, capable all-rounder. Great default for cheaper tasks.",
    },
    {
        "id": "anthropic/claude-haiku-4.5",
        "name": "Claude Haiku 4.5",
        "provider": "Anthropic",
        "description": "Fastest/cheapest — good for trivial parsing.",
    },
    {
        "id": "openai/gpt-5",
        "name": "GPT-5",
        "provider": "OpenAI",
        "description": "Strong general reasoning model.",
    },
    {
        "id": "google/gemini-3-pro",
        "name": "Gemini 3 Pro",
        "provider": "Google",
        "description": "Long-context reasoning with strong instruction following.",
    },
]

VISION_MODELS: list[dict[str, str | bool]] = [
    {
        "id": "openai/gpt-4o-mini",
        "name": "GPT-4o mini",
        "provider": "OpenAI",
        "description": "Cheap, fast OCR / image reading.",
        "recommended": True,
    },
    {
        "id": "openai/gpt-4o",
        "name": "GPT-4o",
        "provider": "OpenAI",
        "description": "Higher-accuracy image understanding.",
    },
    {
        "id": "anthropic/claude-sonnet-4.5",
        "name": "Claude Sonnet 4.5",
        "provider": "Anthropic",
        "description": "Multimodal reading with strong reasoning.",
    },
    {
        "id": "google/gemini-3-pro",
        "name": "Gemini 3 Pro",
        "provider": "Google",
        "description": "Multimodal with long context.",
    },
]

# Maps each runtime-config model field to the catalog the UI should offer for it.
MODEL_CATALOG: dict[str, list[dict[str, str | bool]]] = {
    "openrouter_image_model": IMAGE_MODELS,
    # Stage-3 polish fan-out (GD): an image-EDIT model, same catalog as above.
    "gd_polish_image_model": IMAGE_MODELS,
    "openrouter_model": TEXT_MODELS,
    "openrouter_fast_model": TEXT_MODELS,
    "openrouter_vision_model": VISION_MODELS,
    "gd_planner_model": TEXT_MODELS,
    "browser_planner_model": TEXT_MODELS,
}

# The agents the Agent Configuration panel manages: ONLY the ones actually
# wired to the backend (ids match newfrontend/lib/console-data.ts). ``fields``
# lists the model overrides that agent truly consumes — the panel renders only
# those dropdowns, and the save endpoint rejects anything else, so there are no
# dead switches. Adding an agent here is a claim that its engine passes its
# ``agent_id`` into app.services.openrouter (see tests/test_agent_model_overrides.py).
AGENTS: list[dict[str, str | bool | list[str]]] = [
    {
        "id": "a1",
        "name": "Graphic Designer",
        "role": "Brand & visual assets",
        "category": "design",
        "live": True,
        "fields": [
            "openrouter_model",
            "openrouter_fast_model",
            "openrouter_image_model",
            "openrouter_vision_model",
            "gd_planner_model",
            # Stage-3 polish fan-out runs a separate (premium) image-edit
            # model from Stages 1-2 — see providers._polish_model.
            "gd_polish_image_model",
        ],
    },
    {
        "id": "a6",
        "name": "Marketing Research",
        "role": "Campaigns, competitors & funnel",
        "category": "data",
        "live": True,
        "fields": ["openrouter_model"],
    },
    {
        "id": "a9",
        "name": "Blog Writer",
        "role": "Deep-research blog drafts",
        "category": "copy",
        "live": True,
        "fields": ["openrouter_model"],
    },
    {
        "id": "a10",
        "name": "GEO",
        "role": "AI answer visibility",
        "category": "seo",
        "live": True,
        # GEO's LLM use is parsing-grade (prompt universe, sentiment) via
        # sources.llm_json(agent_id="a10") — fast model only. Engine polling
        # itself goes to the providers' own APIs, not OpenRouter.
        "fields": ["openrouter_fast_model"],
    },
    {
        "id": "a11",
        "name": "Browser Agent",
        "role": "Web copilot in your Chrome",
        "category": "data",
        "live": True,
        # Planner model breaks the task down once per run (browser_agent.planner);
        # the reasoning model decides each act-loop step (browser_agent.brain);
        # the fast model writes the tab digests (browser_agent.digest, fast=True).
        # The Chrome extension is a thin sensor/actuator — every LLM call is here.
        "fields": ["browser_planner_model", "openrouter_model", "openrouter_fast_model"],
    },
]

AGENT_IDS = {str(a["id"]) for a in AGENTS}
AGENT_FIELDS: dict[str, list[str]] = {str(a["id"]): list(a["fields"]) for a in AGENTS}

# Display labels for the per-agent run tables (admin Database panel). Every
# agent that logs runs appears here — including ones absent from AGENTS above
# because they expose no model overrides (a2 uses shared SEO credentials).
AGENT_LABELS: dict[str, str] = {str(a["id"]): str(a["name"]) for a in AGENTS} | {
    "a2": "SEO Analyst",
}


ABILITIES: list[dict[str, str]] = [
    {
        "id": "generate_creatives",
        "name": "Generate creatives",
        "description": "Create brochures, flyers, social posts, and on-brand artwork.",
    },
    {
        "id": "analyze_brand",
        "name": "Analyze brand website",
        "description": "Study the brand's website to build a style profile (colors, fonts, tone, audience).",
    },
    {
        "id": "brief_intake",
        "name": "Requirements intake",
        "description": "Ask for the creative type and purpose before generating.",
    },
]

TOOLS: list[dict[str, str | bool]] = [
    {
        "id": "brand_kit",
        "name": "Brand kit",
        "description": "Fetch all brand logos and style references from your library.",
        "default": True,
    },
    {
        "id": "web_search",
        "name": "Website analysis",
        "description": "Find and analyze the brand's official website.",
        "default": True,
    },
    {
        "id": "file_attachments",
        "name": "File attachments",
        "description": "Read text from PDF, DOCX, and image uploads.",
        "default": True,
    },
    {
        "id": "logo_composite",
        "name": "Logo compositing",
        "description": "Overlay the exact brand logo onto generated creatives.",
        "default": True,
    },
    {
        "id": "canva_export",
        "name": "Canva export",
        "description": "Send finished creatives to Canva for editing.",
        "default": True,
    },
]

IMAGE_MODEL_IDS = {str(m["id"]) for m in IMAGE_MODELS}
ABILITY_IDS = {a["id"] for a in ABILITIES}
TOOL_IDS = {t["id"] for t in TOOLS}


def default_enabled_tools() -> list[str]:
    return [str(t["id"]) for t in TOOLS if t.get("default", True)]


def default_enabled_abilities() -> list[str]:
    return [a["id"] for a in ABILITIES]


def default_image_model() -> str:
    """The image model a new agent config starts from.

    Resolves through :mod:`app.services.runtime_config` (global admin
    override -> environment), NOT ``settings`` directly: reading the env
    value here made an admin's panel-set image model invisible to every
    default and to ``normalize_settings``.
    """
    default = runtime_config.get("openrouter_image_model")
    if default in IMAGE_MODEL_IDS:
        return default
    return str(IMAGE_MODELS[0]["id"])


def normalize_settings(
    image_model: str | None,
    enabled_tools: list[str] | None,
    enabled_abilities: list[str] | None,
) -> dict[str, list[str] | str]:
    model = image_model if image_model in IMAGE_MODEL_IDS else default_image_model()
    tools = [t for t in (enabled_tools or default_enabled_tools()) if t in TOOL_IDS]
    if not tools:
        tools = default_enabled_tools()
    abilities = [
        a for a in (enabled_abilities or default_enabled_abilities()) if a in ABILITY_IDS
    ]
    if not abilities:
        abilities = default_enabled_abilities()
    if "generate_creatives" not in abilities:
        abilities.insert(0, "generate_creatives")
    return {
        "image_model": model,
        "enabled_tools": tools,
        "enabled_abilities": abilities,
    }
