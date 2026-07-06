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
import logging
import re

logger = logging.getLogger("agentos.gd.suggestions")

from .prompts import load_prompt
from .stage1_gradient import (
    SOURCE_NOTE_STAGE1,
    STAGE1_AR_ANCHOR,
    STAGE1_VARIANTS,
)
from .stage2_element import STAGE2_CATEGORIES, STAGE2_VARIANTS
from .stage3_text.prompting import (
    DEFAULT_CTA,
    DEFAULT_HEADLINE,
    DEFAULT_HIGHLIGHT,
    DEFAULT_SUBTEXT_1,
    DEFAULT_SUBTEXT_2,
    validate_stage3_tokens,
)
from .variants import BRAND_KIT_BLOCK, LOCKED_COLORS

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


# ── §7.1.0 pre-generation discovery (the "micro-conversation") ────────────────
# Before any suggestion is surfaced, the agent walks the user through a short,
# conversational discovery so the output is grounded in INTENT, not generic. Two
# groups: (1) intent — the feeling/outcome, audience, tone, style; (2) context —
# is this for an event, and what theme/message is in mind. The answers are stored
# on the run as ``creative_brief`` and folded into EVERY downstream suggestion
# (concept, explore, gradient, element, hooks) plus the synthesized direction.
#
# ``kind`` drives how the UI renders a turn: ``choice`` = chips only,
# ``text`` = free text only, ``choice_text`` = chips with a free-text override.
# Option ids are kept stable across brand wordings so the curated heuristics below
# (and the legacy goal/angle derivation) work for every pack.
DISCOVERY_QUESTIONS = [
    # — Step 1 · Intent —
    {
        "id": "feeling", "group": "intent", "kind": "choice_text",
        "prompt": "First — what feeling or outcome should this creative land? "
                  "Pick the closest, or tell me in your own words.",
        "options": [
            {"id": "trust", "label": "Trust & authority"},
            {"id": "urgency", "label": "Urgency / act now"},
            {"id": "warmth", "label": "Warmth & approachability"},
            {"id": "aspiration", "label": "Aspiration & growth"},
            {"id": "relief", "label": "Relief from overwhelm"},
        ],
        "placeholder": "e.g. calm confidence, “we’ve got your back”",
    },
    {
        "id": "audience", "group": "intent", "kind": "choice_text",
        "prompt": "Who are we talking to?",
        "options": [
            {"id": "solo", "label": "Solo / small attorneys"},
            {"id": "partners", "label": "Firm partners"},
            {"id": "inhouse", "label": "In-house legal teams"},
            {"id": "growing", "label": "Growing firms"},
        ],
        "placeholder": "Describe the audience",
    },
    {
        "id": "tone", "group": "intent", "kind": "choice",
        "prompt": "What tone should it strike?",
        "options": [
            {"id": "premium", "label": "Premium & polished"},
            {"id": "bold", "label": "Bold & punchy"},
            {"id": "friendly", "label": "Friendly & human"},
            {"id": "formal", "label": "Formal & institutional"},
        ],
    },
    {
        "id": "style", "group": "intent", "kind": "choice_text", "optional": True,
        "prompt": "Any visual style you’re leaning toward? (optional)",
        "options": [
            {"id": "minimal", "label": "Minimal & clean"},
            {"id": "editorial", "label": "Editorial"},
            {"id": "cinematic", "label": "Cinematic"},
            {"id": "corporate", "label": "Corporate"},
        ],
        "placeholder": "Optional — references or styling notes",
    },
    # — Step 2 · Event / campaign context —
    {
        "id": "event", "group": "context", "kind": "choice_text",
        "prompt": "Is this for a specific event or moment, or is it evergreen?",
        "options": [
            {"id": "evergreen", "label": "Evergreen"},
            {"id": "webinar", "label": "Webinar / event"},
            {"id": "hiring", "label": "Hiring push"},
            {"id": "seasonal", "label": "Seasonal / holiday"},
            {"id": "launch", "label": "Launch / announcement"},
        ],
        "placeholder": "Name the event, date or moment",
    },
    {
        "id": "theme", "group": "context", "kind": "text", "optional": True,
        "prompt": "Last one — anything specific in mind for the theme or message? (optional)",
        "placeholder": "e.g. “year-end caseload crunch”, a tagline, a campaign name",
    },
]

# Maps that turn discovery answers into natural language for the direction summary
# and into the legacy goal/angle keys the curated concept logic already understands.
_FEELING_PHRASE = {
    "trust": "earns trust and signals authority",
    "urgency": "creates urgency to act now",
    "warmth": "feels warm and approachable",
    "aspiration": "sells aspiration and growth",
    "relief": "promises relief from overwhelm",
}
_TONE_PHRASE = {
    "premium": "premium and polished",
    "bold": "bold and high-contrast",
    "friendly": "friendly and human",
    "formal": "formal and institutional",
}
_STYLE_PHRASE = {
    "minimal": "minimal, lots of negative space",
    "editorial": "editorial and type-led",
    "cinematic": "cinematic and atmospheric",
    "corporate": "clean and corporate",
}
_EVENT_PHRASE = {
    "evergreen": "an evergreen always-on creative",
    "webinar": "tied to a webinar / live event",
    "hiring": "a hiring / recruitment push",
    "seasonal": "a seasonal / holiday moment",
    "launch": "a launch or announcement",
}
# Discovery feeling → the legacy "angle" key (pain-point vs aspiration) that
# recommend_concept already keys off, so the curated A–E logic keeps firing.
_FEELING_ANGLE = {
    "urgency": "pain", "relief": "pain",
    "trust": "aspiration", "warmth": "aspiration", "aspiration": "aspiration",
}
# Discovery event → the legacy "goal" key (lead-gen vs brand awareness).
_EVENT_GOAL = {"hiring": "lead_gen", "launch": "lead_gen", "webinar": "brand", "seasonal": "brand"}


def _discovery_label(qid: str, value: str, questions=None) -> str:
    """Human label for a stored discovery answer (option label, else raw text).

    ``questions`` defaults to the Legal Soft script; pass ``pack.discovery_questions``
    so a templated brand's own wording is used."""
    for q in (questions or DISCOVERY_QUESTIONS):
        if q["id"] == qid:
            for o in q.get("options", []):
                if o["id"] == value:
                    return o["label"]
    return value


def _derive_legacy(brief: dict) -> dict:
    """Back-fill the legacy goal/angle keys from the richer discovery brief so the
    existing curated concept heuristics keep working. Non-destructive: an explicit
    goal/angle in the brief always wins. Returns a NEW merged dict."""
    merged = dict(brief or {})
    feeling = merged.get("feeling")
    if not merged.get("angle") and feeling in _FEELING_ANGLE:
        merged["angle"] = _FEELING_ANGLE[feeling]
    event = merged.get("event")
    if not merged.get("goal") and event in _EVENT_GOAL:
        merged["goal"] = _EVENT_GOAL[event]
    return merged


def _resolve_pack(pack=None):
    """The active brand pack (defaults to Legal Soft). Lazy import avoids a cycle —
    registry imports this module for the Legal Soft content."""
    if pack is not None:
        return pack
    from . import registry

    return registry.get_pack(None)


def _variant_index(stage2_variants) -> dict[str, dict]:
    return {v["id"]: v for v in stage2_variants}


# Hand-written rationales for the primary recommendation set; every other
# variant falls back to its catalogue ``desc``.
_CONCEPT_RATIONALE = {
    "A": "Warm, efficient, approachable — great default for lead-gen.",
    "B": "Social proof / global talent — best for brand awareness.",
    "C": "Authority — a peer professional for partner audiences.",
    "D": "Pain-point storytelling — stops partners mid-scroll.",
    "E": "Institutional authority — prestige architecture.",
}


def recommend_concept(answers: dict, *, pack=None) -> dict:
    """Recommend ONE Stage-2 variant with a rationale (§7.1.1). Highlights only.

    The recommendation still comes from the curated primary set (A–E); the
    returned ``variants`` list now covers the full element catalogue so the UI
    can show every option with a one-line rationale.
    """
    pack = _resolve_pack(pack)
    stage2_variants = pack.stage2_variants
    concept_rationale = pack.concept_rationale
    # Fold the richer discovery brief (feeling/event/…) into the legacy goal/angle
    # keys this curated logic already understands, so the conversation steers the
    # recommendation even when the user never touched the old multiple-choice panel.
    answers = _derive_legacy(answers)
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
                "rationale": concept_rationale.get(v["id"], v["desc"]),
            }
            for v in stage2_variants
        ],
    }


# ── §7.1.0b — synthesized creative direction (the discovery payoff) ───────────
# After the micro-conversation, the agent reflects the brief back as ONE short
# creative direction: which concept it points to, the tone/palette/copy angle to
# carry, and a couple of next-step nudges. Curated + fully offline; an OpenRouter
# key rewrites the prose without changing the contract.


def _curated_direction(brief: dict, pack) -> dict:
    """Deterministic creative direction from the discovery brief + brand pack."""
    concept = recommend_concept(brief, pack=pack)
    rec = concept["recommended"]
    idx = _variant_index(pack.stage2_variants)
    concept_title = idx.get(rec, {}).get("title", rec)

    feeling = brief.get("feeling")
    tone = brief.get("tone")
    style = brief.get("style")
    event = brief.get("event")
    theme = (brief.get("theme") or "").strip()
    audience_lbl = (
        _discovery_label("audience", brief["audience"], pack.discovery_questions)
        if brief.get("audience") else None
    )

    # Build a 2–3 sentence direction, only naming the dimensions the user gave.
    bits = [f"This creative should {_FEELING_PHRASE.get(feeling, 'land your core message')}"]
    if audience_lbl:
        bits[0] += f" for {audience_lbl.lower()}"
    if tone in _TONE_PHRASE:
        bits.append(f"Keep the tone {_TONE_PHRASE[tone]}")
    if style in _STYLE_PHRASE:
        bits.append(f"lean {_STYLE_PHRASE[style]}")
    sentence1 = (bits[0] + ".").replace("..", ".")
    sentence2 = (", ".join(bits[1:]) + ".") if len(bits) > 1 else ""
    direction = f"I’d point this at concept {rec} — {concept_title}: {concept['rationale']}"
    if event and event != "evergreen" and event in _EVENT_PHRASE:
        direction += f" Frame it as {_EVENT_PHRASE[event]}"
        direction += f" around “{theme}”." if theme else "."
    elif theme:
        direction += f" Centre the message on “{theme}”."

    grad = pack.locked_colors.get("gradient", [])
    palette_hint = (
        "Stay on the brand blue/white gradient"
        + (f" ({grad[0]} → {grad[-1]})" if len(grad) >= 2 else "")
        + "; reserve the brand accent for the CTA only."
    )
    # Copy angle: urgency/relief → problem-first; otherwise outcome-first.
    copy_angle = (
        "Open on the pain, then the relief — a short, punchy headline with one highlighted phrase."
        if _FEELING_ANGLE.get(feeling) == "pain"
        else "Lead with the outcome — an aspirational headline with one highlighted phrase."
    )
    highlights = [h for h in (sentence1, sentence2) if h]

    return {
        "type": "direction",
        "state": "proposed",
        "source": "agent",
        "summary": " ".join([s for s in (sentence1, sentence2, direction) if s]),
        "concept": rec,
        "concept_title": concept_title,
        "concept_rationale": concept["rationale"],
        "tone": _TONE_PHRASE.get(tone, tone or ""),
        "palette_hint": palette_hint,
        "copy_angle": copy_angle,
        "highlights": highlights,
    }


def synthesize_direction(brief: dict | None, *, pack=None) -> dict:
    """Reflect the discovery brief back as a single creative direction (§7.1.0b).

    Curated + deterministic so it works fully offline; when an OpenRouter key is
    configured the prose is rewritten by the model (best-effort — any failure
    silently falls back to the curated direction). The recommended concept and
    contract shape are unchanged by the LLM."""
    pack = _resolve_pack(pack)
    curated = _curated_direction(brief or {}, pack)
    return _enrich_direction_with_llm(brief or {}, curated, pack) or curated


def _enrich_direction_with_llm(brief: dict, curated: dict, pack) -> dict | None:
    """Best-effort LLM rewrite of the direction prose. Keeps the curated concept
    pick + palette hint; only the summary/copy_angle prose may change. Returns
    None on any problem so the caller keeps the curated direction."""
    try:
        from app.services.openrouter import get_llm  # lazy — package works without the app
    except Exception:
        logger.debug("OpenRouter not importable; using curated direction fallback")
        return None
    readable = {
        k: _discovery_label(k, v, pack.discovery_questions) if isinstance(v, str) else v
        for k, v in brief.items()
    }
    prompt = (
        "You are a creative director for premium "
        f"{pack.name} ad creatives. A short discovery conversation produced this "
        f"brief: {json.dumps(readable)}.\n\n"
        f"The chosen concept is {curated['concept']} — {curated['concept_title']}: "
        f"{curated['concept_rationale']}. The palette is locked: {curated['palette_hint']}\n\n"
        "Write a crisp creative direction that reflects the brief back to the user. "
        "Do NOT change the concept or palette. Return ONLY minified JSON: "
        '{"summary":"2-3 sentences of direction","copy_angle":"one sentence on the '
        'headline/copy approach"}'
    )
    try:
        msg = get_llm(temperature=0.6, fast=True).invoke(prompt)
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return None
        data = json.loads(match.group(0))
        summary = str(data.get("summary") or "").strip()
        if not summary:
            return None
        return {
            **curated,
            "source": "agent+llm",
            "summary": summary,
            "copy_angle": str(data.get("copy_angle") or curated["copy_angle"]).strip(),
        }
    except Exception:
        logger.warning("LLM direction enrichment failed; using curated fallback", exc_info=True)
        return None


# ── §7.1.0c — the conversation itself (agent talks WITH the user) ─────────────
# A genuine back-and-forth strategist chat (not a fixed questionnaire). The agent
# greets, reads each free-text reply, acknowledges it, pulls intent out of it, and
# asks the next thing — adapting when an OpenRouter key is configured, and still
# holding a real turn-by-turn dialogue offline. When it has enough, it hands back
# the synthesized direction. The frontend keeps the transcript and replays the
# full ``history`` each turn, so this stays stateless server-side.

# Keyword → option-id hints, so the OFFLINE agent can extract intent from a
# free-text reply (the LLM path classifies on its own). Ids are stable across
# brands, so one map serves every pack.
_BRIEF_SYNONYMS = {
    "trust": ["trust", "authority", "credib", "reliable", "trusted", "expert"],
    "urgency": ["urgent", "now", "fast", "quick", "immediately", "deadline", "asap", "hurry"],
    "warmth": ["warm", "friendly", "approach", "human", "welcoming", "caring", "personable"],
    "aspiration": ["aspir", "growth", "ambiti", "success", "elevate", "dream", "premium"],
    "relief": ["relief", "overwhelm", "stress", "burnout", "tired", "calm", "peace", "breathe"],
    "solo": ["solo", "small", "individual", "startup", "myself", "single"],
    "partners": ["partner", "firm", "leadership", "executive", "decision", "principal"],
    "inhouse": ["in-house", "in house", "internal", "counsel", "corporate legal"],
    "growing": ["growing", "scal", "expand", "mid-size", "midsize", "growth-stage"],
    "premium": ["premium", "polished", "luxur", "high-end", "sophisticat", "refined"],
    "bold": ["bold", "punchy", "loud", "striking", "dramatic", "high-contrast", "edgy"],
    "friendly": ["friendly", "human", "casual", "warm", "approach", "conversational"],
    "formal": ["formal", "institution", "serious", "professional", "conservative", "corporate"],
    "minimal": ["minimal", "clean", "simple", "whitespace", "negative space", "uncluttered"],
    "editorial": ["editorial", "magazine", "type-led", "typographic", "layout"],
    "cinematic": ["cinematic", "dramatic", "moody", "atmospher", "film", "lighting"],
    "corporate": ["corporate", "business", "professional"],
    "evergreen": ["evergreen", "always", "general", "no event", "none", "ongoing", "anytime"],
    "webinar": ["webinar", "event", "conference", "workshop", "seminar", "live", "summit"],
    "hiring": ["hiring", "recruit", "job", "careers", "talent", "vacanc", "we're hiring"],
    "seasonal": ["season", "holiday", "christmas", "new year", "summer", "winter", "festive"],
    "launch": ["launch", "announce", "release", "unveil", "introduc", "new service"],
}
# Short acknowledgments the offline agent rotates through so each turn feels like a
# reply, not a form. Chosen deterministically from the reply length (no randomness,
# so resumes/tests stay stable).
_ACKS = ["Got it.", "Love that.", "Makes sense.", "Perfect.", "Great —", "Noted."]
# How many user turns before the offline agent wraps up even if a field is blank.
_CHAT_MAX_USER_TURNS = 8


def _match_option(question: dict, text: str) -> str | None:
    """Best-effort map a free-text reply to one of a discovery question's option
    ids, via the option label words + the synonym hints. None if nothing fits."""
    low = (text or "").lower()
    for opt in question.get("options", []):
        oid = opt["id"]
        label_hit = any(w for w in opt["label"].lower().replace("&", " ").split() if len(w) > 3 and w in low)
        syn_hit = any(s in low for s in _BRIEF_SYNONYMS.get(oid, []))
        if label_hit or syn_hit:
            return oid
    return None


def _ack(text: str) -> str:
    return _ACKS[len(text or "") % len(_ACKS)]


def _chat_messages(history):
    return [m for m in (history or []) if m.get("role") in ("agent", "user")]


def _converse_offline(history, brief, pack) -> dict:
    """Deterministic, offline strategist conversation. Walks the discovery
    dimensions in order but as a dialogue: it fills the dimension the previous
    turn asked about from the user's latest reply, acknowledges it, then asks the
    next open question — or wraps up with the synthesized direction."""
    qs = pack.discovery_questions
    msgs = _chat_messages(history)
    agent_turns = [m for m in msgs if m["role"] == "agent"]
    user_turns = [m for m in msgs if m["role"] == "user"]
    n = len(agent_turns)  # questions asked so far

    brief = dict(brief or {})
    # The user's latest reply answers the most-recently-asked question.
    if user_turns and 1 <= n <= len(qs):
        q = qs[n - 1]
        last = (user_turns[-1].get("text") or "").strip()
        if last:
            brief[q["id"]] = _match_option(q, last) or last

    last_user = (user_turns[-1]["text"] if user_turns else "") or ""
    ack = _ack(last_user) if user_turns else ""
    wrap = n >= len(qs) or len(user_turns) >= _CHAT_MAX_USER_TURNS

    if not wrap and n < len(qs):
        nextq = qs[n]
        if n == 0:
            reply = (f"Hi! I’m your {pack.name} creative strategist. Before I suggest "
                     f"anything, I want to get what you’re going for. {nextq['prompt']}")
        else:
            reply = f"{ack} {nextq['prompt']}".strip()
        return {"type": "chat", "state": "proposed", "source": "agent",
                "reply": reply, "brief": brief, "done": False, "direction": None}

    direction = synthesize_direction(brief, pack=pack)
    reply = (f"{ack} Here’s the direction I’d take: {direction['summary']} "
             "Want me to run with concept "
             f"{direction['concept']}, or tweak anything?").strip()
    return {"type": "chat", "state": "proposed", "source": "agent",
            "reply": reply, "brief": brief, "done": True, "direction": direction}


def _converse_with_llm(history, brief, pack) -> dict | None:
    """Adaptive LLM strategist turn. Returns None (caller falls back to offline)
    when OpenRouter isn't configured or anything goes wrong."""
    try:
        from app.services.openrouter import get_llm  # lazy — package works without the app
    except Exception:
        logger.debug("OpenRouter not importable; using offline strategist conversation")
        return None
    msgs = _chat_messages(history)
    system = (
        f"You are a warm, sharp creative strategist for premium {pack.name} ad "
        "creatives. You are having a SHORT chat to understand the user's intent "
        "BEFORE any design is generated. Ask ONE question per turn, acknowledge "
        "what they just said, and keep replies to 1–2 sentences. Gather: the "
        "feeling/outcome they want, the audience, the tone, optionally a visual "
        "style, whether it's for an event, and any theme/message. When you have "
        "enough (at least feeling + audience + (event or theme)), set done=true "
        "and stop asking.\n\n"
        "Return ONLY minified JSON: {\"reply\":\"your next message\","
        "\"brief\":{\"feeling\":\"\",\"audience\":\"\",\"tone\":\"\",\"style\":\"\","
        "\"event\":\"\",\"theme\":\"\"},\"done\":false}. In brief, prefer these tags "
        "when they fit, else free text — feeling: trust|urgency|warmth|aspiration|"
        "relief; audience: solo|partners|inhouse|growing; tone: premium|bold|"
        "friendly|formal; style: minimal|editorial|cinematic|corporate; event: "
        "evergreen|webinar|hiring|seasonal|launch. Omit a brief key you don't know."
    )
    chat = [("system", system)]
    for m in msgs:
        chat.append(("ai" if m["role"] == "agent" else "human", m.get("text") or ""))
    if not msgs:
        chat.append(("human", "Start the conversation."))
    try:
        out = get_llm(temperature=0.6).invoke(chat)
        content = out.content if isinstance(out.content, str) else str(out.content)
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return None
        data = json.loads(match.group(0))
        reply = str(data.get("reply") or "").strip()
        if not reply:
            return None
        merged = dict(brief or {})
        for k, v in (data.get("brief") or {}).items():
            if isinstance(v, str) and v.strip():
                merged[k] = v.strip()
        done = bool(data.get("done")) or len([m for m in msgs if m["role"] == "user"]) >= _CHAT_MAX_USER_TURNS
        direction = synthesize_direction(merged, pack=pack) if done else None
        return {"type": "chat", "state": "proposed", "source": "agent+llm",
                "reply": reply, "brief": merged, "done": done, "direction": direction}
    except Exception:
        logger.warning("LLM strategist turn failed; using offline conversation", exc_info=True)
        return None


def converse(history=None, brief=None, *, pack=None) -> dict:
    """One strategist turn. LLM-driven when configured, deterministic offline
    fallback otherwise. ``history`` is the full transcript so far (list of
    ``{role: 'agent'|'user', text}``); returns the agent's next ``reply``, the
    updated ``brief``, whether it's ``done``, and the ``direction`` when done."""
    pack = _resolve_pack(pack)
    return _converse_with_llm(history, brief, pack) or _converse_offline(history, brief, pack)


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


def _explore_reason(vid: str, idx: dict[str, dict], explore_reason: dict) -> str:
    return explore_reason.get(vid) or idx[vid]["desc"]


def _curated_explore(answers: dict, exclude: set[str], pack) -> dict:
    idx = _variant_index(pack.stage2_variants)
    order = [x for x in pack.explore_order if x in idx]
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
        return {"id": vid, "title": v["title"], "category": v["category"],
                "reason": _explore_reason(vid, idx, pack.explore_reason)}

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


def explore_elements(answers: dict | None = None, exclude: list[str] | None = None,
                     *, pack=None) -> dict:
    """The agent 'plays' with the wider element library and proposes fresh picks.

    Curated + deterministic so it works fully offline; when an OpenRouter key is
    configured the reasoning is rewritten by the model (best-effort — any
    failure silently falls back to the curated result).
    """
    pack = _resolve_pack(pack)
    answers = answers or {}
    curated = _curated_explore(answers, set(exclude or []), pack)
    enriched = _enrich_explore_with_llm(answers, curated, pack)
    return enriched or curated


def _enrich_explore_with_llm(answers: dict, curated: dict, pack) -> dict | None:
    """Best-effort LLM rewrite of the explorer's reasoning. Returns None on any
    problem so the caller keeps the curated result."""
    idx = _variant_index(pack.stage2_variants)
    try:
        from app.services.openrouter import get_llm  # lazy — package works without the app
    except Exception:
        logger.debug("OpenRouter not importable; using curated suggestion fallback")
        return None
    catalog = "\n".join(
        f"- {v['id']} — {v['title']} ({v['category']}): {v['desc']}" for v in pack.stage2_variants
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
            reason = str(item.get("reason") or _explore_reason(vid, idx, pack.explore_reason)).strip()
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
        logger.warning("LLM element-explore enrichment failed; using curated fallback", exc_info=True)
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


def _validate_gradient_prompt(text: str, *, pack=None) -> list[str]:
    """Return validation errors (empty = valid) for a Stage-1 gradient prompt.

    Enforced so a temporary AI gradient stays on-brand and flows through the same
    machinery as the canonical prompts: it must carry the AR anchor (so
    ``tokens.substitute_stage1`` can swap the aspect ratio) and may only reference
    the selected brand's locked hexes."""
    brand_hexes = _resolve_pack(pack).brand_gradient_hexes
    errors: list[str] = []
    t = (text or "").strip()
    if len(t) < _GRADIENT_PROMPT_MIN:
        errors.append("Gradient prompt is too short.")
    if len(t) > _GRADIENT_PROMPT_MAX:
        errors.append(f"Gradient prompt must be ≤ {_GRADIENT_PROMPT_MAX} characters.")
    if STAGE1_AR_ANCHOR not in t:
        errors.append(f'Gradient prompt must contain the "{STAGE1_AR_ANCHOR}" anchor.')
    off_brand = sorted({h.upper() for h in _HEX_RE.findall(t)} - brand_hexes)
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


def _curated_gradient(answers: dict, steer: str, exclude: set[str], pack) -> dict:
    """Pick one curated gradient, rotating past any already-seen ``cid``s so a
    "Regenerate" always returns something different."""
    curated_gradients = pack.curated_gradients
    pool = [g for g in curated_gradients if g["cid"] not in exclude] or curated_gradients
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
    pack=None,
) -> dict:
    """Propose ONE fresh, on-brand Stage-1 gradient for this creative only.

    Curated + deterministic so it works fully offline; when an OpenRouter key is
    configured the gradient is written by the model (best-effort — any failure or
    invalid/off-brand result silently falls back to the curated pick)."""
    pack = _resolve_pack(pack)
    answers = answers or {}
    steer = (steer or "").strip()
    excl = set(exclude or [])
    curated = _curated_gradient(answers, steer, excl, pack)
    enriched = _enrich_gradient_with_llm(answers, steer, curated, excl, pack)
    return enriched or curated


def _enrich_gradient_with_llm(
    answers: dict, steer: str, curated: dict, exclude: set[str] | None = None, pack=None
) -> dict | None:
    """Best-effort: let the model study the canonical gradients and invent a new
    on-brand one. Returns None on any problem (caller keeps the curated pick)."""
    pack = _resolve_pack(pack)
    try:
        from app.services.openrouter import get_llm  # lazy — package works without the app
    except Exception:
        logger.debug("OpenRouter not importable; using curated suggestion fallback")
        return None

    inspiration = "\n".join(
        f"- {v['id']} — {v['title']}: {pack.load_prompt(v['prompt_file']).strip()}"
        for v in pack.stage1_variants
    )
    palette = ", ".join(sorted(pack.brand_gradient_hexes))
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
        f"Brand kit (reference):\n{pack.brand_kit_block}\n{pack.source_note_stage1}\n\n"
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
        if _validate_gradient_prompt(text, pack=pack):  # non-empty error list → reject
            return None
        css = str(data.get("css_gradient") or "").strip()
        css_ok = (
            css.startswith(("linear-gradient(", "radial-gradient("))
            and not ({h.upper() for h in _HEX_RE.findall(css)} - pack.brand_gradient_hexes)
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
        logger.warning("LLM gradient enrichment failed; using curated fallback", exc_info=True)
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


def _curated_element(answers: dict, steer: str, exclude: set[str], pack) -> dict:
    """Pick one curated element, preferring the steer's category and rotating past
    already-seen ``cid``s so the offline fallback still tracks what the user typed."""
    curated_elements = pack.curated_elements
    pool = [e for e in curated_elements if e["cid"] not in exclude] or curated_elements
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
    pack=None,
) -> dict:
    """Propose ONE fresh, foreground-only Stage-2 element for this creative only.

    Curated + deterministic offline; an OpenRouter key upgrades it to a model-
    written element (best-effort — any failure or invalid result falls back)."""
    pack = _resolve_pack(pack)
    answers = answers or {}
    steer = (steer or "").strip()
    excl = set(exclude or [])
    curated = _curated_element(answers, steer, excl, pack)
    enriched = _enrich_element_with_llm(answers, steer, curated, pack)
    return enriched or curated


def _enrich_element_with_llm(answers: dict, steer: str, curated: dict, pack) -> dict | None:
    """Best-effort: let the model study the catalogue and invent a new element.
    Returns None on any problem (caller keeps the curated pick)."""
    try:
        from app.services.openrouter import get_llm  # lazy — package works without the app
    except Exception:
        logger.debug("OpenRouter not importable; using curated suggestion fallback")
        return None

    catalog = "\n".join(
        f"- {v['id']} — {v['title']} ({v['category']}): {v['subject']}" for v in pack.stage2_variants
    )
    cats = ", ".join(pack.stage2_categories)
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
        if category not in pack.stage2_categories:
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
        logger.warning("LLM element enrichment failed; using curated fallback", exc_info=True)
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


def generate_hooks(concept_id: str | None, *, pack=None) -> dict:
    """§7.1.3 — 5 headline hooks, 3 CTAs, 2 sub-text pairs, all §6.3-valid."""
    pk = _resolve_pack(pack)
    hooks = pk.hooks
    fallback = next(iter(hooks.values())) if hooks else {"headlines": [], "subtext": []}
    block = hooks.get((concept_id or "A").upper(), fallback)
    headlines = []
    for text, highlight in block.get("headlines", []):
        errors = validate_stage3_tokens(
            headline=text, highlight=highlight,
            subtext1=pk.default_subtext_1, subtext2=pk.default_subtext_2, cta=pk.default_cta,
        )
        # Only headline/highlight errors are relevant here.
        relevant = [e for e in errors if "Headline" in e or "Highlight" in e]
        if not relevant:
            headlines.append({"headline": text, "highlight": highlight, "state": "proposed", "source": "agent"})
    ctas = [{"cta": c, "state": "proposed", "source": "agent"} for c in pk.ctas if len(c.split()) <= 4]
    subtext = [
        {"subtext1": a, "subtext2": b, "state": "proposed", "source": "agent"}
        for a, b in block.get("subtext", [])
        if len(a) <= 70 and len(b) <= 70
    ]
    return {"type": "hooks", "headlines": headlines, "ctas": ctas, "subtext_pairs": subtext}


def recommend_font(concept_id: str | None, *, pack=None) -> dict:
    """§7.1.4 — font is locked to the brand's family; only the weight varies."""
    pk = _resolve_pack(pack)
    family = pk.font_family
    recommended = pk.default_font
    names = pk.font_names()
    # Offer a different in-family weight as the alternative (next name, else default).
    alt = next((n for n in names if n != recommended), recommended)
    return {
        "type": "font",
        "state": "proposed",
        "source": "agent",
        "family": family,
        "locked": True,
        "recommended": recommended,
        "alternative": alt,
        "rationale": (
            f"{family} is the locked brand font — every creative uses it. {recommended} "
            f"is the default for headlines; {alt} is an in-family weight if you want a "
            "different emphasis."
        ),
    }


_QA = {
    1: "Check the gradient for banding in the upper third — regenerate if you see stepping.",
    2: "Verify hands and faces look natural; for the honeycomb, confirm no hexagons overlap.",
    3: "Confirm the headline fits the left 40% without clipping and the CTA pill reads cleanly.",
    4: "Confirm the logo sits flush in the top-left with clean margins and nothing is cropped.",
}


def qa_critique(stage: int, *, pack=None) -> dict:
    """§7.1.5 — advisory only, never auto-regenerates."""
    qa = _resolve_pack(pack).qa
    return {"type": "qa", "state": "proposed", "source": "agent", "stage": stage, "note": qa.get(stage, "")}


def suggest_placement(run: dict, pack=None) -> dict:
    """§ "AI Suggest Placement" — propose a polished, premium arrangement for the
    Stage-3 elements present (does NOT auto-apply; the caller decides).

    Composition rules (deterministic, but content-aware):
    * Text is placed in the NEGATIVE SPACE opposite the Stage-2 subject — subject
      on the left -> copy column on the right (and vice-versa); subject up top ->
      copy drops to the lower band. The subject side comes from the Stage-2
      ``element_placement`` (9-cell); ``auto``/unknown falls back to a wide
      left-aligned band in the upper area with the CTA pinned low, leaving the
      centre open for the subject.
    * Every text element gets a column ``w`` (max width fraction) so the renderer
      wraps inside the safe column — nothing runs off the canvas edge (the old
      arranger let copy overflow).
    * Headline on top, sub-headings stacked with even gaps, CTA anchored low,
      venue/website as a bottom-corner footer.

    Returns ``{"layout": {element_id: {x, y, w, anchor}}}``.
    """
    from .stage3_text import layout as _layout

    ids = {l["id"] for l in _layout.resolve_layers(run)}
    subj = (run.get("config", {}).get("element_placement") or "auto").lower()
    on_left = "left" in subj
    on_right = "right" in subj
    on_top = subj.startswith("top")

    margin = 0.07
    # Copy column on the side OPPOSITE the subject; wide band if unknown.
    if on_left:                      # subject left -> copy on the right
        col_x, col_w = 0.50, round(1 - margin - 0.50, 3)
    elif on_right:                   # subject right -> copy on the left
        col_x, col_w = margin, round(0.50 - margin, 3)
    else:                            # center / auto -> wide left-aligned band
        col_x, col_w = margin, round(1 - 2 * margin, 3)

    # Drop the copy to the lower band when the subject is up top.
    y = 0.40 if on_top else margin
    out: dict[str, dict] = {}

    if "headline" in ids:
        out["headline"] = {"x": col_x, "y": round(y, 3), "w": col_w, "anchor": "tl"}
        y += 0.17
    i = 0
    while f"subheading-{i}" in ids:
        out[f"subheading-{i}"] = {"x": col_x, "y": round(min(y, 0.72), 3), "w": col_w, "anchor": "tl"}
        y += 0.08
        i += 1
    if "cta" in ids:
        out["cta"] = {"x": col_x, "y": 0.88, "w": 0.5, "anchor": "bl"}
    if "venue" in ids:
        out["venue"] = {"x": margin, "y": 0.965, "w": 0.5, "anchor": "bl"}
    if "website" in ids:
        out["website"] = {"x": round(1 - margin, 3), "y": 0.965, "w": 0.5, "anchor": "br"}
    return {"layout": out}
