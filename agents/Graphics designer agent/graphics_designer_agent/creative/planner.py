"""Planning rail — turn a brief + brand precedent into a *reviewable* plan.

The spec asks the agent to plan full sequences before anything is generated:
frame count, content flow and visual hierarchy for carousels; cover + in-article
images for blogs; sections for brochures; slides for decks. That plan is returned
for human review *before* generation begins.

Design, mirroring ``suggestions.py``:
- LLM-backed when an OpenRouter key is configured (``app.services.openrouter``),
  with a **deterministic fallback** so the whole rail plans offline and in tests.
- Every plan is grounded on retrieved brand references (the ``grounding`` block
  from ``reference_library.summarize_for_prompt``) — the agent never plans from
  scratch when precedent exists.
- Each plan carries a ``rationale`` and a list of ``decisions`` (step + what +
  why) so the run's decision log can audit the creative reasoning.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from .types import PLAN_HINTS

logger = logging.getLogger("graphics_designer.creative.planner")

_STOP = {
    "the", "a", "an", "and", "or", "of", "for", "to", "with", "in", "on", "at",
    "our", "your", "we", "us", "is", "are", "be", "this", "that", "from", "make",
    "create", "design", "want", "need", "about", "into", "new",
}


def _keywords(brief: str, limit: int = 6) -> list[str]:
    words = re.split(r"[^a-z0-9]+", (brief or "").lower())
    out: list[str] = []
    for w in words:
        if len(w) > 2 and w not in _STOP and w not in out:
            out.append(w)
        if len(out) >= limit:
            break
    return out


def _topic(brief: str, brand_name: str) -> str:
    kws = _keywords(brief, 4)
    if kws:
        return " ".join(kws).title()
    return f"{brand_name} Update"


def _clamp_count(creative_type: str, count: Optional[int]) -> int:
    hint = PLAN_HINTS.get(creative_type, {})
    default = int(hint.get("default_count", 3))
    if count is None:
        return default
    lo, hi = int(hint.get("min", 1)), int(hint.get("max", 12))
    return max(lo, min(hi, int(count)))


# --------------------------------------------------------------------------- #
# Deterministic fallback plans (always available — no model required)
# --------------------------------------------------------------------------- #

# A rotation of on-brand professional subjects so every carousel slide gets a
# DISTINCT foreground (the brand foundation/palette is shared by Stage 1, which is
# what keeps the set cohesive — "shared style, different subjects"). Kept generic
# enough for any business brand; the LLM path proposes brand-specific subjects.
_SUBJECT_ROTATION = [
    "a confident professional wearing a headset, working at a modern desk, looking at the camera",
    "two colleagues collaborating over a laptop in a bright, modern office",
    "a focused professional reviewing documents at a clean desk",
    "a close-up handshake between two business professionals",
    "a smiling team member on a video call in a tidy workspace",
    "an executive presenting to a small team in a bright meeting room",
    "a professional working thoughtfully on a laptop near a window",
    "a friendly receptionist greeting a client at a modern front desk",
]


def _highlight(headline: str) -> str:
    """The key phrase to accent within a headline (designer hierarchy).

    Prefers a number/percentage (the strongest visual hook), else the last word —
    must be a substring of the headline so the text renderer can colour it."""
    m = re.search(r"\d+\s*%|\$?\d[\d,]*\+?", headline or "")
    if m:
        return m.group(0).strip()
    words = (headline or "").split()
    return words[-1] if words else ""


def _carousel_plan(brief: str, brand_name: str, count: int) -> dict[str, Any]:
    topic = _topic(brief, brand_name)
    kws = _keywords(brief, count) or ["benefit"]
    frames: list[dict[str, Any]] = []
    for i in range(count):
        if i == 0:
            role, headline, body = "hook", topic, f"Why {brand_name} — swipe to see."
        elif i == count - 1:
            role, headline, body = "cta", "Get started", f"Talk to {brand_name} today."
        else:
            kw = kws[(i - 1) % len(kws)].title()
            role, headline, body = "body", kw, f"How {brand_name} delivers {kw.lower()}."
        frames.append({
            "index": i + 1, "role": role, "headline": headline, "body": body,
            "highlight": _highlight(headline),
            # Distinct foreground per slide → a real carousel, not one photo ×N.
            "subject": _SUBJECT_ROTATION[i % len(_SUBJECT_ROTATION)],
            "visual": "brand gradient, single focal subject, generous negative space for text",
        })
    return {"frames": frames}


def _presentation_plan(brief: str, brand_name: str, count: int) -> dict[str, Any]:
    topic = _topic(brief, brand_name)
    kws = _keywords(brief, count) or ["overview", "approach", "results"]
    slides: list[dict[str, Any]] = []
    for i in range(count):
        if i == 0:
            slides.append({"index": 1, "title": topic,
                           "bullets": [f"Presented by {brand_name}"],
                           "notes": f"Title slide for {topic}."})
        elif i == count - 1:
            slides.append({"index": i + 1, "title": "Next steps",
                           "bullets": ["Summary of the proposal", f"Contact {brand_name}", "Q&A"],
                           "notes": "Closing slide — restate the ask."})
        else:
            kw = kws[(i - 1) % len(kws)].title()
            slides.append({"index": i + 1, "title": kw,
                           "bullets": [f"Key point on {kw.lower()}", "Supporting detail",
                                       "Proof / example"],
                           "notes": f"Speak to {kw.lower()} for ~1 minute."})
    return {"slides": slides}


def _brochure_plan(brief: str, brand_name: str, count: int) -> dict[str, Any]:
    topic = _topic(brief, brand_name)
    kws = _keywords(brief, max(3, count)) or ["services", "approach", "support"]
    # A real brochure: a roles/feature card grid, a 3-step "how it works", a
    # testimonial, and a contact CTA — not N identical text sections.
    cards = [{
        "title": kw.title()[:24],
        "bullets": [f"{kw.title()} benefit one", f"{kw.title()} benefit two"],
        "initials": "".join(w[0] for w in kw.split()[:2]).upper() or kw[:2].upper(),
    } for kw in kws[:6]]
    pages = [
        {"template": "card_grid", "heading": "What We Offer",
         "highlight": "Offer", "cards": cards},
        {"template": "steps", "heading": "How It Works",
         "steps": [{"title": "Tell us your needs", "desc": f"Share your goals with {brand_name}."},
                   {"title": "We match you", "desc": "Get the right fit, fast."},
                   {"title": "Start delegating", "desc": "Boost output from day one."}]},
        {"template": "testimonial",
         "quote": f"{brand_name} delivered exactly what we needed — highly recommended.",
         "author": "A Happy Client"},
        {"template": "cta_contact", "heading": "Ready To Start?",
         "contact": {"website": f"{brand_name.lower().replace(' ', '')}.com"}},
    ]
    return {
        "cover": {"title": topic, "highlight": _highlight(topic),
                  "subtitle": f"A {brand_name} brochure"},
        "pages": pages,
    }


def _blog_plan(brief: str, brand_name: str, count: int) -> dict[str, Any]:
    topic = _topic(brief, brand_name)
    kws = _keywords(brief, max(1, count - 1)) or ["insight"]
    inline = [{
        "caption": kws[i % len(kws)].title(),
        "visual": f"supporting visual illustrating {kws[i % len(kws)]}",
    } for i in range(max(0, count - 1))]
    return {
        "cover": {"title": topic, "subtitle": f"{brand_name} blog",
                  "visual": "wide hero with the article title leading"},
        "inline": inline,
    }


_BUILDERS = {
    "carousel": _carousel_plan,
    "presentation": _presentation_plan,
    "brochure": _brochure_plan,
    "blog": _blog_plan,
}


def _deterministic_plan(creative_type: str, brief: str, brand_name: str, count: int) -> dict[str, Any]:
    builder = _BUILDERS.get(creative_type)
    if not builder:
        raise ValueError(f"No planner for creative type: {creative_type}")
    return builder(brief, brand_name, count)


# --------------------------------------------------------------------------- #
# Optional LLM enrichment (lazy — never required)
# --------------------------------------------------------------------------- #

def _llm_plan(
    creative_type: str, brief: str, brand_name: str, count: int, grounding: str,
) -> Optional[dict[str, Any]]:
    try:
        from app.services.openrouter import get_llm  # lazy — package works without the app
    except Exception:
        return None

    hint = PLAN_HINTS.get(creative_type, {})
    shape = {
        "carousel": '{"frames":[{"index":1,"role":"hook|body|cta","headline":"...","highlight":"<key phrase that appears verbatim in headline>","body":"...","subject":"<a DISTINCT on-brand foreground subject for THIS slide, different from every other slide, with generous negative space on one side for text>","visual":"..."}]}',
        "presentation": '{"slides":[{"index":1,"title":"...","bullets":["..."],"notes":"..."}]}',
        "brochure": '{"cover":{"title":"...","highlight":"<key phrase verbatim in title>","subtitle":"..."},"pages":[{"template":"card_grid","heading":"...","highlight":"<verbatim>","cards":[{"title":"...","bullets":["...","..."],"initials":"AB"}]},{"template":"steps","heading":"...","steps":[{"title":"...","desc":"..."}]},{"template":"testimonial","quote":"...","author":"..."},{"template":"cta_contact","heading":"...","contact":{"phone":"...","email":"...","website":"..."}}]}',
        "blog": '{"cover":{"title":"...","subtitle":"...","visual":"..."},"inline":[{"caption":"...","visual":"..."}]}',
    }[creative_type]
    carousel_note = (
        "Every slide must have its OWN distinct foreground subject (different scene "
        "per slide) — never reuse one image across slides. All slides share the brand "
        "palette/gradient/logo for cohesion. For each headline, set 'highlight' to the "
        "single most important phrase (it must appear verbatim in the headline).\n\n"
        if creative_type == "carousel" else ""
    )
    prompt = (
        f"You are a senior creative director planning a {creative_type} for the brand "
        f"{brand_name}. Brief: {brief or '(none given)'}.\n\n"
        f"Plan {count} {hint.get('unit', 'unit')}(s). Decide content flow and visual "
        f"hierarchy. Stay strictly on-brand.\n\n"
        f"{carousel_note}"
        f"Brand precedent to take reference from (match its style/layout):\n{grounding}\n\n"
        f"Return STRICT JSON matching exactly this shape: {shape}. No prose."
    )
    try:
        msg = get_llm(temperature=0.7, fast=False).invoke(prompt)
        content = getattr(msg, "content", "") or ""
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) and data else None
    except Exception as exc:  # noqa: BLE001 - enrichment must never break planning
        logger.info("LLM planning failed (%s); using deterministic plan", exc)
        return None


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def plan(
    creative_type: str,
    brief: str,
    *,
    brand_name: str,
    grounding: str = "",
    count: Optional[int] = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Build a reviewable plan for one creative.

    Returns ``{creative_type, count, source, rationale, decisions, <body>}`` where
    ``<body>`` is the type-specific structure (``frames`` / ``slides`` /
    ``sections`` / ``cover`` + ``inline``). ``source`` is ``"agent+llm"`` when the
    model produced the plan, else ``"deterministic"``.
    """
    n = _clamp_count(creative_type, count)
    body = _deterministic_plan(creative_type, brief, brand_name, n)
    source = "deterministic"

    if use_llm:
        enriched = _llm_plan(creative_type, brief, brand_name, n, grounding)
        if enriched:
            # Trust the model's structure but keep a sane floor: only adopt it if it
            # carries the expected top-level body key.
            expected_key = "pages" if creative_type == "brochure" else (
                "cover" if creative_type == "blog" else
                "slides" if creative_type == "presentation" else "frames")
            if expected_key in enriched:
                body = enriched
                source = "agent+llm"

    unit = PLAN_HINTS.get(creative_type, {}).get("unit", "frame")
    grounded = bool(grounding and "No on-brand reference" not in grounding)
    rationale = (
        f"Planned a {n}-{unit} {creative_type} for '{brief or 'brand update'}'"
        + (", grounded in retrieved brand precedent." if grounded else
           ", no prior references found — used brand defaults.")
    )
    decisions = [
        {"step": "strategy", "source": "agent",
         "decision": f"{n} {unit}(s)",
         "rationale": f"Default flow for a {creative_type}; adjusted to the brief length."},
        {"step": "layout", "source": "agent",
         "decision": f"Plan via {source}",
         "rationale": rationale},
    ]
    return {
        "creative_type": creative_type,
        "count": n,
        "source": source,
        "grounded": grounded,
        "rationale": rationale,
        "decisions": decisions,
        **body,
    }
