"""Auto-mode planner: one LLM call that plans the whole creative from the brief.

The plan references ONLY real pack inventory (Stage-1/2 variant ids, Stage-4
logo-library ids). Malformed output or unknown ids are retried with the
validation errors echoed back (max 2 retries); total failure raises PlanError —
an honest error, never a fabricated plan.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("graphics_designer.planner")

PLAN_VERSION = 1
_MAX_ATTEMPTS = 3  # 1 try + 2 retries-with-errors


class PlanError(Exception):
    """Planning failed for a user-visible reason (router maps it to HTTP 502)."""


def _get_planner_llm():
    """Module-level seam tests monkeypatch (mirrors suggestions._get_llm).
    Resolves the planner model through runtime_config so the admin panel's
    global/per-agent overrides apply with no redeploy."""
    from app.services import runtime_config
    from app.services.openrouter import get_llm

    return get_llm(temperature=0.4, model=runtime_config.get_for_agent("a1", "gd_planner_model"))


def _inventory(pack, logo_ids: list[str]) -> str:
    s1 = "\n".join(f'- {v["id"]}: {v["title"]} — {v["desc"]}' for v in pack.stage1_variants)
    s2 = "\n".join(f'- {v["id"]}: {v["title"]} — {v["desc"]}' for v in pack.stage2_variants)
    logos = ", ".join(logo_ids) if logo_ids else "(no logo library — logo_id must be null)"
    return (f"STAGE-1 BACKGROUNDS (pick one id):\n{s1}\n\n"
            f"STAGE-2 SUBJECTS (pick one id):\n{s2}\n\n"
            f"LOGO VARIANT IDS: {logos}")


def _plan_ask(pack, brief: str, logo_ids: list[str]) -> str:
    return (
        f"You are the creative director for {pack.name}. Plan ONE social creative "
        "from the client brief using ONLY the inventory below.\n\n"
        f'CLIENT BRIEF (the #1 hard rule — the plan must visibly serve it): "{brief}"\n\n'
        f"{_inventory(pack, logo_ids)}\n\n"
        f"Brand kit (reference):\n{pack.brand_kit_block}\n\n"
        "Write the words too: headline ≤ 8 words, one highlight word taken from "
        "the headline, subline ≤ 14 words, cta ≤ 4 words. Every reason is ONE "
        "short sentence tied to the brief.\n\n"
        'Return ONLY minified JSON: {"concept":"one sentence",'
        '"gradient":{"cid":"...","reason":"..."},'
        '"element":{"cid":"...","reason":"..."},'
        '"text":{"headline":"...","highlight":"...","subline":"...","cta":"...","reason":"..."},'
        '"logo":{"logo_id":"..." or null,"reason":"..."}}'
    )


def _validate(cand: dict, pack, logo_ids: list[str]) -> list[str]:
    errors: list[str] = []
    if not isinstance(cand, dict):
        return ["The reply must be a JSON object."]
    s1 = {v["id"] for v in pack.stage1_variants}
    s2 = {v["id"] for v in pack.stage2_variants}
    g = str((cand.get("gradient") or {}).get("cid") or "").upper()
    e = str((cand.get("element") or {}).get("cid") or "").upper()
    if g not in s1:
        errors.append(f"gradient.cid '{g}' is not one of the stage-1 ids.")
    if e not in s2:
        errors.append(f"element.cid '{e}' is not one of the stage-2 ids.")
    text = cand.get("text") or {}
    if not str(text.get("headline") or "").strip():
        errors.append("text.headline is required.")
    if not str(text.get("cta") or "").strip():
        errors.append("text.cta is required.")
    if len(str(text.get("headline") or "")) > 80:
        errors.append("text.headline must be ≤ 80 characters.")
    if len(str(text.get("subline") or "")) > 120:
        errors.append("text.subline must be ≤ 120 characters.")
    if len(str(text.get("cta") or "")) > 40:
        errors.append("text.cta must be ≤ 40 characters.")
    lid = (cand.get("logo") or {}).get("logo_id")
    if logo_ids:
        if lid is not None and lid not in logo_ids:
            errors.append(f"logo.logo_id '{lid}' is not one of the logo ids.")
    elif lid is not None:
        errors.append("logo.logo_id must be null — this brand has no logo library.")
    return errors


def build_plan(run: dict, pack, brief: str, logo_ids: list[str]) -> dict:
    brief = (brief or "").strip()
    if not brief:
        raise PlanError("Auto mode needs a brief — tell the studio what this creative is about.")
    ask = _plan_ask(pack, brief, logo_ids)
    try:
        llm = _get_planner_llm()
    except Exception as exc:  # noqa: BLE001 - missing key / app layer
        raise PlanError("The planner model is unavailable — check the OpenRouter configuration.") from exc
    errors: list[str] = []
    for attempt in range(_MAX_ATTEMPTS):
        q = ask if attempt == 0 else (
            ask + "\n\nYOUR PREVIOUS ANSWER WAS REJECTED: " + " ".join(errors)
            + " Fix every issue and return ONLY the corrected minified JSON."
        )
        try:
            msg = llm.invoke(q)
        except Exception as exc:  # noqa: BLE001
            raise PlanError("The planner call failed — try again in a moment.") from exc
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            errors = ["The reply was not the requested minified JSON object."]
            continue
        try:
            cand = json.loads(match.group(0))
        except Exception:  # noqa: BLE001
            errors = ["The reply was not valid JSON."]
            continue
        errors = _validate(cand, pack, logo_ids)
        if not errors:
            text = cand.get("text") or {}
            return {
                "version": PLAN_VERSION,
                "brief": brief,
                "concept": str(cand.get("concept") or "").strip()[:200],
                "gradient": {"cid": str(cand["gradient"]["cid"]).upper(),
                             "reason": str(cand["gradient"].get("reason") or "").strip()[:200]},
                "element": {"cid": str(cand["element"]["cid"]).upper(),
                            "reason": str(cand["element"].get("reason") or "").strip()[:200]},
                "text": {
                    "headline": str(text.get("headline") or "").strip(),
                    "highlight": str(text.get("highlight") or "").strip(),
                    "subline": str(text.get("subline") or "").strip(),
                    "cta": str(text.get("cta") or "").strip(),
                    "reason": str(text.get("reason") or "").strip()[:200],
                },
                "logo": {"logo_id": (cand.get("logo") or {}).get("logo_id"),
                         "reason": str((cand.get("logo") or {}).get("reason") or "").strip()[:200]},
            }
        logger.warning("plan attempt %d rejected: %s", attempt + 1, "; ".join(errors))
    raise PlanError("The planner's output kept failing validation: " + " ".join(errors))
