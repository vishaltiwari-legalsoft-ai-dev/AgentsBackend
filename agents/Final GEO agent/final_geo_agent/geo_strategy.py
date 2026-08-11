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

from final_geo_agent import geo_engines, geo_metrics, geo_poll, geo_prompts

GEO_AGENT_ID = "a10"
HISTORY_CAP = 3
ACTION_STATUSES = ("todo", "in_progress", "done", "skipped")

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
1. EVERY action must be justified by a specific number or fact from the DATA section — name it \
in why_evidence ("cited 123x where brand absent", "named on 9/38 AIO"). No generic SEO advice.
2. Actions must be executable by a small marketing team: concrete deliverable, owner role, \
effort (low/medium/high), expected impact (low/medium/high), timeframe in weeks.
3. Each action names the ONE kpi (from the allowed metric keys) it is meant to move.
4. Be honest about sequencing and expectations: quick retrieval-rail wins first, parametric \
mention-building as the long arc. If the data shows a strength, say how to defend it.
5. Write in plain business English a non-specialist team member can execute from. No jargon \
without a one-line explanation.
6. 3 to 5 pillars, 2 to 4 actions each, 12 actions maximum across the whole plan.

Return STRICT JSON only, no markdown fences, exactly this shape:
{
  "summary": "5-8 sentences: where the brand stands (use the numbers), the core thesis of \
this plan, and what success looks like in 90 days",
  "pillars": [
    {
      "title": "...",
      "objective": "one sentence, outcome-phrased",
      "why_evidence": "the specific measured facts this pillar rests on",
      "actions": [
        {
          "title": "...",
          "detail": "2-4 sentences: exactly what to produce/do and how, specific to THIS brand",
          "owner_role": "content|outreach|founder|ops",
          "effort": "low|medium|high",
          "impact": "low|medium|high",
          "timeframe_weeks": 4,
          "kpi": "one of the allowed metric keys",
          "target": "measurable 90-day target for that kpi, phrased as a number"
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

def collect_baseline(brand: dict) -> dict:
    """Every measured fact the strategist sees — also stored with the plan so
    the panel can show baseline → current for each KPI later."""
    cfg = geo_poll.ensure_config(brand)
    answers = geo_poll.recent_answers(brand["id"], days=7)
    entities = list(geo_poll.alias_map(cfg).keys())
    report = geo_metrics.engine_report(answers, entities, brand.get("domain", ""))

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


def _evidence_prompt(brand: dict, baseline: dict) -> str:
    return (
        f"BRAND: {brand.get('name')} ({brand.get('domain')})\n"
        f"Category seeds: {', '.join(brand.get('seeds') or []) or 'unknown'}\n\n"
        "DATA — everything below is measured this week from real sampled AI answers "
        "(not estimates). Build the plan from THIS:\n"
        + json.dumps(baseline, indent=1)
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


def _clean(raw: dict, baseline: dict) -> dict:
    pillars = []
    for pillar in list(raw.get("pillars") or [])[:5]:
        actions = []
        for action in list(pillar.get("actions") or [])[:4]:
            title = str(action.get("title", "")).strip()
            if not title:
                continue
            actions.append({
                "id": uuid.uuid4().hex[:8],
                "title": title,
                "detail": str(action.get("detail", "")).strip(),
                "owner_role": str(action.get("owner_role", "ops")),
                "effort": action.get("effort") if action.get("effort") in ("low", "medium", "high") else "medium",
                "impact": action.get("impact") if action.get("impact") in ("low", "medium", "high") else "medium",
                "timeframe_weeks": int(action.get("timeframe_weeks") or 4),
                "kpi": str(action.get("kpi", "mention_rate")),
                "target": str(action.get("target", "")),
                "status": "todo",
            })
        if actions:
            pillars.append({
                "title": str(pillar.get("title", "")).strip() or "Workstream",
                "objective": str(pillar.get("objective", "")).strip(),
                "why_evidence": str(pillar.get("why_evidence", "")).strip(),
                "actions": actions,
            })
    if not pillars:
        raise ValueError("Strategy model returned no usable pillars — try again")
    monitoring = raw.get("monitoring") or {}
    return {
        "summary": str(raw.get("summary", "")).strip(),
        "pillars": pillars,
        "monitoring": {
            "cadence": str(monitoring.get("cadence", "")).strip(),
            "review_ritual": str(monitoring.get("review_ritual", "")).strip(),
            "leading_indicators": [str(x) for x in (monitoring.get("leading_indicators") or [])][:4],
        },
        "expectations": str(raw.get("expectations", "")).strip(),
        "baseline": baseline,
        "generated_at": _now(),
    }


def generate_strategy(brand: dict) -> dict:
    """Measured data → executable plan, persisted with its baseline scoreboard."""
    baseline = collect_baseline(brand)
    if baseline["n_answers"] < 20:
        raise ValueError(
            "Not enough measured answers yet — run a full poll first so the plan "
            "rests on real data, not guesses"
        )
    raw = _llm_strategy(STRATEGIST_SYSTEM, _evidence_prompt(brand, baseline))
    strategy = _clean(raw, baseline)

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


def load_strategy(brand_id: str) -> dict | None:
    return state.load(strategy_doc_id(brand_id))


def set_action_status(brand_id: str, action_id: str, status: str) -> dict:
    if status not in ACTION_STATUSES:
        raise ValueError(f"status must be one of {ACTION_STATUSES}")
    doc = load_strategy(brand_id)
    if not doc or not doc.get("current"):
        raise KeyError("No strategy generated yet")
    for pillar in doc["current"]["pillars"]:
        for action in pillar["actions"]:
            if action["id"] == action_id:
                action["status"] = status
                action["status_at"] = _now()
                state.save(strategy_doc_id(brand_id), doc)
                return doc
    raise KeyError(f"Unknown action: {action_id}")
