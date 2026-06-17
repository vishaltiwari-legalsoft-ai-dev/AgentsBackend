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

from .prompts import load_prompt
from .tokens import (
    DEFAULT_CTA,
    DEFAULT_HEADLINE,
    DEFAULT_HIGHLIGHT,
    DEFAULT_SUBTEXT_1,
    DEFAULT_SUBTEXT_2,
    STAGE1_AR_ANCHOR,
    validate_stage3_tokens,
)
from .variants import (
    BRAND_KIT_BLOCK,
    LOCKED_COLORS,
    SOURCE_NOTE_STAGE1,
    STAGE1_VARIANTS,
    STAGE2_CATEGORIES,
    STAGE2_VARIANTS,
)

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


# ── §7.1.1c — AI gradient ("let the agent invent a fresh, on-brand gradient") ─
# The agent studies the canonical Stage-1 gradient prompts and proposes ONE new
# gradient for THIS creative only. The result is TEMPORARY — it is stored on the
# run config, never written to ``prompts/`` and never added to STAGE1_VARIANTS or
# the frozen CANONICAL_SHA256 baseline, so it cannot dilute the prompt library.

# The only colours an on-brand background may use — the locked blue/white family.
# Deliberately excludes the orange CTA + near-black text hexes (never a backdrop)
# while allowing the deep-navy extensions the canonical gradients already use.
_BRAND_GRADIENT_HEXES = {
    h.upper()
    for h in (
        *LOCKED_COLORS["gradient"],          # FFFFFF, BDCFED, A2C0E6, 1746A2
        LOCKED_COLORS["accent"],             # 85AEFD
        LOCKED_COLORS["headline_highlight"]["from"],  # 86AFFE
        LOCKED_COLORS["headline_highlight"]["to"],    # 2653AB
        "#0E2A5E",                           # deep navy used by variants F/J
        "#3A66B5",                           # mid blue used by variant H
    )
}
_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")
_GRADIENT_PROMPT_MIN = 80
_GRADIENT_PROMPT_MAX = 1500


def _validate_gradient_prompt(text: str) -> list[str]:
    """Return validation errors (empty = valid) for a Stage-1 gradient prompt.

    Enforced so a temporary AI gradient stays on-brand and flows through the same
    machinery as the canonical prompts: it must carry the AR anchor (so
    ``tokens.substitute_stage1`` can swap the aspect ratio) and may only reference
    the locked blue/white brand hexes."""
    errors: list[str] = []
    t = (text or "").strip()
    if len(t) < _GRADIENT_PROMPT_MIN:
        errors.append("Gradient prompt is too short.")
    if len(t) > _GRADIENT_PROMPT_MAX:
        errors.append(f"Gradient prompt must be ≤ {_GRADIENT_PROMPT_MAX} characters.")
    if STAGE1_AR_ANCHOR not in t:
        errors.append(f'Gradient prompt must contain the "{STAGE1_AR_ANCHOR}" anchor.')
    off_brand = sorted({h.upper() for h in _HEX_RE.findall(t)} - _BRAND_GRADIENT_HEXES)
    if off_brand:
        errors.append("Off-brand colours not allowed: " + ", ".join(off_brand))
    return errors


# Curated fresh gradients — used offline (no OpenRouter key) or whenever the LLM
# result fails validation. Each is novel vs. the canonical A–L set, brand-only,
# and carries the AR anchor + "no text" so it is byte-compatible with the rest of
# the pipeline. ``cid`` lets the UI exclude already-seen picks on "Regenerate".
_CURATED_GRADIENTS = [
    {
        "cid": "bloom",
        "title": "Radial Brand Bloom",
        "desc": "A soft white core blooming outward through light blue into deep royal blue at the edges.",
        "css_gradient": "radial-gradient(circle at 50% 45%, #FFFFFF 0%, #BDCFED 35%, #A2C0E6 60%, #1746A2 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio immersive abstract background gradient built as a "
            "centered radial bloom. Begin with a luminous pure white #FFFFFF core just above "
            "center, blooming outward through soft light blue #BDCFED and #A2C0E6, and settling "
            "into deep royal blue #1746A2 at the outer edges with a gentle vignette. Soft, "
            "seamless blending with no harsh edges. Minimalist, cinematic, ultra-smooth gradient "
            "texture, high resolution, no noise, no text."
        ),
    },
    {
        "cid": "tritone",
        "title": "Tri-Tone Vertical Wash",
        "desc": "Three feathered horizontal bands — white, mid blue, royal blue — flowing top to bottom.",
        "css_gradient": "linear-gradient(180deg, #FFFFFF 0%, #A2C0E6 50%, #1746A2 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio immersive abstract background gradient as a smooth "
            "vertical wash of three feathered bands. Start with pure white #FFFFFF across the "
            "top, melt through medium blue #A2C0E6 in the middle, and finish in deep royal blue "
            "#1746A2 along the bottom, with each transition softly feathered so no band edge is "
            "visible. Soft, seamless blending with no harsh edges. Minimalist, cinematic, "
            "ultra-smooth gradient texture, high resolution, no noise, no text."
        ),
    },
    {
        "cid": "spotlight",
        "title": "Corner Spotlight Sweep",
        "desc": "A deep royal-blue field lifted by a soft white spotlight glowing from the upper-left.",
        "css_gradient": "radial-gradient(ellipse at 20% 20%, #FFFFFF 0%, #BDCFED 30%, #1746A2 80%)",
        "prompt": (
            "Create a 16:9 aspect ratio immersive abstract background gradient as a deep royal "
            "blue #1746A2 field illuminated by a soft white #FFFFFF spotlight glowing from the "
            "upper-left corner, fading through light blue #BDCFED as it spreads toward the lower-"
            "right. Subtle, even falloff with a calm premium mood. Soft, seamless blending with "
            "no harsh edges. Minimalist, cinematic, ultra-smooth gradient texture, high "
            "resolution, no noise, no text."
        ),
    },
    {
        "cid": "dual",
        "title": "Dual-Origin Diagonal",
        "desc": "Two soft light sources — top-left and bottom-right — meeting over a royal-blue mid.",
        "css_gradient": "linear-gradient(135deg, #FFFFFF 0%, #A2C0E6 50%, #1746A2 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio immersive abstract background gradient lit from two "
            "origins. A pure white #FFFFFF glow enters from the top-left and a soft light blue "
            "#BDCFED glow enters from the bottom-right, both dissolving over a medium blue "
            "#A2C0E6 to deep royal blue #1746A2 diagonal core where they meet. Balanced, airy, "
            "and premium. Soft, seamless blending with no harsh edges. Minimalist, cinematic, "
            "ultra-smooth gradient texture, high resolution, no noise, no text."
        ),
    },
]


def _stable_seed(*parts: str) -> int:
    """A deterministic, run-stable integer from text parts (Python's hash() is
    salted per-process, so we sum char codes instead)."""
    return sum(ord(c) for c in "".join(parts))


# Composition archetypes the LLM is FORCED to use, so results vary instead of
# defaulting to a radial "spotlight". Linear directions lead; radial is one option
# among several, deliberately placed last so it is never the rotation default.
_COMPOSITIONS = [
    ("diagonal", "a LINEAR diagonal sweep flowing corner-to-corner (e.g. top-left to bottom-right)"),
    ("vertical", "a LINEAR vertical wash flowing top-to-bottom (or bottom-to-top)"),
    ("horizontal", "a LINEAR horizontal sweep flowing left-to-right"),
    ("dual", "a LINEAR dual-origin blend with two light sources meeting over a deeper core"),
    ("radial", "a RADIAL bloom glowing from the centre outward"),
    ("spotlight", "a RADIAL off-centre spotlight glowing from one corner across a deep field"),
]

# Steer keywords that PIN a specific composition (override the rotation) so a
# user who types "diagonal" / "vertical" actually gets it.
_STEER_COMPOSITION = {
    "diagonal": "diagonal", "vertical": "vertical", "horizontal": "horizontal",
    "sweep": "diagonal", "linear": "diagonal", "band": "horizontal", "bands": "horizontal",
    "radial": "radial", "bloom": "radial", "center": "radial", "centre": "radial",
    "spotlight": "spotlight", "glow": "radial", "dual": "dual",
}


def _target_composition(steer: str, exclude: set[str]) -> tuple[str, str]:
    """Pick the composition the LLM must use. A directional word in the steer wins;
    otherwise rotate (seeded by the steer + how many picks were excluded) so each
    'Regenerate' returns a different layout instead of always radial."""
    s = (steer or "").lower()
    for kw, key in _STEER_COMPOSITION.items():
        if kw in s:
            return next(c for c in _COMPOSITIONS if c[0] == key)
    seed = _stable_seed(steer) + len(exclude)
    return _COMPOSITIONS[seed % len(_COMPOSITIONS)]


def _curated_gradient(answers: dict, steer: str, exclude: set[str]) -> dict:
    """Pick one curated gradient, rotating past any already-seen ``cid``s so a
    "Regenerate" always returns something different."""
    pool = [g for g in _CURATED_GRADIENTS if g["cid"] not in exclude] or _CURATED_GRADIENTS
    start = _stable_seed(steer, json.dumps(answers, sort_keys=True)) % len(pool)
    g = pool[(start + len(exclude)) % len(pool)]
    return {
        "type": "gradient",
        "state": "proposed",
        "source": "agent",
        "ai": False,
        "gradient": {
            "id": "AI",
            "cid": g["cid"],
            "title": g["title"],
            "desc": g["desc"],
            "prompt": g["prompt"],
            "css_gradient": g["css_gradient"],
        },
        "note": "Temporary — used only for this creative, not saved to the library.",
    }


def suggest_gradient(
    answers: dict | None = None,
    *,
    steer: str | None = None,
    exclude: list[str] | None = None,
) -> dict:
    """Propose ONE fresh, on-brand Stage-1 gradient for this creative only.

    Curated + deterministic so it works fully offline; when an OpenRouter key is
    configured the gradient is written by the model (best-effort — any failure or
    invalid/off-brand result silently falls back to the curated pick)."""
    answers = answers or {}
    steer = (steer or "").strip()
    excl = set(exclude or [])
    curated = _curated_gradient(answers, steer, excl)
    enriched = _enrich_gradient_with_llm(answers, steer, curated, excl)
    return enriched or curated


def _enrich_gradient_with_llm(
    answers: dict, steer: str, curated: dict, exclude: set[str] | None = None
) -> dict | None:
    """Best-effort: let the model study the canonical gradients and invent a new
    on-brand one. Returns None on any problem (caller keeps the curated pick)."""
    try:
        from app.services.openrouter import get_llm  # lazy — package works without the app
    except Exception:
        return None

    inspiration = "\n".join(
        f"- {v['id']} — {v['title']}: {load_prompt(v['prompt_file']).strip()}"
        for v in STAGE1_VARIANTS
    )
    palette = ", ".join(sorted(_BRAND_GRADIENT_HEXES))
    _, comp_directive = _target_composition(steer, exclude or set())
    steer_line = f"\nUser steer (honour its mood, e.g. warmer/minimal): {steer}" if steer else ""
    prompt = (
        "You are an art director for premium legal-tech ad backgrounds. Study the "
        "existing brand gradient prompts below and invent ONE NEW gradient that is "
        "visibly different from all of them, while staying strictly on-brand.\n\n"
        "HARD RULES:\n"
        f'1. The "prompt" MUST begin with "Create a {STAGE1_AR_ANCHOR} immersive '
        'abstract background gradient" and MUST end with "no noise, no text.".\n'
        f"2. Use ONLY these brand hex colours, by code: {palette}. No other colours.\n"
        f"3. COMPOSITION — the gradient MUST be {comp_directive}. Do NOT use any "
        "other composition (do NOT default to a radial bloom unless told to here). "
        "Make the named composition unmistakable in the wording.\n"
        "4. Within that fixed composition, get novelty from colour ordering, light "
        "position, feathering and vignette — NEVER from new colours.\n"
        "5. No people, objects, logos, or text in the image.\n\n"
        f"Brand kit (reference):\n{BRAND_KIT_BLOCK}\n{SOURCE_NOTE_STAGE1}\n\n"
        f"Existing gradient prompts:\n{inspiration}{steer_line}\n\n"
        'Return ONLY minified JSON: {"title":"2-4 words","desc":"one sentence",'
        '"prompt":"the full gradient prompt","css_gradient":"a CSS gradient that '
        "matches the composition above (linear-gradient(...) for linear, "
        'radial-gradient(...) for radial) using only the brand hexes"}'
    )
    try:
        msg = get_llm(temperature=0.8, fast=True).invoke(prompt)
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return None
        data = json.loads(match.group(0))
        text = str(data.get("prompt") or "").strip()
        if _validate_gradient_prompt(text):  # non-empty error list → reject
            return None
        css = str(data.get("css_gradient") or "").strip()
        css_ok = (
            css.startswith(("linear-gradient(", "radial-gradient("))
            and not ({h.upper() for h in _HEX_RE.findall(css)} - _BRAND_GRADIENT_HEXES)
        )
        title = str(data.get("title") or "AI Gradient").strip()[:48]
        desc = str(data.get("desc") or curated["gradient"]["desc"]).strip()[:160]
        return {
            **curated,
            "source": "agent+llm",
            "ai": True,
            "gradient": {
                "id": "AI",
                "cid": "llm",
                "title": title,
                "desc": desc,
                "prompt": text,
                "css_gradient": css if css_ok else curated["gradient"]["css_gradient"],
            },
        }
    except Exception:
        return None


# ── §7.1.1d — AI element ("let the agent invent a fresh element / subject") ────
# Symmetric to the AI gradient: the agent studies the Stage-2 catalogue subjects
# and writes ONE brand-new element for THIS creative only. Stored on the run
# config (``custom_element``), never added to STAGE2_VARIANTS. A Stage-2 element
# describes ONLY the foreground subject — never the background/gradient/palette,
# which Stage 1 owns (enforced by ``_validate_element_subject``).

_ELEMENT_SUBJECT_MIN = 60
_ELEMENT_SUBJECT_MAX = 700
# Words that mean the subject is straying into Stage-1's territory (background).
_ELEMENT_BANNED_WORDS = ("gradient", "background", "backdrop", "palette", "wallpaper")


def _validate_element_subject(text: str) -> list[str]:
    """Return validation errors (empty = valid) for a Stage-2 element subject.

    Mirrors the catalogue invariants (see tests/test_stage2_elements.py): the
    subject is foreground-only — no colour codes and no background/gradient words —
    so the shared blend prompt keeps owning the Stage-1 base."""
    errors: list[str] = []
    t = (text or "").strip()
    if len(t) < _ELEMENT_SUBJECT_MIN:
        errors.append("Element description is too short.")
    if len(t) > _ELEMENT_SUBJECT_MAX:
        errors.append(f"Element description must be ≤ {_ELEMENT_SUBJECT_MAX} characters.")
    if "#" in t:
        errors.append("Element must not specify colour codes (the palette is locked to Stage 1).")
    low = t.lower()
    hit = [w for w in _ELEMENT_BANNED_WORDS if w in low]
    if hit:
        errors.append("Element must describe only the subject, not the background: " + ", ".join(hit))
    return errors


# Curated fresh elements — used offline or when the LLM result fails validation.
# Each is foreground-only (no background/colours), leaves negative space for copy,
# and is novel vs. the catalogue. ``cid`` lets the UI exclude already-seen picks.
_CURATED_ELEMENTS = [
    {
        "cid": "scales",
        "title": "Minimal Scales of Justice",
        "desc": "An ultra-minimal matte 3D set of balanced justice scales, one thin orange accent.",
        "category": "object",
        "subject": (
            "An ultra-minimal matte 3D rendering of a balanced set of scales of justice, "
            "perfectly level, with one thin warm-orange accent line along the beam and a single "
            "soft shadow beneath. Object centered-low, vast empty space around it. No pedestal "
            "clutter, no engraving, restrained and premium."
        ),
    },
    {
        "cid": "casefiles",
        "title": "Stacked Case Files",
        "desc": "A neat stack of manila case folders, one edge fanned, lower-left, copy space above.",
        "category": "flatlay",
        "subject": (
            "A neat stack of manila case folders bound with a single elastic band, the top "
            "folder's edge slightly fanned to show ordered documents, resting in the lower-left "
            "with soft overhead daylight and a faint natural shadow. Upper-right kept clear. "
            "Sharp focus, clean isolated objects, no desk clutter."
        ),
    },
    {
        "cid": "nightdesk",
        "title": "Late-Night Desk Lamp",
        "desc": "A lone desk lamp pooling warm light over an empty chair and closed laptop.",
        "category": "scene",
        "subject": (
            "A single desk lamp casting a warm pool of light over an empty leather office chair "
            "and a closed silver laptop, the rest of the room dissolving into soft shadow, NO "
            "people. Told as a quiet after-hours moment, set into the lower-right of the frame "
            "with ample dark negative space above and left. Photoreal, matte, cinematic."
        ),
    },
    {
        "cid": "assuredva",
        "title": "Assured VA, Headset",
        "desc": "Half-body professional in a charcoal blazer with a slim headset, lower-right.",
        "category": "people",
        "subject": (
            "A photoreal half-body of a professional in their early 30s wearing a charcoal blazer "
            "over a plain top and a slim discreet headset, calm assured half-smile, shoulders "
            "angled slightly to camera. 85mm, f/1.8, one soft window light with gentle shadow "
            "falloff. Subject in the lower-right, generous negative space upper-left. Matte, fine "
            "grain, premium restraint."
        ),
    },
    {
        "cid": "skylinewindow",
        "title": "Corner-Office Window View",
        "desc": "A high floor-to-ceiling window framing distant towers, foreground in shadow.",
        "category": "architecture",
        "subject": (
            "A view through a high floor-to-ceiling corner-office window framing distant glass "
            "towers in soft daylight haze, the window mullions and foreground sill kept in gentle "
            "shadow, NO people, no logos. Shot from inside, deep focus, the window occupying the "
            "right side and leaving the left open. Clean, premium, architectural calm."
        ),
    },
]


# Steer keyword → category, so an offline / fallback pick still matches what the
# user typed instead of rotating blindly.
_ELEMENT_CATEGORY_HINTS = {
    "people": ("woman", "man", "person", "people", "lawyer", "attorney", "assistant",
               "va", "team", "colleague", "colleagues", "portrait", "staff",
               "professional", "female", "male", "paralegal", "client", "headshot"),
    "object": ("gavel", "pen", "scale", "scales", "contract", "book", "laptop", "phone",
               "key", "door", "toggle", "handshake", "object", "icon", "device"),
    "flatlay": ("flatlay", "flat-lay", "desk", "files", "file", "folder", "paperwork",
                "documents", "document", "stationery", "workspace", "top-down", "overhead"),
    "architecture": ("office", "building", "tower", "towers", "skyline", "city",
                     "interior", "architecture", "window", "conference", "boardroom", "lobby"),
    "scene": ("scene", "night", "lamp", "evening", "room", "story", "moment", "late",
              "after-hours", "atmosphere"),
}


def _infer_element_category(steer: str) -> str | None:
    """Best-matching catalogue category for the user's steer (None if no signal)."""
    s = (steer or "").lower()
    best, score = None, 0
    for cat, kws in _ELEMENT_CATEGORY_HINTS.items():
        c = sum(1 for k in kws if k in s)
        if c > score:
            best, score = cat, c
    return best


def _curated_element(answers: dict, steer: str, exclude: set[str]) -> dict:
    """Pick one curated element, preferring the steer's category and rotating past
    already-seen ``cid``s so the offline fallback still tracks what the user typed."""
    pool = [e for e in _CURATED_ELEMENTS if e["cid"] not in exclude] or _CURATED_ELEMENTS
    cat = _infer_element_category(steer)
    preferred = [e for e in pool if e["category"] == cat] if cat else []
    pick_from = preferred or pool
    start = _stable_seed(steer, json.dumps(answers, sort_keys=True)) % len(pick_from)
    e = pick_from[(start + len(exclude)) % len(pick_from)]
    return {
        "type": "element",
        "state": "proposed",
        "source": "agent",
        "ai": False,
        "element": {
            "id": "AI",
            "cid": e["cid"],
            "title": e["title"],
            "desc": e["desc"],
            "category": e["category"],
            "subject": e["subject"],
        },
        "note": "Temporary — used only for this creative, not added to the element library.",
    }


def suggest_element(
    answers: dict | None = None,
    *,
    steer: str | None = None,
    exclude: list[str] | None = None,
) -> dict:
    """Propose ONE fresh, foreground-only Stage-2 element for this creative only.

    Curated + deterministic offline; an OpenRouter key upgrades it to a model-
    written element (best-effort — any failure or invalid result falls back)."""
    answers = answers or {}
    steer = (steer or "").strip()
    excl = set(exclude or [])
    curated = _curated_element(answers, steer, excl)
    enriched = _enrich_element_with_llm(answers, steer, curated)
    return enriched or curated


def _enrich_element_with_llm(answers: dict, steer: str, curated: dict) -> dict | None:
    """Best-effort: let the model study the catalogue and invent a new element.
    Returns None on any problem (caller keeps the curated pick)."""
    try:
        from app.services.openrouter import get_llm  # lazy — package works without the app
    except Exception:
        return None

    catalog = "\n".join(
        f"- {v['id']} — {v['title']} ({v['category']}): {v['subject']}" for v in STAGE2_VARIANTS
    )
    cats = ", ".join(STAGE2_CATEGORIES)
    # The user's request is the #1 driver. State it up front AND as the first rule
    # so the model depicts exactly what they typed instead of a generic catalogue echo.
    if steer:
        request_line = (
            f'THE USER ASKED FOR: "{steer}"\nYour element MUST clearly and literally '
            "depict this. This is the single most important requirement — do not "
            "substitute a different subject.\n\n"
        )
        rule1 = (
            f'1. MATCH THE REQUEST: the element MUST be a faithful depiction of "{steer}". '
            "Pick the category that best fits it.\n"
        )
    else:
        request_line = ""
        rule1 = (
            "1. Invent ONE element that is visibly different from every catalogue entry.\n"
        )
    prompt = (
        "You are an art director for premium legal-tech ad creatives. The gradient "
        "background is locked separately (Stage 1), so you design ONLY the foreground "
        "subject ('element').\n\n"
        f"{request_line}"
        "HARD RULES:\n"
        f"{rule1}"
        "2. Describe ONLY the foreground subject — a person, object, flatlay, "
        "architecture or scene. NEVER mention the background, gradient, wallpaper, "
        "palette, or any colour/hex codes (Stage 1 owns those).\n"
        "3. Compose for ad copy: place the subject to one side / lower area and leave "
        "generous negative space for a headline.\n"
        f"4. Choose a category from exactly this set: {cats}.\n"
        "5. Photoreal or clean 3D, premium and restrained. No logos, no on-image text.\n\n"
        f"Client answers: {json.dumps(answers)}\n\n"
        f"Catalogue (for style reference only — do NOT copy these):\n{catalog}\n\n"
        'Return ONLY minified JSON: {"title":"2-4 words","desc":"one short sentence",'
        '"category":"one of the allowed categories","subject":"the full element '
        'description, foreground only"}'
    )
    try:
        llm = get_llm(temperature=0.8, fast=True)
        data = None
        # Two attempts: a description that mentions the background (common photo
        # phrasing) fails validation, so we retry once with a pointed correction
        # rather than silently dropping a well-matched element to the curated pick.
        for attempt in range(2):
            ask = prompt if attempt == 0 else (
                prompt + "\n\nYOUR PREVIOUS ANSWER WAS REJECTED: it mentioned the "
                "background/a colour, or was malformed. Return ONLY the foreground "
                "subject — no background, scene wash, gradient, or colour words."
            )
            msg = llm.invoke(ask)
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            match = re.search(r"\{.*\}", content, re.S)
            if not match:
                continue
            candidate = json.loads(match.group(0))
            subject = str(candidate.get("subject") or "").strip()
            if not _validate_element_subject(subject):  # empty error list → valid
                data = candidate
                break
        if data is None:
            return None
        subject = str(data.get("subject") or "").strip()
        category = str(data.get("category") or "").strip().lower()
        if category not in STAGE2_CATEGORIES:
            category = curated["element"]["category"]
        title = str(data.get("title") or "AI Element").strip()[:48]
        desc = str(data.get("desc") or curated["element"]["desc"]).strip()[:160]
        return {
            **curated,
            "source": "agent+llm",
            "ai": True,
            "element": {
                "id": "AI",
                "cid": "llm",
                "title": title,
                "desc": desc,
                "category": category,
                "subject": subject,
            },
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
