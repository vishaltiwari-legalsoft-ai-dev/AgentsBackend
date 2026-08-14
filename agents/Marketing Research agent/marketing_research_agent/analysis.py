"""Analysis brain — turns structured numbers into readable narrative.

Online: calls the shared OpenRouter LLM with a prompt from ``prompts/``.
Offline (``MR_OFFLINE=1``) or on any failure: a deterministic template summary,
so the agent and its tests run with no network. ``narrate`` never raises.

**The template summaries are legitimate — but they must never masquerade as
model output.** They flow into ``report["markdown"]``/``["html"]`` and download
as a client-facing PDF, so every degraded path here reports the honest pair
``ai=False`` + a non-empty ``fallback_reason`` naming the actual cause (offline
flag / missing credential / provider error / timeout / validation rejected).
The ``*_result`` functions are the ones that carry it; the older plain
``narrate``/``llm_json``/``llm_text`` wrappers stay for callers that only need
the value.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_PROMPTS = Path(__file__).resolve().parent / "prompts"

# Agent id in the platform catalog — routes the creator's per-agent model
# override (Agent Configuration panel) to every MR LLM call.
MR_AGENT_ID = "a6"


def load_prompt(name: str) -> str:
    p = _PROMPTS / f"{name}.txt"
    return p.read_text(encoding="utf-8") if p.exists() else "{data}"


def is_offline() -> bool:
    return os.environ.get("MR_OFFLINE") == "1"


def _fmt_money(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def _offline_summary(kind: str, data: dict) -> str:
    """Deterministic plain-language read used when the LLM is unavailable.

    Mirrors the prompts' house style — and the shape the report doc's <Prose>
    renders: a standalone verdict line, "- " bullet findings, then Recommend."""
    channels = data.get("channels") or {}
    issues = data.get("issues") or []
    totals = data.get("totals") or {}
    lead = ""
    bullets: list[str] = []
    recommend = ""

    if totals.get("spend") is not None:
        lead = (
            f"Total spend {_fmt_money(totals.get('spend'))} produced "
            f"{totals.get('demos_completed', 'n/a')} completed demos at "
            f"{_fmt_money(totals.get('cost_per_demo_completed'))} each."
        )

    # Cheapest vs priciest channel by cost per demo booked.
    ranked = sorted(
        ((ch, a.get("cost_per_demo_booked")) for ch, a in channels.items() if a.get("cost_per_demo_booked")),
        key=lambda t: t[1],
    )
    if len(ranked) >= 2:
        best, worst = ranked[0], ranked[-1]
        bullets.append(f"- {best[0]} is the most efficient channel at {_fmt_money(best[1])} per demo booked.")
        bullets.append(f"- {worst[0]} is the most expensive at {_fmt_money(worst[1])} per demo booked.")
    elif ranked:
        bullets.append(f"- {ranked[0][0]} is running at {_fmt_money(ranked[0][1])} per demo booked.")

    # One bullet per flagged vendor — the desk reads these individually.
    for v in (data.get("red_flag_vendors") or [])[:4]:
        bullets.append(f"- Red flag: {v.get('vendor')} — {(v.get('reasons') or ['flagged'])[0]}.")

    if issues:
        top = issues[0]
        bullets.append(f"- Top issue: {top.get('text', '')}.")
        recommend = (f"Recommend: address {top.get('count', '')} flagged campaigns, "
                     f"starting with the worst offenders.")
    else:
        # Non-campaign kinds.
        attribution = data.get("attribution")
        if attribution:
            bullets.append(f"- {attribution.get('pct')}% of leads are attributed to a source.")
        comps = data.get("competitors")
        if comps:
            changed = [c.get("competitor") for c in comps if c.get("changed")]
            bullets.append(
                f"- {len(changed)} competitor(s) changed this period: {', '.join(changed)}." if changed
                else "- No material competitor changes detected."
            )
        ranked_opps = data.get("ranked")
        if ranked_opps:
            bullets.append(f"- {len(ranked_opps)} qualified media opportunities surfaced.")

    if not lead:
        # No totals line — promote the first finding so the read still opens with
        # a standalone verdict rather than a bullet.
        lead = bullets.pop(0).lstrip("- ") if bullets else ""
    lines = [ln for ln in [lead, *bullets, recommend] if ln]
    return "\n".join(lines) or json.dumps(data, default=str)[:300]


# --- honest-failure reasons -------------------------------------------------

OFFLINE_REASON = (
    "MR_OFFLINE=1 — deterministic offline template, not model output"
)
EMPTY_REPLY_REASON = "the model returned an empty reply"
NO_JSON_REASON = "the model reply contained no parseable JSON"


def failure_reason(exc: BaseException) -> str:
    """Name the actual cause of an LLM failure for ``fallback_reason``.

    Classifies into the categories the UI distinguishes — missing credential,
    timeout, provider error — and always appends the concrete exception so the
    reason is diagnosable. Never includes a secret value: the exception text
    from ``runtime_config.require`` names the env var, not the key.
    """
    name = type(exc).__name__
    msg = (str(exc) or "").strip()
    low = f"{name} {msg}".lower()
    if "timeout" in low or "timed out" in low or "deadline" in low:
        kind = "the model call timed out"
    elif any(k in low for k in (
            "api key", "api_key", "apikey", "credential", "unauthorized",
            "unauthenticated", "401", "403", "not configured", "missing key")):
        kind = "the model provider credential is missing or was rejected"
    elif name in ("ImportError", "ModuleNotFoundError"):
        kind = "the model provider is unavailable in this runtime"
    elif "rate limit" in low or "429" in low:
        kind = "the model provider rate-limited the request"
    else:
        kind = "the model provider call failed"
    return f"{kind} ({name}: {msg})"[:300] if msg else f"{kind} ({name})"


# --- LLM calls (``*_result`` carry the honest pair) --------------------------

def llm_json_result(prompt: str) -> tuple[object | None, str | None]:
    """``(value, fallback_reason)``. ``fallback_reason`` is None only when the
    model genuinely produced parseable JSON."""
    if is_offline():
        return None, OFFLINE_REASON
    try:
        import re

        from app.services.openrouter import get_llm

        resp = get_llm(temperature=0.1, agent_id=MR_AGENT_ID).invoke(prompt)
        text = getattr(resp, "content", str(resp))
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if not m:
            return None, NO_JSON_REASON
        return json.loads(m.group(1)), None
    except Exception as exc:
        return None, failure_reason(exc)


def llm_text_result(prompt: str) -> tuple[str | None, str | None]:
    """``(text, fallback_reason)``. ``fallback_reason`` is None only when the
    model genuinely produced text."""
    if is_offline():
        return None, OFFLINE_REASON
    try:
        from app.services.openrouter import get_llm

        resp = get_llm(temperature=0.3, agent_id=MR_AGENT_ID).invoke(prompt)
        text = getattr(resp, "content", str(resp)) or None
        return (text, None) if text else (None, EMPTY_REPLY_REASON)
    except Exception as exc:
        return None, failure_reason(exc)


def llm_json(prompt: str):
    """Call the LLM and parse a JSON object/array from the reply. Returns None
    offline or on any failure (callers must provide a deterministic fallback).

    Value-only wrapper — prefer :func:`llm_json_result` on any path whose output
    reaches a user, so the degraded case can be labelled."""
    return llm_json_result(prompt)[0]


def llm_text(prompt: str) -> str | None:
    """Call the LLM for a free-text reply. Returns None offline / on failure.

    Value-only wrapper — prefer :func:`llm_text_result` on user-facing paths."""
    return llm_text_result(prompt)[0]


def narrate_result(kind: str, data: dict) -> dict:
    """Narrative plus its provenance: ``{"text", "ai", "fallback_reason"}``.

    ``ai`` is True only when the text is genuinely model-written. Whenever it is
    False the text is the deterministic ``_offline_summary`` template and
    ``fallback_reason`` names why — never one without the other."""
    if is_offline():
        return {"text": _offline_summary(kind, data), "ai": False,
                "fallback_reason": OFFLINE_REASON}
    try:
        from app.services.openrouter import get_llm

        prompt = load_prompt(kind).replace("{data}", json.dumps(data, default=str))
        llm = get_llm(temperature=0.3, agent_id=MR_AGENT_ID)
        resp = llm.invoke(prompt)
        text = getattr(resp, "content", str(resp))
    except Exception as exc:
        return {"text": _offline_summary(kind, data), "ai": False,
                "fallback_reason": failure_reason(exc)}
    if not (text or "").strip():
        return {"text": _offline_summary(kind, data), "ai": False,
                "fallback_reason": EMPTY_REPLY_REASON}
    return {"text": text, "ai": True, "fallback_reason": None}


def narrate(kind: str, data: dict) -> str:
    """Text-only wrapper — prefer :func:`narrate_result` on any path that
    reaches a report, an answer card or a PDF."""
    return narrate_result(kind, data)["text"]
