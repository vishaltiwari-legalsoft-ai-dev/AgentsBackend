"""Agent Suggestion layer (spec §7) — proposes, never decides.

Every suggestion is returned in the ``proposed`` state with ``source: "agent"``.
A value only reaches a prompt after the router records an explicit approve event
in the run manifest (§7.2). Suggestions are curated + validated against §6.3 so
the layer works fully offline; an LLM can be layered on top via ``enrich_*``
hooks without changing the approval contract.

Hooks are tailored to the chosen Stage-2 concept's emotional angle (§7.1.3):
D → pain-point, B → social-proof/global-talent, C/E → authority, A → efficiency.
"""

from __future__ import annotations

import json
import re

from .tokens import (
    DEFAULT_CTA,
    DEFAULT_HEADLINE,
    DEFAULT_HIGHLIGHT,
    DEFAULT_SUBTEXT_1,
    DEFAULT_SUBTEXT_2,
    validate_stage3_tokens,
)
from .variants import STAGE2_VARIANTS

# ── §7.1.1 onboarding questions ───────────────────────────────────────────────
ONBOARDING_QUESTIONS = [
    {
        "id": "goal",
        "question": "What's the campaign goal?",
        "options": [{"id": "lead_gen", "label": "Lead generation"}, {"id": "brand", "label": "Brand awareness"}],
    },
    {
        "id": "audience",
        "question": "Who are we talking to?",
        "options": [{"id": "solo", "label": "Solo attorneys"}, {"id": "partners", "label": "Firm partners"}],
    },
    {
        "id": "angle",
        "question": "What emotional angle?",
        "options": [{"id": "aspiration", "label": "Aspiration"}, {"id": "pain", "label": "Pain-point"}],
    },
]


def _variant_index() -> dict[str, dict]:
    return {v["id"]: v for v in STAGE2_VARIANTS}


# Hand-written rationales for the primary recommendation set; every other
# variant falls back to its catalogue ``desc``.
_CONCEPT_RATIONALE = {
    "A": "Warm, efficient, approachable — great default for lead-gen.",
    "B": "Social proof / global talent — best for brand awareness.",
    "C": "Authority — a peer professional for partner audiences.",
    "D": "Pain-point storytelling — stops partners mid-scroll.",
    "E": "Institutional authority — prestige architecture.",
}


def recommend_concept(answers: dict) -> dict:
    """Recommend ONE Stage-2 variant with a rationale (§7.1.1). Highlights only.

    The recommendation still comes from the curated primary set (A–E); the
    returned ``variants`` list now covers the full element catalogue so the UI
    can show every option with a one-line rationale.
    """
    angle = answers.get("angle")
    audience = answers.get("audience")
    if angle == "pain":
        rec, why = "D", "Partners scrolling at night → D's 11:47 PM burnout scene mirrors their reality; pair it with an empathy hook."
    elif audience == "partners":
        rec, why = "C", "Firm partners respond to authority — C's full-figure professional reads as a peer they'd introduce to clients."
    elif answers.get("goal") == "brand":
        rec, why = "B", "Brand awareness favours B's honeycomb trio — it signals vetted global talent without a hard sell."
    else:
        rec, why = "A", "Lead-gen for solo attorneys → A's single warm VA is approachable and efficiency-forward."
    return {
        "type": "concept",
        "state": "proposed",
        "source": "agent",
        "recommended": rec,
        "rationale": why,
        "variants": [
            {
                "id": v["id"],
                "recommended": v["id"] == rec,
                "rationale": _CONCEPT_RATIONALE.get(v["id"], v["desc"]),
            }
            for v in STAGE2_VARIANTS
        ],
    }


# ── §7.1.1b — element explorer ("let the agent play with new elements") ───────
# Leans into the less-obvious categories the user might not reach for first.
_EXPLORE_BIAS = ["object", "flatlay", "architecture", "scene"]

# Curated, creative one-liners for the elements the explorer tends to surface.
_EXPLORE_REASON = {
    "G": "A bare contract line and pen says 'simple paperwork' without a single face — pure, premium, copy-friendly.",
    "H": "An open door is the cleanest metaphor for 'start now' — symbolic, minimal, tons of headline room.",
    "I": "A single 'on' toggle sells instant activation — perfect for a speed / 'in 3 days' message.",
    "K": "A quiet handshake carries trust and partnership while staying abstract and brand-safe.",
    "N": "A tidy desk flatlay reads as organised competence and leaves the upper-left wide open for text.",
    "Q": "A signed-contract flatlay closes the loop on 'deal done' — proof without a stock-photo face.",
    "O": "A hazy glass skyline signals scale and authority with zero people to art-direct.",
    "R": "Blue-hour towers add warmth and ambition — premium without feeling corporate-cold.",
    "D": "An empty 11:47 PM office tells the burnout story in one frame — strong for a pain-point hook.",
    "P": "A two-person desk scene shows mentorship / collaboration when one VA isn't enough.",
    "L": "A calm, assured portrait with big upper-left negative space is an easy, premium base to riff on.",
}

# Default exploration order — deliberately fresh ideas first.
_EXPLORE_ORDER = ["G", "H", "I", "K", "N", "Q", "O", "R", "D", "P", "L"]


def _explore_reason(vid: str, idx: dict[str, dict]) -> str:
    return _EXPLORE_REASON.get(vid) or idx[vid]["desc"]


def _curated_explore(answers: dict, exclude: set[str]) -> dict:
    idx = _variant_index()
    order = [x for x in _EXPLORE_ORDER if x in idx]
    if answers.get("angle") == "pain":
        order = ["D"] + [x for x in order if x != "D"]
    elif answers.get("goal") == "brand":
        arch = [x for x in order if idx[x]["category"] == "architecture"]
        order = arch + [x for x in order if x not in arch]
    order = [x for x in order if x not in exclude]

    pick_ids = order[:3]
    rest = order[3:]
    wildcard_id = next(
        (x for x in rest if idx[x]["category"] in ("object", "scene")),
        rest[0] if rest else (pick_ids[-1] if pick_ids else None),
    )

    def card(vid: str) -> dict:
        v = idx[vid]
        return {"id": vid, "title": v["title"], "category": v["category"], "reason": _explore_reason(vid, idx)}

    return {
        "type": "explore",
        "state": "proposed",
        "source": "agent",
        "ai": False,
        "picks": [card(x) for x in pick_ids],
        "wildcard": card(wildcard_id) if wildcard_id else None,
        "idea": "Mix it up — these defer the whole background to your locked Stage 1 gradient, so the element is the only variable to play with.",
        "note": "Suggestions only — nothing is generated until you pick a card and hit generate.",
    }


def explore_elements(answers: dict | None = None, exclude: list[str] | None = None) -> dict:
    """The agent 'plays' with the wider element library and proposes fresh picks.

    Curated + deterministic so it works fully offline; when an OpenRouter key is
    configured the reasoning is rewritten by the model (best-effort — any
    failure silently falls back to the curated result).
    """
    answers = answers or {}
    curated = _curated_explore(answers, set(exclude or []))
    enriched = _enrich_explore_with_llm(answers, curated)
    return enriched or curated


def _enrich_explore_with_llm(answers: dict, curated: dict) -> dict | None:
    """Best-effort LLM rewrite of the explorer's reasoning. Returns None on any
    problem so the caller keeps the curated result."""
    idx = _variant_index()
    try:
        from app.services.openrouter import get_llm  # lazy — package works without the app
    except Exception:
        return None
    catalog = "\n".join(
        f"- {v['id']} — {v['title']} ({v['category']}): {v['desc']}" for v in STAGE2_VARIANTS
    )
    prompt = (
        "You are a creative director for premium legal-tech ad creatives. The "
        "gradient background is locked separately, so each catalogue 'element' "
        "is just the foreground subject. From this FIXED catalogue, choose 3 "
        "less-obvious elements worth experimenting with and 1 bold wildcard, "
        "tuned to the client answers. Use ids EXACTLY as written.\n\n"
        f"Catalogue:\n{catalog}\n\nClient answers: {json.dumps(answers)}\n\n"
        'Return ONLY minified JSON: {"picks":[{"id":"X","reason":"one punchy '
        'sentence"}],"wildcard":{"id":"X","reason":"one sentence"},"idea":"one '
        'sentence on combining or pushing the elements"}'
    )
    try:
        msg = get_llm(temperature=0.7, fast=True).invoke(prompt)
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return None
        data = json.loads(match.group(0))

        def card(item: dict) -> dict | None:
            vid = str(item.get("id", "")).upper()
            if vid not in idx:
                return None
            v = idx[vid]
            reason = str(item.get("reason") or _explore_reason(vid, idx)).strip()
            return {"id": vid, "title": v["title"], "category": v["category"], "reason": reason}

        picks = [c for c in (card(p) for p in data.get("picks", [])) if c]
        if not picks:
            return None
        wildcard = card(data.get("wildcard") or {})
        return {
            **curated,
            "source": "agent+llm",
            "ai": True,
            "picks": picks[:3],
            "wildcard": wildcard or curated["wildcard"],
            "idea": str(data.get("idea") or curated["idea"]).strip(),
        }
    except Exception:
        return None


def recommend_aspect_ratio(placement: str | None) -> dict:
    """§7.1.2 — placement → AR."""
    table = {"feed": "4:5", "story": "9:16", "reel": "9:16", "linkedin": "1:1"}
    ar = table.get((placement or "feed").lower(), "4:5")
    why = {
        "4:5": "Instagram/Facebook feed → 4:5 maximises vertical real estate without cropping.",
        "9:16": "Story/Reel placement → full-bleed 9:16.",
        "1:1": "LinkedIn → 1:1 is safest across its feed crops.",
    }[ar]
    return {"type": "aspect_ratio", "state": "proposed", "source": "agent", "recommended": ar, "rationale": why}


# ── §7.1.3 hooks, tailored per concept ────────────────────────────────────────
_HOOKS: dict[str, dict] = {
    "A": {
        "headlines": [
            ("Hire Vetted Virtual Legal Staff Fast", "Virtual Legal Staff"),
            ("Scale Your Firm With Virtual Legal Staff", "Virtual Legal Staff"),
            ("Add Trusted Virtual Legal Staff Today", "Virtual Legal Staff"),
            ("Grow Faster With Virtual Legal Staff", "Virtual Legal Staff"),
            (DEFAULT_HEADLINE, DEFAULT_HIGHLIGHT),
        ],
        "subtext": [
            (DEFAULT_SUBTEXT_1, DEFAULT_SUBTEXT_2),
            ("Trained legal VAs, ready when you are.", "Onboard in days, not months."),
        ],
    },
    "B": {
        "headlines": [
            ("Tap Into Global Virtual Legal Staff", "Virtual Legal Staff"),
            ("Hire World-Class Virtual Legal Staff", "Virtual Legal Staff"),
            ("The Best Virtual Legal Staff, Vetted", "Virtual Legal Staff"),
            ("Pre-Vetted Virtual Legal Staff Worldwide", "Virtual Legal Staff"),
            (DEFAULT_HEADLINE, DEFAULT_HIGHLIGHT),
        ],
        "subtext": [
            ("Choose from pre-vetted talent across the globe.", "The top 1% of legal VAs, ready to start."),
            (DEFAULT_SUBTEXT_1, DEFAULT_SUBTEXT_2),
        ],
    },
    "C": {
        "headlines": [
            ("Elevate Your Firm With Legal Staff", "Legal Staff"),
            ("Professional Virtual Legal Staff For Firms", "Virtual Legal Staff"),
            ("Trusted Virtual Legal Staff For Attorneys", "Virtual Legal Staff"),
            ("The Standard In Virtual Legal Staff", "Virtual Legal Staff"),
            (DEFAULT_HEADLINE, DEFAULT_HIGHLIGHT),
        ],
        "subtext": [
            ("Experienced legal professionals, vetted to your standard.", "Integrated with your firm in days."),
            (DEFAULT_SUBTEXT_1, DEFAULT_SUBTEXT_2),
        ],
    },
    "D": {
        "headlines": [
            ("Reclaim Your Evenings With Legal Staff", "Legal Staff"),
            ("Stop Drowning In Legal Casework Alone", "Legal Casework"),
            ("Your Caseload Shouldn't Cost Your Nights", "Your Nights"),
            ("Get Your Life Back, Delegate Casework", "Delegate Casework"),
            (DEFAULT_HEADLINE, DEFAULT_HIGHLIGHT),
        ],
        "subtext": [
            ("The 11 PM grind isn't sustainable — or necessary.", "Delegate the overflow to pre-vetted legal VAs."),
            ("Your caseload shouldn't cost your evenings.", "Start delegating in under 3 days."),
        ],
    },
}
_HOOKS["E"] = _HOOKS["C"]  # both authority


_CTAS = ["Book a Free Consultation", "Get Started Today", "Hire Your Legal VA"]


def generate_hooks(concept_id: str | None) -> dict:
    """§7.1.3 — 5 headline hooks, 3 CTAs, 2 sub-text pairs, all §6.3-valid."""
    pack = _HOOKS.get((concept_id or "A").upper(), _HOOKS["A"])
    headlines = []
    for text, highlight in pack["headlines"]:
        errors = validate_stage3_tokens(
            headline=text, highlight=highlight,
            subtext1=DEFAULT_SUBTEXT_1, subtext2=DEFAULT_SUBTEXT_2, cta=DEFAULT_CTA,
        )
        # Only headline/highlight errors are relevant here.
        relevant = [e for e in errors if "Headline" in e or "Highlight" in e]
        if not relevant:
            headlines.append({"headline": text, "highlight": highlight, "state": "proposed", "source": "agent"})
    ctas = [{"cta": c, "state": "proposed", "source": "agent"} for c in _CTAS if len(c.split()) <= 4]
    subtext = [
        {"subtext1": a, "subtext2": b, "state": "proposed", "source": "agent"}
        for a, b in pack["subtext"]
        if len(a) <= 70 and len(b) <= 70
    ]
    return {"type": "hooks", "headlines": headlines, "ctas": ctas, "subtext_pairs": subtext}


def recommend_font(concept_id: str | None) -> dict:
    """§7.1.4 — font is locked to the Causten family; only the weight varies."""
    # Authority concepts read heavier; warm/efficiency concepts read a touch lighter.
    alt = "Causten ExtraBold" if (concept_id or "").upper() in ("C", "D", "E") else "Causten SemiBold"
    return {
        "type": "font",
        "state": "proposed",
        "source": "agent",
        "family": "Causten",
        "locked": True,
        "recommended": "Causten Bold",
        "alternative": alt,
        "rationale": (
            "Causten is the locked brand font — every creative uses it. Causten Bold "
            f"is the punchy default for headlines; {alt} is an in-family weight if you "
            "want a different emphasis."
        ),
    }


_QA = {
    1: "Check the gradient for banding in the upper third — regenerate if you see stepping.",
    2: "Verify hands and faces look natural; for the honeycomb, confirm no hexagons overlap.",
    3: "Confirm the headline fits the left 40% without clipping and the CTA pill reads cleanly.",
    4: "Confirm the logo sits flush in the top-left with clean margins and nothing is cropped.",
}


def qa_critique(stage: int) -> dict:
    """§7.1.5 — advisory only, never auto-regenerates."""
    return {"type": "qa", "state": "proposed", "source": "agent", "stage": stage, "note": _QA.get(stage, "")}
