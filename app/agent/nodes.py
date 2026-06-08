"""LangGraph node implementations for the AgentOS workflow.

Each node takes the current `AgentState` and returns a partial state update.
LLM calls (OpenRouter) degrade gracefully to deterministic logic on failure so a
transient model error never breaks the whole pipeline.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent import prompts
from app.agent.state import AgentState
from app.config import settings
from app.services import firestore_repo, imaging, storage
from app.services.openrouter import generate_image, get_llm

logger = logging.getLogger("agentos.agent")


def select_logo(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the brand's primary logo from its creatives.

    A creative only qualifies as a logo if its filename contains 'logo' or it
    lives in a logos/SVGs folder — otherwise we return None rather than passing
    off an arbitrary image (e.g. a 3D render frame) as the logo. Among
    qualifiers, vector files and shallower paths win.
    """
    best: dict[str, Any] | None = None
    best_score = 0.0
    for c in samples:
        name = (c.get("file_name") or "").lower()
        ftype = c.get("file_type") or ""
        is_svg = ftype == "image/svg+xml" or name.endswith(".svg")
        is_png = ftype == "image/png" or name.endswith(".png")
        has_logo_word = "logo" in name
        in_logo_dir = "/logos/" in name or "/logo/" in name
        in_svg_dir = "/svgs/" in name

        if not (has_logo_word or in_logo_dir or (in_svg_dir and is_svg)):
            continue
        if not (is_svg or is_png):
            continue  # logos must be renderable images, not EXR/PDF/etc.

        score = 0.0
        if has_logo_word:
            score += 4
        if is_svg:
            score += 2
        elif is_png:
            score += 1
        if in_logo_dir:
            score += 2
        elif in_svg_dir:
            score += 1
        score -= name.count("/") * 0.1  # prefer shallower paths
        if score > best_score:
            best_score, best = score, c
    return best if best_score > 0 else None


def retrieve_context(state: AgentState) -> dict[str, Any]:
    """Step 2/3 — resolve the brand, its samples, and its matching logo."""
    message = state["message"]
    brand_id = state.get("brand_id")
    try:
        if brand_id:
            brand = firestore_repo.get_brand(brand_id)
        else:
            lower = message.lower()
            brand = next(
                (
                    b
                    for b in firestore_repo.list_brands()
                    if b["brand_name"].lower() in lower
                ),
                None,
            )
        # Pull more samples than the default 50 so the LLM has richer context
        # and the analysis gallery has variety to pick from.
        samples = (
            firestore_repo.list_creatives_by_brand(brand["id"], limit=200)
            if brand
            else []
        )

        # Identify and sign the brand's logo so it can be exported to Canva.
        logo: dict[str, Any] | None = None
        chosen = select_logo(samples)
        if chosen:
            try:
                logo = {
                    "file_name": chosen.get("file_name", "logo"),
                    "view_url": storage.signed_url_for_gs_uri(chosen["file_url"]),
                    "gs_uri": chosen["file_url"],
                }
            except Exception as exc:  # noqa: BLE001 - logo is best-effort
                logger.warning("could not sign logo url: %s", exc)

        return {"brand": brand, "samples": samples, "logo": logo}
    except Exception as exc:  # noqa: BLE001 - DB optional during setup
        logger.warning("brand lookup skipped: %s", exc)
        return {"brand": None, "samples": [], "logo": None}


def categorize(state: AgentState) -> dict[str, Any]:
    """Segregate the request into a creative category (banner/flyer/brochure/...)."""
    message = state["message"]
    try:
        llm = get_llm(temperature=0)
        response = llm.invoke(
            [SystemMessage(prompts.CATEGORIZE_SYSTEM), HumanMessage(message)]
        )
        label = str(response.content).strip().lower()
        category = next((c for c in prompts.CATEGORIES if c in label), "banner")
    except Exception as exc:  # noqa: BLE001 - keyword fallback
        logger.warning("categorize LLM failed, using keywords: %s", exc)
        lower = message.lower()
        category = next((c for c in prompts.CATEGORIES if c in lower), "banner")
    return {"category": category}


def decide_intent(state: AgentState) -> dict[str, Any]:
    """Classify the request as 'analyze' or 'generate' (LLM with keyword fallback)."""
    message = state["message"]
    try:
        llm = get_llm(temperature=0)
        response = llm.invoke(
            [SystemMessage(prompts.INTENT_SYSTEM), HumanMessage(message)]
        )
        text = str(response.content).strip().lower()
        intent = "analyze" if "analyze" in text else "generate"
    except Exception as exc:  # noqa: BLE001 - fall back to keywords
        logger.warning("intent LLM failed, using keywords: %s", exc)
        lower = message.lower()
        intent = (
            "analyze"
            if "analyze" in lower and ("brand" in lower or "kit" in lower)
            else "generate"
        )
    return {"intent": intent}


_GALLERY_LIMIT = 12


def analyze_brand(state: AgentState) -> dict[str, Any]:
    """Produce a brand-kit summary + visual gallery for the interactive Brand Hub."""
    brand = state.get("brand")
    if not brand:
        return {
            "result": {
                "type": "message",
                "text": (
                    "I couldn't find a matching brand. Ingest the Brand Kits first "
                    "(`python -m app.ingest`) or name an existing brand."
                ),
            }
        }

    samples = state.get("samples", [])
    try:
        total = firestore_repo.count_creatives_by_brand(brand["id"])
    except Exception as exc:  # noqa: BLE001 - fall back to sample length
        logger.warning("creative count failed: %s", exc)
        total = len(samples)

    gallery = storage.to_gallery(samples, _GALLERY_LIMIT)

    summary = (
        f"{brand['brand_name']} has {total} stored creatives across logos, "
        "design files, and campaign assets. Browse the gallery below for the "
        "actual brand kit content."
    )
    try:
        llm = get_llm(temperature=0.3)
        file_list = ", ".join(c.get("file_name", "") for c in samples[:30])
        if file_list:
            response = llm.invoke(
                [
                    SystemMessage(
                        "You are a brand strategist. In 2 concise sentences, describe "
                        "what this brand kit contains and the kind of creative work "
                        "it suggests, based on the brand name and the file list."
                    ),
                    HumanMessage(
                        f"Brand: {brand['brand_name']}\nTotal files: {total}\n"
                        f"Sample file names: {file_list}"
                    ),
                ]
            )
            text = str(response.content).strip()
            if text:
                summary = text
    except Exception as exc:  # noqa: BLE001
        logger.warning("brand summary LLM failed: %s", exc)

    return {
        "result": {
            "type": "brand_analysis",
            "brand": brand,
            "creative_count": total,
            "summary": summary,
            "gallery": gallery,
        }
    }


def build_master_prompt(state: AgentState) -> dict[str, Any]:
    """Step 4 — synthesize the Master Prompt from the brief, brand kit, and
    category-specific reference examples (LLM with deterministic fallback)."""
    message = state["message"]
    brand = state.get("brand")
    samples = state.get("samples", [])
    category = state.get("category", "banner")
    brand_name = brand["brand_name"] if brand else None
    reference = prompts.brand_master_reference(brand_name)
    try:
        llm = get_llm(temperature=0.5)
        parts = [f"Asset category: {category}", f"User brief: {message}"]
        if reference:
            # Authoritative brand-wise master prompt + house rules from the CSV.
            parts.append(reference)
        else:
            # Fall back to whatever structured metadata the brand has.
            context = prompts.brand_context_block(brand, samples)
            if context:
                parts.append(f"Brand kit:\n{context}")
        human = "\n\n".join(parts)
        response = llm.invoke(
            [SystemMessage(prompts.MASTER_PROMPT_SYSTEM), HumanMessage(human)]
        )
        master_prompt = str(response.content).strip()
        if not master_prompt:
            raise ValueError("empty master prompt")
    except Exception as exc:  # noqa: BLE001
        logger.warning("master prompt LLM failed, using fallback: %s", exc)
        master_prompt = prompts.fallback_master_prompt(message, brand, samples)
    return {"master_prompt": master_prompt}


def _logo_reference(state: AgentState) -> list[tuple[bytes, str]] | None:
    """Fetch + rasterize the brand's real logo as a reference image (PNG)."""
    logo = state.get("logo")
    if not logo or not logo.get("gs_uri"):
        return None
    try:
        raw = storage.download_bytes(logo["gs_uri"])
        png = imaging.to_png_logo(raw, file_name=logo.get("file_name", ""))
        return [(png, "image/png")] if png else None
    except Exception as exc:  # noqa: BLE001 - logo reference is best-effort
        logger.warning("could not prepare logo reference: %s", exc)
        return None


def generate_assets(state: AgentState) -> dict[str, Any]:
    """Step 6 — Dual-Asset Generation Strategy.

    Variation A composites the brand's EXACT logo (passed as a reference image)
    when available, so the model uses the real mark instead of inventing one.
    Variation B keeps a blank placeholder for precise logo placement in Canva.
    """
    master_prompt = state["master_prompt"]
    logo_ref = _logo_reference(state)

    if logo_ref:
        logo_bytes, logo_mime = generate_image(
            prompts.with_reference_logo_instruction(master_prompt),
            reference_images=logo_ref,
        )
    else:
        logo_bytes, logo_mime = generate_image(
            prompts.with_logo_instruction(master_prompt)
        )

    ph_bytes, ph_mime = generate_image(
        prompts.with_placeholder_instruction(master_prompt)
    )
    return {
        "images": {
            "with_logo": {"bytes": logo_bytes, "mime": logo_mime},
            "with_placeholder": {"bytes": ph_bytes, "mime": ph_mime},
        }
    }


def _persist_one(
    brand: dict[str, Any] | None, image: dict[str, Any], file_name: str
) -> tuple[str, str | None]:
    """Upload one generated variation; return (view_url, gs_uri).

    AI-generated assets are deliberately stored in the dedicated `generated/`
    GCS prefix and are NEVER recorded in Firestore. This guarantees the
    retrieval pipeline (which queries Firestore for brand samples) can only
    ever surface human-curated brand-kit content, preventing the agent from
    conditioning on its own prior outputs and the resulting quality drift.
    The gs_uri lets chat history re-sign the URL after it expires.
    """
    data, mime = image["bytes"], image["mime"]
    if not storage.is_configured():
        return f"data:{mime};base64,{base64.b64encode(data).decode()}", None

    partition = brand["id"] if brand else "_unbranded"
    gs_uri, signed_url = storage.upload_generated(partition, file_name, data, mime)
    return signed_url, gs_uri


def persist(state: AgentState) -> dict[str, Any]:
    """Steps 7/8 — store both variations and assemble the API result."""
    brand = state.get("brand")
    images = state["images"]
    stamp = int(time.time())
    slug = re.sub(r"[^a-z0-9]+", "-", (brand["brand_name"] if brand else "asset").lower())

    with_logo_url, with_logo_gs = _persist_one(
        brand, images["with_logo"], f"{slug}-{stamp}-A-logo.png"
    )
    with_placeholder_url, with_placeholder_gs = _persist_one(
        brand, images["with_placeholder"], f"{slug}-{stamp}-B-placeholder.png"
    )

    canva_configured = bool(settings.canva_client_id and settings.canva_client_secret)
    return {
        "result": {
            "type": "assets",
            "brand": brand["brand_name"] if brand else None,
            "category": state.get("category", "banner"),
            "master_prompt": state["master_prompt"],
            "assets": {
                "with_logo": {
                    "url": with_logo_url,
                    "gs_uri": with_logo_gs,
                    "mime_type": images["with_logo"]["mime"],
                },
                "with_placeholder": {
                    "url": with_placeholder_url,
                    "gs_uri": with_placeholder_gs,
                    "mime_type": images["with_placeholder"]["mime"],
                },
            },
            "logo": state.get("logo"),
            "canva": {
                "configured": canva_configured,
                "import_url": "/api/canva/authorize" if canva_configured else None,
            },
        }
    }


def route_by_intent(state: AgentState) -> str:
    """Conditional edge selector after intent classification."""
    return "analyze" if state.get("intent") == "analyze" else "generate"
