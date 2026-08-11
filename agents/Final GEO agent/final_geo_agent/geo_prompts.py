"""GEO agent — per-brand prompt universe.

A prompt universe is the fixed panel of natural-language buyer questions we
poll AI engines with (polling, not rank-tracking). Generated once per brand by
the LLM from the brand's name/domain/seeds, then human-edited; stored at
``geo-prompts-{brand_id}`` in the shared a2 state seam.
"""
from __future__ import annotations

import datetime as dt
import uuid

from seo_geo_agent import state
from seo_geo_agent.sources import llm_json

GEO_AGENT_ID = "a10"
DEFAULT_UNIVERSE_SIZE = 40

INTENTS = ("brand", "category", "problem")
STAGES = ("awareness", "consideration", "purchase")

_SYSTEM = (
    "You design prompt panels for measuring a brand's visibility in AI answer "
    "engines (ChatGPT, Perplexity, Gemini). Write the exact questions real "
    "buyers would type in a chat, in natural language. Cover three intents: "
    "'brand' (asks about the brand by name), 'category' (asks for the best "
    "providers/tools in the brand's category WITHOUT naming the brand), and "
    "'problem' (describes the pain the brand solves, no category words). Most "
    "prompts must NOT name the brand — that's the point of measurement. Return "
    "STRICT JSON: {\"prompts\": [{\"text\": str, \"intent\": "
    "\"brand\"|\"category\"|\"problem\", \"stage\": "
    "\"awareness\"|\"consideration\"|\"purchase\"}]}"
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def prompts_doc_id(brand_id: str) -> str:
    return f"geo-prompts-{brand_id}"


def load_universe(brand_id: str) -> dict | None:
    return state.load(prompts_doc_id(brand_id))


def save_universe(brand_id: str, prompts: list[dict]) -> dict:
    doc = {"brand_id": brand_id, "prompts": prompts, "updated_at": _now()}
    state.save(prompts_doc_id(brand_id), doc)
    return doc


def _clean(raw: list[dict], n: int, taken: set[str] | None = None) -> list[dict]:
    prompts: list[dict] = []
    seen: set[str] = set(taken or ())
    for item in raw:
        text = str(item.get("text", "")).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        intent = item.get("intent") if item.get("intent") in INTENTS else "category"
        stage = item.get("stage") if item.get("stage") in STAGES else "consideration"
        prompts.append(
            {
                "id": uuid.uuid4().hex[:8],
                "text": text,
                "intent": intent,
                "stage": stage,
                "enabled": True,
                "source": "ai",
            }
        )
        if len(prompts) >= n:
            break
    return prompts


def generate_universe(brand: dict, n: int = DEFAULT_UNIVERSE_SIZE) -> dict:
    """LLM-draft a prompt universe for a brand and persist it.

    Raises ``CredentialMissing`` (from ``llm_json``) offline/keyless — the
    router surfaces that honestly instead of inventing prompts.
    """
    seeds = ", ".join(brand.get("seeds") or []) or "unknown"
    prompt = (
        f"Brand: {brand.get('name')} ({brand.get('domain')})\n"
        f"Category seeds: {seeds}\n\n"
        f"Write {n} distinct buyer prompts. Distribution: ~15% brand intent, "
        f"~45% category intent, ~40% problem intent, spread across the three "
        f"funnel stages. Keep each under 25 words, conversational, specific "
        f"to this category (not generic marketing questions)."
    )
    data = llm_json(_SYSTEM, prompt, agent_id=GEO_AGENT_ID)
    # the team's own questions are the most valuable panel members — a
    # regenerate must NEVER wipe them, only refresh the AI-drafted ones
    existing = load_universe(brand["id"]) or {}
    custom = [p for p in existing.get("prompts", []) if p.get("source") == "custom"]
    prompts = _clean(
        list(data.get("prompts") or []), n,
        taken={p["text"].lower() for p in custom},
    )
    if not prompts:
        raise ValueError("LLM returned no usable prompts — try again")
    return save_universe(brand["id"], custom + prompts)


def add_custom_prompt(
    brand_id: str, text: str, intent: str = "category", stage: str = "consideration"
) -> dict:
    """Append one team-written question. Duplicates rejected honestly."""
    text = text.strip()
    if not (5 <= len(text) <= 400):
        raise ValueError("Prompt must be 5-400 characters")
    doc = load_universe(brand_id) or {"brand_id": brand_id, "prompts": []}
    if any(p.get("text", "").lower() == text.lower() for p in doc.get("prompts", [])):
        raise ValueError("That question is already in the universe")
    doc.setdefault("prompts", []).append({
        "id": uuid.uuid4().hex[:8],
        "text": text,
        "intent": intent if intent in INTENTS else "category",
        "stage": stage if stage in STAGES else "consideration",
        "enabled": True,
        "source": "custom",
    })
    return save_universe(brand_id, doc["prompts"])


def enabled_prompts(brand_id: str) -> list[dict]:
    doc = load_universe(brand_id)
    if not doc:
        return []
    return [p for p in doc.get("prompts", []) if p.get("enabled", True)]
