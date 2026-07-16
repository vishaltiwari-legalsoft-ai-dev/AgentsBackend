"""Analysis brain — turns structured numbers into readable narrative.

Online: calls the shared OpenRouter LLM with a prompt from ``prompts/``.
Offline (``MR_OFFLINE=1``) or on any failure: a deterministic template summary,
so the agent and its tests run with no network. ``narrate`` never raises.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_PROMPTS = Path(__file__).resolve().parent / "prompts"


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


def llm_json(prompt: str):
    """Call the LLM and parse a JSON object/array from the reply. Returns None
    offline or on any failure (callers must provide a deterministic fallback)."""
    if is_offline():
        return None
    try:
        import re

        from app.services.openrouter import get_llm

        resp = get_llm(temperature=0.1).invoke(prompt)
        text = getattr(resp, "content", str(resp))
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        return json.loads(m.group(1)) if m else None
    except Exception:
        return None


def llm_text(prompt: str) -> str | None:
    """Call the LLM for a free-text reply. Returns None offline / on failure."""
    if is_offline():
        return None
    try:
        from app.services.openrouter import get_llm

        resp = get_llm(temperature=0.3).invoke(prompt)
        return getattr(resp, "content", str(resp)) or None
    except Exception:
        return None


def narrate(kind: str, data: dict) -> str:
    if is_offline():
        return _offline_summary(kind, data)
    try:
        from app.services.openrouter import get_llm

        prompt = load_prompt(kind).replace("{data}", json.dumps(data, default=str))
        llm = get_llm(temperature=0.3)
        resp = llm.invoke(prompt)
        return getattr(resp, "content", str(resp))
    except Exception:
        return _offline_summary(kind, data)
