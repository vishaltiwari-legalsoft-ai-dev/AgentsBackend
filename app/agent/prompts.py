"""Prompt construction for the agent (Buildguide.md, Phase 2 / Step 4).

The LLM-based synthesis lives in the nodes; these helpers provide the structured
context and the deterministic fallback used when the LLM is unavailable.
"""

from __future__ import annotations

import csv
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger("agentos.prompts")

MASTER_PROMPT_SYSTEM = (
    "You are a senior brand designer for the LS Design Productions house of brands. "
    "You receive a brand's CANONICAL master prompt + house rules (the source of "
    "truth), a user brief, and the target asset category.\n\n"
    "RULES:\n"
    "1. PRESERVE every concrete brand attribute EXACTLY as written in the reference: "
    "exact hex color codes, font names, logo rules, layout grammar, photography "
    "direction, and copy voice. Never summarize, drop, soften, or invent brand "
    "attributes — copy the specifics verbatim into your prompt.\n"
    "2. ADAPT the creative to the brief: choose the subject, scene, headline & "
    "sub-headline, offer/CTA, and composition that fit the user's request and the "
    "asset category, written in the brand's voice. Replace any [bracketed] slots "
    "with concrete choices.\n"
    "3. Output ONE complete, richly detailed image-generation prompt. Long and "
    "specific is GOOD — include the exact colors, fonts, aspect ratio, and layout "
    "from the reference. Output ONLY the final prompt text, no preamble or quotes."
)

INTENT_SYSTEM = (
    "Classify the user's request into exactly one word: 'analyze' if they want "
    "to inspect/summarize an existing brand kit (colors, fonts, guidelines), or "
    "'generate' if they want to create a new image/creative. Reply with only the "
    "single word."
)

# Supported creative categories the agent segregates requests into.
CATEGORIES = (
    "banner",
    "flyer",
    "brochure",
    "social_post",
    "advertisement",
    "poster",
)

CATEGORIZE_SYSTEM = (
    "Categorize the creative request into exactly one of these labels: "
    f"{', '.join(CATEGORIES)}. If none fit well, choose the closest. "
    "Reply with only the single label, lowercase."
)


# Columns in the brand-wise master-prompt CSV.
_COL_BRAND = "Brand"
_COL_DOC1 = "Document 1 Content (Rules / Brand Identity / Master Prompts)"
_COL_DOC2 = "Document 2 Content (Sales / Visual / Copy Ideology)"

# Generous caps so the full master prompt (with exact colors/fonts/layout) is
# passed to the model intact rather than truncated.
_UNIVERSAL_CAP = 4000
_BRAND_CAP = 12000


def _csv_path() -> Path:
    if settings.master_prompts_csv:
        return Path(settings.master_prompts_csv)
    return (
        Path(__file__).resolve().parent.parent
        / "data"
        / "Sample_master_prompt_brandwise - Sheet2.csv"
    )


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _clean_brand_cell(cell: str) -> str:
    """Reduce a CSV brand cell (e.g. '10. Practice Al / Lawvy - AI OS') to its
    core brand name ('practice al')."""
    cleaned = re.sub(r"^\s*\d+\.\s*", "", cell.strip())
    cleaned = re.split(r"\s[-/]\s", cleaned)[0]
    return _norm(cleaned)


@lru_cache(maxsize=1)
def _load_brand_prompts() -> dict[str, Any]:
    """Load the brand-wise master-prompt CSV (cached).

    Returns {"universal": str, "rows": [(clean_brand, full_cell, content), ...]}.
    Degrades to empty data if the CSV is missing/malformed so the agent keeps
    working.
    """
    path = _csv_path()
    universal = ""
    rows: list[tuple[str, str, str]] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                cell = (row.get(_COL_BRAND) or "").strip()
                if not cell:
                    continue
                doc1 = (row.get(_COL_DOC1) or "").strip()
                doc2 = (row.get(_COL_DOC2) or "").strip()
                content = f"{doc1}\n\n{doc2}".strip()
                if cell.lower().startswith("universal house rules"):
                    universal = content[:_UNIVERSAL_CAP]
                    continue
                if cell.lower().startswith("misc"):
                    continue
                rows.append((_clean_brand_cell(cell), _norm(cell), content))
    except FileNotFoundError:
        logger.warning("master prompt CSV not found at %s", path)
    except Exception as exc:  # noqa: BLE001 - never let CSV issues break a run
        logger.warning("failed to read master prompt CSV: %s", exc)
    return {"universal": universal, "rows": rows}


def _brand_variants(brand_name: str) -> set[str]:
    """Normalized forms of a brand name, tolerating the CSV's 'AI'->'Al' spelling."""
    base = _norm(brand_name)
    return {base, base.replace("ai", "al")}


def brand_master_reference(brand_name: Optional[str]) -> str:
    """Return the authoritative master-prompt + house-rules reference for a brand.

    Matches our Firestore brand name to the best CSV row (exact core-name match
    preferred, substring fallback) and returns the universal rules plus that
    brand's master prompt content. Empty string if there's no match.
    """
    if not brand_name:
        return ""
    data = _load_brand_prompts()
    rows: list[tuple[str, str, str]] = data["rows"]
    variants = _brand_variants(brand_name)

    best: tuple[float, str] | None = None
    for clean_brand, full_cell, content in rows:
        score = 0.0
        if clean_brand in variants:
            score = 3.0
        elif any(v in full_cell for v in variants):
            score = 2.0
        if score:
            score -= len(clean_brand) * 0.001  # prefer the most specific name
            if best is None or score > best[0]:
                best = (score, content)

    if best is None:
        return ""

    parts: list[str] = []
    if data["universal"]:
        parts.append(f"UNIVERSAL HOUSE RULES:\n{data['universal']}")
    parts.append(f"BRAND MASTER PROMPT & IDENTITY:\n{best[1][:_BRAND_CAP]}")
    return "\n\n".join(parts)


def brand_context_block(brand: Optional[dict[str, Any]], samples: list[dict[str, Any]]) -> str:
    """Human-readable brand context appended to the LLM brief."""
    lines: list[str] = []
    if brand:
        meta = brand.get("brand_metadata") or {}
        lines.append(f"Brand: {brand.get('brand_name')}")
        if meta.get("primary_colors"):
            lines.append(f"Primary colors: {', '.join(meta['primary_colors'])}")
        if meta.get("fonts"):
            lines.append(f"Typography: {', '.join(meta['fonts'])}")
        if meta.get("tone_of_voice"):
            lines.append(f"Tone of voice: {meta['tone_of_voice']}")
    if samples:
        names = ", ".join(s.get("file_name", "") for s in samples[:8])
        lines.append(f"Reference samples for style: {names}")
    return "\n".join(lines)


def fallback_master_prompt(
    message: str, brand: Optional[dict[str, Any]], samples: list[dict[str, Any]]
) -> str:
    """Deterministic Master Prompt used if the LLM call fails."""
    context = brand_context_block(brand, samples)
    parts = [
        "Create a high-quality, production-ready marketing image.",
        f"Brief: {message.strip()}",
    ]
    if context:
        parts.append(f"Brand kit:\n{context}")
    parts.append(
        "Keep strict brand consistency in color, typography, and tone; clean, "
        "modern composition suitable for marketing."
    )
    return "\n\n".join(parts)


def with_logo_instruction(master_prompt: str) -> str:
    """Variation A (no reference available) — best-effort logo."""
    return (
        f"{master_prompt}\n\nLOGO: Integrate the brand's logo naturally and "
        "prominently in an appropriate location."
    )


def with_reference_logo_instruction(master_prompt: str) -> str:
    """Variation A (reference logo provided) — composite the EXACT logo."""
    return (
        f"{master_prompt}\n\nLOGO: The attached reference image is the brand's "
        "OFFICIAL logo. Use it EXACTLY as provided — reproduce the same shapes, "
        "colors, wordmark, and proportions. Do NOT redraw, restyle, recolor, "
        "translate, or add/remove any text in the logo. Place it cleanly in the "
        "top-left following the brand layout, at a tasteful size with clear space "
        "around it."
    )


def with_placeholder_instruction(master_prompt: str) -> str:
    """Variation B — blank placeholder for manual logo placement in Canva."""
    return (
        f"{master_prompt}\n\nLOGO: Do NOT draw any logo, brand mark, or wordmark. "
        "Leave a clean, empty rectangular placeholder area (subtle outline on a "
        "flat neutral fill) where the logo should later be placed. Keep the rest "
        "of the composition identical to the branded version."
    )
