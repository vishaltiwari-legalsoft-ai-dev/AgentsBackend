"""GEO agent — per-brand prompt universe.

A prompt universe is the fixed panel of natural-language buyer questions we
poll AI engines with (polling, not rank-tracking). Generated once per brand by
the LLM from the brand's name/domain/seeds, then human-edited; stored at
``geo-prompts-{brand_id}`` in the shared a2 state seam.

Document shape::

    {"brand_id": str,
     "prompts":  [{"id", "text", "intent", "stage", "enabled", "source", "persona"}],
     "personas": [{"key", "label", "description"}],
     "updated_at": iso}

``persona`` on a prompt is a key from ``personas`` or ``""`` (unassigned); a key
the document does not know is coerced to ``""`` on every write, so a deleted
persona never leaves dangling tags behind.

Every write goes through :func:`geo_store.mutate`. The universe is edited from
the console, appended to by bulk paste and replaced by regeneration, and any
two of those can overlap — a plain load→append→save loses whichever landed
first.

Size is capped at :data:`MAX_UNIVERSE` prompts, enabled or not: sweep cost is
prompts × engines × runs, and a day-doc of answers hard-trims at 900 KB.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid

from seo_geo_agent import state
from seo_geo_agent.sources import llm_json

from final_geo_agent import geo_store

GEO_AGENT_ID = "a10"
DEFAULT_UNIVERSE_SIZE = 40
MAX_UNIVERSE = 250

INTENTS = ("brand", "category", "problem")
STAGES = ("awareness", "consideration", "purchase")

PROMPT_MIN_CHARS, PROMPT_MAX_CHARS = 5, 400

MAX_PERSONAS = 8
PERSONA_LABEL_MIN, PERSONA_LABEL_MAX = 2, 60
PERSONA_DESCRIPTION_MAX = 240
PERSONA_KEY_MAX = 24

# Skip reasons are part of the intake contract: the console shows them verbatim.
REASON_TOO_SHORT = "too short"
REASON_TOO_LONG = "too long"
REASON_DUPLICATE_BATCH = "duplicate in your list"
REASON_DUPLICATE_UNIVERSE = "already in the universe"
REASON_FULL = f"universe is full ({MAX_UNIVERSE})"

_SYSTEM = (
    "You design prompt panels for measuring a brand's visibility in AI answer "
    "engines (ChatGPT, Perplexity, Gemini). Write the exact questions real "
    "buyers would type in a chat, in natural language. Cover three intents: "
    "'brand' (asks about the brand by name), 'category' (asks for the best "
    "providers/tools in the brand's category WITHOUT naming the brand), and "
    "'problem' (describes the pain the brand solves, no category words). Most "
    "prompts must NOT name the brand — that's the point of measurement. "
)
_PERSONA_SYSTEM = (
    "The brand has named buyer personas, listed in the request with a key. "
    "Write each question the way ONE of those personas would actually type it "
    "— their vocabulary, their situation, their level of expertise — and tag it "
    "with that persona's key. Spread the questions evenly across the personas. "
)
_SHAPE = (
    "Return STRICT JSON: {\"prompts\": [{\"text\": str, \"intent\": "
    "\"brand\"|\"category\"|\"problem\", \"stage\": "
    "\"awareness\"|\"consideration\"|\"purchase\"%s}]}"
)

# One leading list marker: "- ", "* ", "• ", "1. ", "1) ", "(1) " and the
# dash/bullet glyphs word processors substitute for them. Numbering needs the
# trailing space so "1.5 million calls" keeps its number.
_LIST_MARKER = re.compile(r"^(?:[-*•‣◦▪–—]+\s*|\(?\d{1,3}[.)]\s+)")
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"), ("`", "`"))
_SLUG_JUNK = re.compile(r"[^a-z0-9]+")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def prompts_doc_id(brand_id: str) -> str:
    return f"geo-prompts-{brand_id}"


def load_universe(brand_id: str) -> dict | None:
    doc = state.load(prompts_doc_id(brand_id))
    if doc is not None:
        doc.setdefault("personas", [])
    return doc


def enabled_prompts(brand_id: str) -> list[dict]:
    doc = load_universe(brand_id)
    if not doc:
        return []
    return [p for p in doc.get("prompts", []) if p.get("enabled", True)]


# ---------------------------------------------------------------- personas ----


def _slug(text: str) -> str:
    return _SLUG_JUNK.sub("-", text.lower()).strip("-")[:PERSONA_KEY_MAX].strip("-")


def _validate_personas(raw: list[dict]) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("personas must be a list")
    if len(raw) > MAX_PERSONAS:
        raise ValueError(f"At most {MAX_PERSONAS} personas")
    personas: list[dict] = []
    keys: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each persona must be an object")
        label = " ".join(str(item.get("label") or "").split())
        if not (PERSONA_LABEL_MIN <= len(label) <= PERSONA_LABEL_MAX):
            raise ValueError(
                f"Persona label must be {PERSONA_LABEL_MIN}-{PERSONA_LABEL_MAX} characters"
            )
        description = " ".join(str(item.get("description") or "").split())
        if len(description) > PERSONA_DESCRIPTION_MAX:
            raise ValueError(
                f"Persona description must be at most {PERSONA_DESCRIPTION_MAX} characters"
            )
        key = _slug(str(item.get("key") or "")) or _slug(label)
        if not key:
            raise ValueError(f"Cannot derive a key for persona {label!r}")
        if key in keys:
            raise ValueError(f"Duplicate persona key {key!r}")
        keys.add(key)
        personas.append({"key": key, "label": label, "description": description})
    return personas


def _persona_keys(doc: dict) -> set[str]:
    return {p.get("key", "") for p in doc.get("personas") or [] if p.get("key")}


def _tag(prompt: dict, keys: set[str]) -> dict:
    record = dict(prompt)
    persona = record.get("persona") or ""
    record["persona"] = persona if persona in keys else ""
    return record


def set_personas(brand_id: str, personas: list[dict]) -> dict:
    """Replace the brand's personas; prompts tagged with a dropped key are
    untagged in the same transaction. Raises ``ValueError`` on invalid input."""
    clean = _validate_personas(personas)
    keys = {p["key"] for p in clean}
    stamp = _now()

    def change(current: dict) -> tuple[dict, dict]:
        doc = {
            **current,
            "brand_id": brand_id,
            "prompts": [_tag(p, keys) for p in current.get("prompts") or []],
            "personas": clean,
            "updated_at": stamp,
        }
        return doc, doc

    return geo_store.mutate(prompts_doc_id(brand_id), change)


# -------------------------------------------------------------------- write ----


def save_universe(brand_id: str, prompts: list[dict]) -> dict:
    """Replace the prompt list; personas on the document are preserved.

    A prompt record without a ``persona`` key keeps the persona the stored
    record of the same id carries — an editor that predates personas must not
    be able to untag the whole universe by round-tripping it.
    """
    if len(prompts) > MAX_UNIVERSE:
        raise ValueError(REASON_FULL)
    stamp = _now()

    def change(current: dict) -> tuple[dict, dict]:
        keys = _persona_keys(current)
        stored = {p.get("id"): p for p in current.get("prompts") or []}
        merged = []
        for prompt in prompts:
            record = dict(prompt)
            if "persona" not in record:
                record["persona"] = (stored.get(record.get("id")) or {}).get("persona", "")
            merged.append(_tag(record, keys))
        doc = {
            **current,
            "brand_id": brand_id,
            "prompts": merged,
            "personas": current.get("personas") or [],
            "updated_at": stamp,
        }
        return doc, doc

    return geo_store.mutate(prompts_doc_id(brand_id), change)


# ------------------------------------------------------------------- intake ----


def _strip_line(line: str) -> str:
    text = line.strip()
    while True:
        stripped = _LIST_MARKER.sub("", text, count=1).strip()
        for open_q, close_q in _QUOTE_PAIRS:
            if len(stripped) >= 2 and stripped[0] == open_q and stripped[-1] == close_q:
                stripped = stripped[1:-1].strip()
                break
        if stripped == text:
            return " ".join(text.split())
        text = stripped


def parse_prompt_lines(raw: str | list[str]) -> list[str]:
    """Pasted text → candidate prompts, one per non-blank line, in order.

    Leading list markers and surrounding quotes are stripped, whitespace is
    collapsed. No validation here — that is :func:`add_prompts`' job, which
    reports per line rather than rejecting the paste.
    """
    chunks = raw if isinstance(raw, list) else [raw]
    lines: list[str] = []
    for chunk in chunks:
        for line in str(chunk or "").splitlines():
            text = _strip_line(line)
            if text:
                lines.append(text)
    return lines


def add_prompts(
    brand_id: str,
    raw: str | list[str],
    *,
    persona: str = "",
    intent: str = "category",
    stage: str = "consideration",
) -> dict:
    """Bulk intake: parse, validate per line, append in ONE transaction.

    Returns ``{"added": [records], "skipped": [{"text", "reason"}],
    "total": int, "universe": doc}``. A line is skipped, never the batch: the
    caller sees exactly which lines landed and why the others did not.
    """
    candidates = parse_prompt_lines(raw)
    intent = intent if intent in INTENTS else "category"
    stage = stage if stage in STAGES else "consideration"
    # ids and the stamp are fixed up front so ``change`` is pure over the doc
    # and a retried transaction commits the same records
    ids = [uuid.uuid4().hex[:8] for _ in candidates]
    stamp = _now()

    if not candidates:
        doc = load_universe(brand_id) or {"brand_id": brand_id, "prompts": [], "personas": []}
        return {"added": [], "skipped": [], "total": len(doc["prompts"]), "universe": doc}

    def change(current: dict) -> tuple[dict, dict]:
        existing = list(current.get("prompts") or [])
        keys = _persona_keys(current)
        tag = persona if persona in keys else ""
        taken = {str(p.get("text", "")).lower() for p in existing}
        batch: set[str] = set()
        added: list[dict] = []
        skipped: list[dict] = []
        for text, pid in zip(candidates, ids):
            if len(text) < PROMPT_MIN_CHARS:
                skipped.append({"text": text, "reason": REASON_TOO_SHORT})
                continue
            if len(text) > PROMPT_MAX_CHARS:
                skipped.append({"text": text, "reason": REASON_TOO_LONG})
                continue
            low = text.lower()
            if low in batch:
                skipped.append({"text": text, "reason": REASON_DUPLICATE_BATCH})
                continue
            batch.add(low)
            if low in taken:
                skipped.append({"text": text, "reason": REASON_DUPLICATE_UNIVERSE})
                continue
            if len(existing) + len(added) >= MAX_UNIVERSE:
                skipped.append({"text": text, "reason": REASON_FULL})
                continue
            added.append({
                "id": pid,
                "text": text,
                "intent": intent,
                "stage": stage,
                "enabled": True,
                "source": "custom",
                "persona": tag,
            })
        doc = {
            **current,
            "brand_id": brand_id,
            "prompts": existing + added,
            "personas": current.get("personas") or [],
            "updated_at": stamp if added else current.get("updated_at") or stamp,
        }
        result = {
            "added": added,
            "skipped": skipped,
            "total": len(doc["prompts"]),
            "universe": doc,
        }
        return doc, result

    return geo_store.mutate(prompts_doc_id(brand_id), change)


def add_custom_prompt(
    brand_id: str, text: str, intent: str = "category", stage: str = "consideration"
) -> dict:
    """Single-prompt intake for the existing endpoint: the universe on success,
    ``ValueError`` carrying the skip reason when nothing landed."""
    result = add_prompts(brand_id, text, intent=intent, stage=stage)
    if not result["added"]:
        reason = result["skipped"][0]["reason"] if result["skipped"] else "empty"
        raise ValueError(f"Prompt not added: {reason}")
    return result["universe"]


# ----------------------------------------------------------------- generate ----


def _clean(
    raw: list[dict], n: int, taken: set[str] | None = None,
    persona_keys: set[str] | None = None,
) -> list[dict]:
    prompts: list[dict] = []
    seen: set[str] = set(taken or ())
    keys = persona_keys or set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        intent = item.get("intent") if item.get("intent") in INTENTS else "category"
        stage = item.get("stage") if item.get("stage") in STAGES else "consideration"
        persona = item.get("persona") if item.get("persona") in keys else ""
        prompts.append(
            {
                "id": uuid.uuid4().hex[:8],
                "text": text,
                "intent": intent,
                "stage": stage,
                "enabled": True,
                "source": "ai",
                "persona": persona,
            }
        )
        if len(prompts) >= n:
            break
    return prompts


def _system_prompt(personas: list[dict]) -> str:
    if not personas:
        return _SYSTEM + _SHAPE % ""
    return _SYSTEM + _PERSONA_SYSTEM + _SHAPE % ", \"persona\": <one of the given keys>"


def _persona_block(personas: list[dict]) -> str:
    if not personas:
        return ""
    lines = [
        f"- key={p['key']}: {p['label']}" + (f" — {p['description']}" if p.get("description") else "")
        for p in personas
    ]
    return (
        "\nBuyer personas (write each question as one of them would type it, "
        "tag it with the key, spread evenly):\n" + "\n".join(lines) + "\n"
    )


def generate_universe(brand: dict, n: int = DEFAULT_UNIVERSE_SIZE) -> dict:
    """LLM-draft a prompt universe for a brand and persist it.

    Raises ``CredentialMissing`` (from ``llm_json``) offline/keyless — the
    router surfaces that honestly instead of inventing prompts.

    The team's own questions are the most valuable panel members: a regenerate
    refreshes only the AI-drafted ones, and the custom set it keeps is the one
    in the document AT COMMIT TIME, not the one read before the model call.
    """
    existing = load_universe(brand["id"]) or {}
    personas = existing.get("personas") or []
    custom_count = sum(1 for p in existing.get("prompts") or [] if p.get("source") == "custom")
    if custom_count >= MAX_UNIVERSE:
        raise ValueError(f"{REASON_FULL}: remove some custom prompts before regenerating")

    seeds = ", ".join(brand.get("seeds") or []) or "unknown"
    prompt = (
        f"Brand: {brand.get('name')} ({brand.get('domain')})\n"
        f"Category seeds: {seeds}\n"
        f"{_persona_block(personas)}\n"
        f"Write {n} distinct buyer prompts. Distribution: ~15% brand intent, "
        f"~45% category intent, ~40% problem intent, spread across the three "
        f"funnel stages. Keep each under 25 words, conversational, specific "
        f"to this category (not generic marketing questions)."
    )
    data = llm_json(_system_prompt(personas), prompt, agent_id=GEO_AGENT_ID)
    raw = list(data.get("prompts") or [])
    stamp = _now()

    def change(current: dict) -> tuple[dict, dict]:
        custom = [p for p in current.get("prompts") or [] if p.get("source") == "custom"]
        drafted = _clean(
            raw, min(n, MAX_UNIVERSE - len(custom)),
            taken={str(p.get("text", "")).lower() for p in custom},
            persona_keys=_persona_keys(current),
        )
        if not drafted:
            raise ValueError("LLM returned no usable prompts — try again")
        doc = {
            **current,
            "brand_id": brand["id"],
            "prompts": custom + drafted,
            "personas": current.get("personas") or [],
            "updated_at": stamp,
        }
        return doc, doc

    return geo_store.mutate(prompts_doc_id(brand["id"]), change)
