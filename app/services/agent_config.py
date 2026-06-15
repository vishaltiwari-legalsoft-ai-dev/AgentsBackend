"""Graphic Designer agent settings exposed to the console UI."""

from __future__ import annotations

from app.config import settings

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
    default = settings.openrouter_image_model
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


def public_settings_payload() -> dict:
    defaults = normalize_settings(None, None, None)
    return {
        "agent_id": "a1",
        "agent_name": "Graphic Designer",
        "image_models": IMAGE_MODELS,
        "abilities": ABILITIES,
        "tools": TOOLS,
        "defaults": defaults,
    }
