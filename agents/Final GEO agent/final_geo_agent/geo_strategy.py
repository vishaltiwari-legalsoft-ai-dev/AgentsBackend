"""GEO agent — the Action Plan: measured data in, executable strategy out.

Insights say WHERE the brand stands; this module produces WHAT TO DO about
it. The strategy is drafted by the full (non-fast) model against a detailed
GEO-specialist brief, grounded in the brand's actual measured numbers — and
every generated plan stores its baseline metrics, so the panel can show
baseline → current → target for every KPI as later polls come in. That
delta view IS the monitoring: the plan carries its own scoreboard.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid

from seo_geo_agent import state
from seo_geo_agent.sources import CredentialMissing

from final_geo_agent import (
    geo_engines, geo_prompts, geo_venues, geo_window,
)

GEO_AGENT_ID = "a10"
HISTORY_CAP = 3
ACTION_STATUSES = ("todo", "in_progress", "done", "skipped")
# The plan is grounded in "this week's" measured numbers, and the baseline it
# stores is what the panel later compares against — so the window is a property
# of the plan, not a caller's choice.
BASELINE_DAYS = geo_window.DEFAULT_DAYS

# ----------------------------------------------------------- the specialist

STRATEGIST_SYSTEM = """You are a senior Generative Engine Optimization (GEO) strategist. You build \
execution-grade action plans for brands that want to be named and cited inside AI answers \
(ChatGPT, Gemini, Perplexity, Google AI Overviews). You have run this playbook across many \
brands and you know what the research actually supports.

DOCTRINE you operate under (do not restate it to the user; apply it):
- Two rails decide visibility. RETRIEVAL rail (live search behind AI answers): moves in 4-12 \
weeks via extractable, answer-shaped content, freshness, and presence on the third-party pages \
engines already cite. PARAMETRIC rail (what models "know"): moves in quarters via volume and \
consistency of third-party brand mentions (reviews, listicles, Reddit, YouTube, press).
- Third-party mentions predict AI visibility far better than backlinks or domain authority. \
The single highest-ROI action is getting the brand into the specific third-party pages the \
engines are ALREADY citing on the brand's own tracked questions (the source-gap list).
- Answer-shaped content gets cited: a question-form heading followed by a direct 40-80 word \
answer, concrete numbers, tables/steps, visible dateModified. Listicle-format pages take a \
disproportionate share of AI citations for commercial-intent questions.
- Genuine expert participation works on Reddit; astroturfing gets brands excluded.
- FORBIDDEN (debunked or harmful — never recommend): llms.txt as a visibility lever, schema \
markup as a citation lever, keyword stuffing, hidden prompt injection, fake or incentivized \
reviews, Reddit astroturfing, buying backlinks for GEO.

RULES for the plan you write:
1. EVERY action must be justified by a specific number or fact from the DATA section - name it \
in why_evidence ("cited 123x where brand absent", "named on 9/38 AIO"). No generic SEO advice.
2. NAME THE PLACE. Every off-site action must set "venue" to a name copied EXACTLY from the \
VENUES list. You may not invent, guess or "recall" a subreddit, forum, publication or channel \
that is not in that list - a plan whose first step is a dead link is worse than no plan, and \
anything you invent is dropped before the user ever sees it. On-site work uses venue "" and \
names the URL path in the deliverable instead.
3. Each action states a DELIVERABLE: the artefact that exists once it is done ("a 600-word \
answer post", "the completed G2 profile", "an outreach email to the editor of X"), never an \
activity ("engage with the community"). If a marketer cannot start it on Monday morning from \
what you wrote, rewrite it.
4. Actions must be executable by a small marketing team: owner role, effort (low/medium/high), \
expected impact (low/medium/high).
5. Each action names the ONE kpi (from the allowed metric keys) it is meant to move.
6. Group the actions into 3-4 WAVES by calendar time: weeks 1-2 first, then 3-4, then 5-8, then \
9-12 if needed. Cheap retrieval-rail wins (technical fixes, answer-shaped content, review \
profiles) belong in the earliest waves; parametric mention-building (communities, editorial \
placement, video) follows. A wave is what one small team can finish in that fortnight - 2 to 4 \
actions, never more.
7. Respect each venue's culture. Reddit and forums reject promotion: the deliverable there is a \
genuinely useful expert answer that happens to come from us, never a pitch. Say so in the action \
itself.
8. Write in plain business English a non-specialist can execute from. No jargon without a \
one-line explanation.
9. 12 actions maximum across the whole plan.

Return STRICT JSON only, no markdown fences, exactly this shape:
{
  "summary": "5-8 sentences: where the brand stands (use the numbers), the core thesis of \
this plan, and what success looks like in 90 days",
  "waves": [
    {
      "weeks": "1-2",
      "title": "short name for what this fortnight is about",
      "objective": "one sentence, outcome-phrased",
      "why_evidence": "the specific measured facts this wave rests on",
      "actions": [
        {
          "title": "...",
          "venue": "EXACT name from the VENUES list, or empty string for our own site",
          "deliverable": "the artefact that exists once this is done",
          "detail": "2-4 sentences: exactly how to produce it, specific to THIS brand \
and THIS venue, including whatever the venue's rules require",
          "owner_role": "content|outreach|founder|ops",
          "effort": "low|medium|high",
          "impact": "low|medium|high",
          "kpi": "one of the allowed metric keys",
          "target": "measurable 90-day target for that kpi, phrased as a number",
          "why_evidence": "the measured fact that makes this worth doing"
        }
      ]
    }
  ],
  "monitoring": {
    "cadence": "what to re-measure and how often, referencing the weekly poll and monthly AIO snapshot",
    "review_ritual": "a 15-minute weekly team ritual: which tab to open, which 3 numbers to read, when to escalate",
    "leading_indicators": ["2-4 early signals that the plan is working before the headline rate moves"]
  },
  "expectations": "honest 2-4 sentences: what moves in 4-12 weeks vs what takes quarters, and \
that AI answers are sampled so week-to-week wiggle is noise while the trend is signal"
}

Allowed metric keys for "kpi": mention_rate, citation_rate, sov_self, aio_named_rate, \
aio_cited_rate, source_gap_top_count, missing_questions_count."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def strategy_doc_id(brand_id: str) -> str:
    return f"geo-strategy-{brand_id}"


# ------------------------------------------------------------- the evidence

def collect_baseline(window: geo_window.MeasuredWindow) -> dict:
    """Every measured fact the strategist sees — also stored with the plan so
    the panel can show baseline → current for each KPI later.

    Takes the WINDOW, not the brand. It used to open its own, and its only
    caller then opened a second one over the identical days and recomputed the
    identical report: one ``POST /strategy/generate`` was 56 day-doc fetches and
    two full ``engine_report`` runs over the same answers. Asking for the window
    the caller already has makes paying twice unspellable.
    """
    brand = window.brand
    cfg = window.cfg
    answers = window.answers
    report = window.report

    rollup = report.get("prompt_rollup", [])
    missing = [r for r in rollup if r["self_rate"] == 0]
    missing_with_rivals = [r for r in missing if r["rivals"]]
    winning = [r for r in rollup if r["self_rate"] > 0]

    aio = [a for a in answers if a.get("engine") == geo_engines.AIO_ENGINE and not a.get("error")]
    aio_shown = [a for a in aio if not a.get("no_aio")]
    aio_named = [a for a in aio_shown if a.get("brand_mentioned")]
    aio_cited = [a for a in aio_shown if a.get("brand_cited")]

    blended = report["blended"]
    return {
        "measured_at": _now(),
        "n_answers": blended["mention"]["n_answers"],
        "prompt_count": len(geo_prompts.enabled_prompts(brand["id"])),
        "mention_rate": blended["mention"]["rate"] or 0.0,
        "citation_rate": blended["citation"]["rate"] or 0.0,
        "sov_self": blended["sov"]["share"].get("self") or 0.0,
        "per_engine": {
            e: {"mention_rate": b["mention"]["rate"] or 0.0, "n": b["n_answers"]}
            for e, b in report["engines"].items()
        },
        "competitors_tracked": [c.get("name", "") for c in cfg.get("competitors") or []],
        "competitor_rates": {
            k: (v["rate"] or 0.0) for k, v in report["competitors"].items()
        },
        "source_gaps": [
            {"domain": g["domain"], "count": g["count"]} for g in report["source_gap"][:8]
        ],
        "source_gap_top_count": report["source_gap"][0]["count"] if report["source_gap"] else 0,
        "top_cited_sources": [s["domain"] for s in blended["source_mix"][:8]],
        "winning_questions": [
            {"text": r["text"], "self_rate": r["self_rate"], "intent": r["intent"]}
            for r in sorted(winning, key=lambda r: -r["self_rate"])[:8]
        ],
        "missing_questions": [
            {"text": r["text"], "rivals": [x["key"] for x in r["rivals"]], "intent": r["intent"]}
            for r in missing_with_rivals[:10]
        ] + [
            {"text": r["text"], "rivals": [], "intent": r["intent"]}
            for r in missing if not r["rivals"]
        ][:5],
        "missing_questions_count": len(missing),
        "winning_questions_count": len(winning),
        "aio_snapshot": {
            "queries_checked": len(aio),
            "aio_shown": len(aio_shown),
            "open_slots": len(aio) - len(aio_shown),
            "brand_named": len(aio_named),
            "brand_cited": len(aio_cited),
        },
        "aio_named_rate": (len(aio_named) / len(aio_shown)) if aio_shown else 0.0,
        "aio_cited_rate": (len(aio_cited) / len(aio_shown)) if aio_shown else 0.0,
    }


def _venue_brief(discovery: dict) -> str:
    """The venue list, formatted for the model that must copy names out of it.

    Deliberately verbose about provenance: a venue the engines already cite on
    our own questions is a different quality of target from one that merely
    turned up in a search, and the strategist should sequence accordingly.
    """
    venues = discovery.get("venues") or []
    if not venues:
        return (
            "\nVENUES: none discovered (live search unavailable). Do NOT name any "
            "specific subreddit, forum, publication or channel — you have no verified "
            "list to copy from. Restrict this plan to work on our own site and to "
            "actions that name no external venue.\n"
        )
    lines = []
    for venue in venues[:30]:
        bits = [f"- {venue['name']} [{venue['kind']}] {venue['url']}"]
        if venue.get("cited_where_absent"):
            bits.append(
                f"cited {venue['cited_where_absent']}x by engines on our questions "
                "in answers where we were ABSENT"
            )
        if venue.get("brand_present") is False:
            bits.append("brand has NO profile here")
        for example in (venue.get("examples") or [])[:2]:
            bits.append(f"e.g. \"{example.get('title', '')}\" {example.get('url', '')}")
        lines.append(" | ".join(bits))
    caveat = "" if discovery.get("complete") else (
        "\n(This list is PARTIAL — some discovery searches failed. Work with what is "
        "here; do not fill the gaps from memory.)"
    )
    return (
        f"\nVENUES — real places found for \"{discovery.get('category', '')}\", by live "
        "search and by our own citation data. The \"venue\" field of every off-site "
        "action MUST be one of these names, copied exactly:\n"
        + "\n".join(lines) + caveat + "\n"
    )


def _evidence_prompt(brand: dict, baseline: dict, discovery: dict) -> str:
    return (
        f"BRAND: {brand.get('name')} ({brand.get('domain')})\n"
        f"Category seeds: {', '.join(brand.get('seeds') or []) or 'unknown'}\n\n"
        "DATA — everything below is measured this week from real sampled AI answers "
        "(not estimates). Build the plan from THIS:\n"
        + json.dumps(baseline, indent=1)
        + _venue_brief(discovery)
        + "\n\nNotes on reading the data:\n"
        "- source_gaps = third-party domains engines cited on our tracked questions in answers "
        "where the brand was ABSENT (count = how often). These are the highest-ROI placement targets.\n"
        "- missing_questions = tracked buyer questions where the brand was never named; 'rivals' "
        "lists who was named instead.\n"
        "- aio_snapshot = Google AI Overview: on how many queries an Overview appeared and how "
        "often the brand was named in it vs cited (linked) by it.\n"
        "- If competitors_tracked is empty, the share-of-voice number is meaningless — say so "
        "and make tracking named rivals an early action.\n\n"
        "Write the strategy JSON now."
    )


# ------------------------------------------------------------ generate/load

def _llm_strategy(system: str, prompt: str) -> dict:
    """Full-model call (strategy deserves the big model, not the fast one).
    Raises CredentialMissing on any failure — honest 503, never a canned plan."""
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    try:
        from app.services.openrouter import get_llm

        raw = get_llm(temperature=0.4, fast=False, agent_id=GEO_AGENT_ID).invoke(
            [("system", system), ("user", prompt)]
        ).content
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip())
        return json.loads(text)
    except CredentialMissing:
        raise
    except Exception as exc:  # noqa: BLE001 — no key, bad JSON, provider down
        raise CredentialMissing(f"Strategy model unavailable: {exc}") from exc


ACTION_CAP = 12
WAVE_CAP = 4
ACTIONS_PER_WAVE = 4


def _wave_order(weeks: str) -> int:
    """Sort key from a "1-2" / "5-8" label. A wave whose label we cannot read
    goes last rather than silently jumping the queue."""
    digits = re.match(r"\s*(\d+)", str(weeks or ""))
    return int(digits.group(1)) if digits else 999


def _clean_action(raw: dict, discovery: dict, dropped: list[dict]) -> dict | None:
    """One action, or None if it should never reach the user.

    The venue check is the point of this function. The strategist is handed a
    discovered venue list and told to copy names from it; a name outside that
    list means the model produced a place from memory, and a plan whose first
    step is a subreddit that does not exist destroys trust in the whole panel.
    Those actions are dropped and recorded, never quietly rewritten.
    """
    title = str(raw.get("title", "")).strip()
    if not title:
        return None
    venue_name = str(raw.get("venue", "")).strip()
    venue = geo_venues.venue_by_name(discovery, venue_name) if venue_name else None
    if venue_name and venue is None:
        dropped.append({"title": title, "venue": venue_name, "reason": "venue not in discovered list"})
        return None
    return {
        "id": uuid.uuid4().hex[:8],
        "title": title,
        # the venue is stored resolved, so the panel links the real URL rather
        # than whatever the model typed
        "venue": {
            "name": venue["name"], "url": venue["url"], "kind": venue["kind"],
            "cited_where_absent": venue.get("cited_where_absent", 0),
            "examples": (venue.get("examples") or [])[:2],
        } if venue else None,
        "deliverable": str(raw.get("deliverable", "")).strip(),
        # a list a team can tick off, not a paragraph they have to parse
        "steps": [
            step for step in
            (str(x).strip() for x in (raw.get("steps") or [])[:4])
            if step
        ],
        "detail": str(raw.get("detail", "")).strip(),
        "owner_role": str(raw.get("owner_role", "ops")),
        "effort": raw.get("effort") if raw.get("effort") in ("low", "medium", "high") else "medium",
        "impact": raw.get("impact") if raw.get("impact") in ("low", "medium", "high") else "medium",
        "kpi": str(raw.get("kpi", "mention_rate")),
        "target": str(raw.get("target", "")),
        "why_evidence": str(raw.get("why_evidence", "")).strip(),
        "status": "todo",
    }


def _clean(raw: dict, baseline: dict, discovery: dict) -> dict:
    """Model output → the plan we are willing to show, with what we refused.

    ``dropped_actions`` is kept and surfaced rather than swallowed: a plan that
    silently shrank is indistinguishable from a plan the model wrote short, and
    only one of those is worth investigating.
    """
    dropped: list[dict] = []
    waves = []
    budget = ACTION_CAP
    for wave in sorted(list(raw.get("waves") or [])[:WAVE_CAP],
                       key=lambda w: _wave_order(w.get("weeks"))):
        actions = []
        for item in list(wave.get("actions") or [])[:ACTIONS_PER_WAVE]:
            if budget <= 0:
                break
            action = _clean_action(item, discovery, dropped)
            if action:
                actions.append(action)
                budget -= 1
        if actions:
            waves.append({
                "weeks": str(wave.get("weeks", "")).strip() or "1-2",
                "title": str(wave.get("title", "")).strip() or "Workstream",
                "objective": str(wave.get("objective", "")).strip(),
                "why_evidence": str(wave.get("why_evidence", "")).strip(),
                "actions": actions,
            })
    if not waves:
        raise ValueError(
            "Strategy model returned no usable actions"
            + (f" — {len(dropped)} named venues we could not verify" if dropped else "")
            + " — try again"
        )
    monitoring = raw.get("monitoring") or {}
    return {
        "summary": str(raw.get("summary", "")).strip(),
        "waves": waves,
        "monitoring": {
            "cadence": str(monitoring.get("cadence", "")).strip(),
            "review_ritual": str(monitoring.get("review_ritual", "")).strip(),
            "leading_indicators": [str(x) for x in (monitoring.get("leading_indicators") or [])][:4],
        },
        "expectations": str(raw.get("expectations", "")).strip(),
        "baseline": baseline,
        "venues": {
            "category": discovery.get("category", ""),
            "counts": discovery.get("counts", {}),
            "searched": discovery.get("searched", 0),
            "complete": discovery.get("complete", False),
            "errors": discovery.get("errors", []),
        },
        "dropped_actions": dropped,
        "generated_at": _now(),
    }


def generate_strategy(brand: dict) -> dict:
    """Measured data → executable plan, persisted with its baseline scoreboard.

    ONE window per request: the baseline, the ``n_answers`` gate and the venue
    discovery all read the same measured week, fetched and scored once.
    """
    window = geo_window.open_window(brand, BASELINE_DAYS)
    baseline = collect_baseline(window)
    if baseline["n_answers"] < 20:
        raise ValueError(
            "Not enough measured answers yet — run a full poll first so the plan "
            "rests on real data, not guesses"
        )
    prompts = geo_prompts.enabled_prompts(brand["id"])
    # real places, from live search plus the domains our own polls show the
    # engines citing — never from the model's memory
    discovery = geo_venues.discover(brand, prompts, window.report)
    raw = _llm_strategy(STRATEGIST_SYSTEM, _evidence_prompt(brand, baseline, discovery))
    strategy = _clean(raw, baseline, discovery)

    doc = state.load(strategy_doc_id(brand["id"])) or {"brand_id": brand["id"], "history": []}
    if doc.get("current"):
        doc.setdefault("history", []).insert(0, {
            "generated_at": doc["current"].get("generated_at"),
            "summary": doc["current"].get("summary", "")[:400],
        })
        doc["history"] = doc["history"][:HISTORY_CAP]
    doc["current"] = strategy
    state.save(strategy_doc_id(brand["id"]), doc)
    return doc


def _as_waves(current: dict) -> dict:
    """Bring a stored plan up to the wave shape the panel reads.

    Plans written before waves stored ``pillars``, and a panel that indexed
    ``waves`` on one of those took the whole console down with a client-side
    exception rather than showing the plan it already had. Converting on read
    keeps every plan a team has already worked through visible and its action
    statuses editable.

    One pillar becomes one wave. Its ``weeks`` label comes from the longest
    ``timeframe_weeks`` its actions carried, because that is the only calendar
    information those plans ever held -- inventing a fortnight would be worse
    than a wide one.
    """
    if current.get("waves") or not current.get("pillars"):
        return current
    waves = []
    for pillar in current.get("pillars") or []:
        actions = [
            {"venue": None, "deliverable": "", "steps": [], "why_evidence": "", **action}
            for action in pillar.get("actions") or []
        ]
        span = max((int(a.get("timeframe_weeks") or 0) for a in actions), default=0)
        waves.append({
            "weeks": f"1-{span}" if span else "1-4",
            "title": pillar.get("title", "Workstream"),
            "objective": pillar.get("objective", ""),
            "why_evidence": pillar.get("why_evidence", ""),
            "actions": actions,
            "span": span,
        })
    waves.sort(key=lambda w: w.pop("span") or 99)
    return {**current, "waves": waves, "shape": "migrated-from-pillars"}


def load_strategy(brand_id: str) -> dict | None:
    doc = state.load(strategy_doc_id(brand_id))
    if doc and doc.get("current"):
        doc = {**doc, "current": _as_waves(doc["current"])}
    return doc


def set_action_status(brand_id: str, action_id: str, status: str) -> dict:
    if status not in ACTION_STATUSES:
        raise ValueError(f"status must be one of {ACTION_STATUSES}")
    doc = load_strategy(brand_id)      # normalises an old pillars-shaped plan
    if not doc or not doc.get("current"):
        raise KeyError("No strategy generated yet")
    doc["current"].pop("pillars", None)   # the write below persists the wave shape
    for wave in doc["current"].get("waves") or []:
        for action in wave["actions"]:
            if action["id"] == action_id:
                action["status"] = status
                action["status_at"] = _now()
                state.save(strategy_doc_id(brand_id), doc)
                return doc
    raise KeyError(f"Unknown action: {action_id}")
