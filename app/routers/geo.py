"""GEO agent (a10) API — AI answer-engine visibility for a2's brands.

Mounted under ``/api``. Conventions follow the a2 SEO router: any signed-in
user can read and poll; registry-shaping mutations (prompts, config) are
Creator-only; ``CredentialMissing`` surfaces as 503 with the real message
(never fabricated data); unknown brand → 404; bad state → 409/422.
"""
from __future__ import annotations

import datetime as dt
import hmac
import logging
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.security import get_current_user, require_creator
from app.services import run_tracking
from final_geo_agent import (
    geo_compare, geo_engines, geo_history, geo_metrics, geo_poll, geo_prompts,
    geo_strategy, opt_pipeline,
)
from seo_geo_agent import insights
from seo_geo_agent.sources import CredentialMissing

router = APIRouter()
logger = logging.getLogger("agentos.geo")

GEO_AGENT_ID = "a10"
GEO_AGENT_NAME = "GEO"


def _track(user: dict, action: str, task: str, brand: dict | None = None,
           *, usage_action: str = "generate") -> None:
    """Mandatory usage trail → agent_runs__a10 + master runs (admin DB panel)."""
    run_tracking.record_activity(
        user, agent_id=GEO_AGENT_ID, agent_name=GEO_AGENT_NAME, category="seo",
        action=action, task=task,
        brand=(brand or {}).get("name"), brand_id=(brand or {}).get("id"),
        usage_action=usage_action,
    )


# Least wall clock worth handing a brand. Below this a step cannot finish even
# one batch, so the brand is better left due than charged for a partial batch.
MIN_BRAND_SECONDS = 20.0


def _cron_budget_seconds() -> float:
    """Wall clock ONE CRON FIRE may spend in total, across every due brand.

    Env-tunable because the ceiling is a deploy fact — it must stay under the
    service's request timeout — not a code one.
    """
    raw = os.environ.get("GEO_CRON_BUDGET_SECONDS", "")
    try:
        return max(10.0, min(float(raw), 1800.0)) if raw else geo_poll.DEFAULT_CRON_BUDGET_SECONDS
    except ValueError:
        logger.warning("GEO_CRON_BUDGET_SECONDS=%r is not a number — using default", raw)
        return geo_poll.DEFAULT_CRON_BUDGET_SECONDS


def _brand_or_404(brand_id: str) -> dict:
    for brand in insights.list_brands():
        if brand["id"] == brand_id and brand.get("enabled", True):
            return brand
    raise HTTPException(status_code=404, detail="Unknown brand")


class PromptItem(BaseModel):
    id: str
    text: str = Field(min_length=1, max_length=400)
    intent: str = "category"
    stage: str = "consideration"
    enabled: bool = True
    source: str = "ai"          # "ai" | "custom" — custom survives regeneration


class CustomPromptIn(BaseModel):
    text: str = Field(min_length=5, max_length=400)
    intent: str = "category"
    stage: str = "consideration"


class PromptsIn(BaseModel):
    prompts: list[PromptItem]


class ConfigIn(BaseModel):
    # None = leave untouched (same convention as admin settings).
    aliases: dict[str, list[str]] | None = None
    competitors: list[dict] | None = None
    daily_cap: int | None = Field(default=None, ge=10, le=20000)
    # scheduled polling: how many days between sweeps, and whether the cron
    # may run this brand at all
    poll_interval_days: int | None = Field(default=None, ge=1, le=30)
    auto_poll: bool | None = None


class RescanIn(BaseModel):
    days: int = Field(default=7, ge=1, le=30)


class PollIn(BaseModel):
    engines: list[str] | None = None
    runs: int = Field(default=geo_poll.DEFAULT_RUNS, ge=1, le=5)
    batch_size: int = Field(default=geo_poll.DEFAULT_BATCH, ge=1, le=50)


@router.get("/geo/config")
def geo_config(user: dict = Depends(get_current_user)) -> dict:
    return {
        "engines": geo_engines.available_engines(),
        # per-engine mode + model: a chip must never read "Perplexity" when the
        # answer actually came from an OpenRouter stand-in
        "engine_status": geo_engines.engine_status(),
        "default_runs": geo_poll.DEFAULT_RUNS,
        "default_daily_cap": geo_poll.DEFAULT_DAILY_CAP,
    }


@router.get("/geo/brands")
def geo_brands(user: dict = Depends(get_current_user)) -> dict:
    brands = []
    for brand in insights.list_brands():
        if not brand.get("enabled", True):
            continue
        universe = geo_prompts.load_universe(brand["id"])
        cfg = None
        try:
            cfg = geo_poll.ensure_config(brand)
        except Exception:  # noqa: BLE001 — status listing must never 500
            logger.exception("geo: config load failed for %s", brand["id"])
        recent = geo_poll.recent_answers(brand["id"], days=7)
        brands.append(
            {
                "id": brand["id"],
                "name": brand.get("name", brand["id"]),
                "domain": brand.get("domain", ""),
                "prompts": len(universe.get("prompts", [])) if universe else 0,
                "recent_answers": len(recent),
                "calls_used_today": geo_poll.used_today(cfg) if cfg else 0,
                "competitors": len((cfg or {}).get("competitors") or []),
            }
        )
    return {"brands": brands}


@router.get("/geo/brands/{brand_id}/prompts")
def get_prompts(brand_id: str, user: dict = Depends(get_current_user)) -> dict:
    _brand_or_404(brand_id)
    return geo_prompts.load_universe(brand_id) or {"brand_id": brand_id, "prompts": []}


@router.post("/geo/brands/{brand_id}/prompts/generate")
def generate_prompts(brand_id: str, _creator: dict = Depends(require_creator)) -> dict:
    brand = _brand_or_404(brand_id)
    try:
        universe = geo_prompts.generate_universe(brand)
    except CredentialMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _track(_creator, "prompts_generate",
           f"Generated prompt universe — {len(universe.get('prompts', []))} prompts", brand)
    return universe


@router.post("/geo/brands/{brand_id}/prompts/custom")
def add_custom_prompt(
    brand_id: str, body: CustomPromptIn, _creator: dict = Depends(require_creator)
) -> dict:
    brand = _brand_or_404(brand_id)
    try:
        universe = geo_prompts.add_custom_prompt(brand_id, body.text, body.intent, body.stage)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _track(_creator, "prompt_custom_add", f"Custom prompt added: {body.text[:60]}", brand)
    return universe


@router.put("/geo/brands/{brand_id}/prompts")
def put_prompts(
    brand_id: str, body: PromptsIn, _creator: dict = Depends(require_creator)
) -> dict:
    _brand_or_404(brand_id)
    if not body.prompts:
        raise HTTPException(status_code=422, detail="At least one prompt is required")
    return geo_prompts.save_universe(
        brand_id, [p.model_dump() for p in body.prompts]
    )


@router.get("/geo/brands/{brand_id}/config")
def get_geo_brand_config(brand_id: str, user: dict = Depends(get_current_user)) -> dict:
    brand = _brand_or_404(brand_id)
    return geo_poll.ensure_config(brand)


@router.put("/geo/brands/{brand_id}/config")
def put_geo_brand_config(
    brand_id: str, body: ConfigIn, _creator: dict = Depends(require_creator)
) -> dict:
    brand = _brand_or_404(brand_id)
    # Seed the defaults first. ``save_config`` patches whatever document it
    # finds, so a config first written by this endpoint (tracking a competitor
    # before anything has read the config) would exist WITHOUT the brand's own
    # aliases — and a poll against it would then find the brand in none of its
    # own answers.
    geo_poll.ensure_config(brand)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    return geo_poll.save_config(brand_id, patch)


@router.post("/geo/brands/{brand_id}/poll/step")
def poll_step(
    brand_id: str, body: PollIn, user: dict = Depends(get_current_user)
) -> dict:
    brand = _brand_or_404(brand_id)
    try:
        result = geo_poll.poll_step(
            brand, engines=body.engines, runs=body.runs, batch_size=body.batch_size
        )
    except CredentialMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    engines = ", ".join(body.engines) if body.engines else "all engines"
    _track(user, "poll_step", f"AI answer poll ({engines}, batch {body.batch_size})", brand)
    return result


@router.get("/geo/brands/{brand_id}/poll/status")
def poll_status(brand_id: str, user: dict = Depends(get_current_user)) -> dict:
    """Where today's sweep stands and when the next one is due — what the panel
    shows instead of making someone sit through a progress bar."""
    brand = _brand_or_404(brand_id)
    try:
        return geo_poll.poll_status(brand)
    except CredentialMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/geo/brands/{brand_id}/report")
def report(
    brand_id: str, days: int = 7, user: dict = Depends(get_current_user)
) -> dict:
    brand = _brand_or_404(brand_id)
    days = max(1, min(days, 30))
    cfg = geo_poll.ensure_config(brand)
    answers = geo_poll.recent_answers(brand_id, days=days)
    entities = list(geo_poll.alias_map(cfg).keys())
    result = geo_metrics.engine_report(answers, entities, brand.get("domain", ""))
    result |= {
        "brand_id": brand_id,
        "days": days,
        # so an engine whose last measurement fell outside the window can say
        # when it was measured instead of disappearing from the panel
        "engine_last_seen": cfg.get("engine_last_seen") or {},
        "competitor_names": {
            (c.get("key") or c.get("name", "")): c.get("name", "")
            for c in cfg.get("competitors") or []
        },
    }
    return result


@router.get("/geo/brands/{brand_id}/answers")
def answers(
    brand_id: str,
    prompt_id: str | None = None,
    engine: str | None = None,
    days: int = 7,
    user: dict = Depends(get_current_user),
) -> dict:
    _brand_or_404(brand_id)
    days = max(1, min(days, 30))
    rows = geo_poll.recent_answers(brand_id, days=days)
    if prompt_id:
        rows = [a for a in rows if a.get("prompt_id") == prompt_id]
    if engine:
        rows = [a for a in rows if a.get("engine") == engine]
    rows.sort(key=lambda a: a.get("at", ""), reverse=True)
    return {"answers": rows[:200], "total": len(rows)}


@router.get("/geo/brands/{brand_id}/comparison")
def comparison(
    brand_id: str, days: int = 7, user: dict = Depends(get_current_user)
) -> dict:
    """Head-to-head: every tracked rival scored on the same answers we are.

    Asked for in the 18 Aug review — "which companies are cited for the same
    answers as us". Same window, same denominators as ``/report``, so a number
    here and a number there can be read side by side without a footnote.
    """
    brand = _brand_or_404(brand_id)
    days = max(1, min(days, 30))
    cfg = geo_poll.ensure_config(brand)
    answers = geo_poll.recent_answers(brand_id, days=days)
    return geo_compare.build(
        answers, cfg, brand, aliases=geo_poll.alias_map(cfg),
    ) | {"days": days}


@router.post("/geo/brands/{brand_id}/rescan")
def rescan(
    brand_id: str, body: RescanIn, _creator: dict = Depends(require_creator)
) -> dict:
    """Re-read stored answers with the current competitor list.

    A rival added today is invisible until the next sweep, because mentions are
    detected when an answer is stored. The answer text is already on disk, so
    this finds their name in it now — zero engine calls, no new spend. Creator
    only, because it rewrites stored measurements.
    """
    brand = _brand_or_404(brand_id)
    result = geo_poll.rescan_mentions(brand, days=body.days)
    _track(_creator, "rescan_mentions",
           f"Rescanned {result['answers_scanned']} stored answers "
           f"({result['answers_updated']} updated) over {result['days']} days",
           brand, usage_action="edit")
    return result


@router.get("/geo/brands/{brand_id}/history")
def history(
    brand_id: str, days: int = 90, user: dict = Depends(get_current_user)
) -> dict:
    """The GEO score over time — one point per completed sweep.

    Points are banked when a sweep finishes, so this is a read of stored
    history, not a re-derivation of a rolling window. A brand that was already
    polling before the history document existed gets ONE reconstruction from
    its remaining day-docs; the stamp on the document stops that from running
    again on every panel load.
    """
    brand = _brand_or_404(brand_id)
    days = max(7, min(days, 365))
    cfg = geo_poll.ensure_config(brand)

    if geo_history.needs_backfill(brand_id):
        points = geo_history.backfill(
            brand_id,
            geo_poll.answers_by_day(brand_id, days=geo_history.BACKFILL_DAYS),
            list(geo_poll.alias_map(cfg).keys()),
            brand.get("domain", ""),
        )
    else:
        points = geo_history.load_points(brand_id)

    cutoff = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    ).strftime("%Y%m%d")
    windowed = [p for p in points if (p.get("date") or "") >= cutoff]
    return {
        "brand_id": brand_id,
        "days": days,
        "points": windowed,
        "trend": geo_history.trend(windowed),
        "component_labels": geo_history.COMPONENT_LABELS,
        "min_point_answers": geo_history.MIN_POINT_ANSWERS,
        "names": geo_compare.entity_names(cfg, brand),
        # every point older than this is gone for good, so the panel can say
        # why the line starts where it does instead of implying nothing happened
        "backfill_days": geo_history.BACKFILL_DAYS,
    }


# ------------------------- Content Optimizer (Layers 1-6) -------------------------


class OptimizerAnalyzeIn(BaseModel):
    keyword: str = Field(min_length=2, max_length=200)
    locale: str = Field(default="en-US", max_length=10)
    draft: str = Field(default="", max_length=200_000)
    vertical: str | None = None
    own_domain: str = Field(default="", max_length=200)


class OptimizerRescoreIn(BaseModel):
    analysis_id: str = Field(min_length=3, max_length=80)
    draft: str = Field(min_length=1, max_length=200_000)


@router.post("/geo/optimizer/analyze")
def optimizer_analyze(body: OptimizerAnalyzeIn, user: dict = Depends(get_current_user)) -> dict:
    """Fresh SERP snapshot + profiles (+ draft score when a draft is sent).
    Costs one Serper call + ~20 page fetches + embeddings; snapshot is pinned."""
    _track(user, "optimizer_analyze", f"Content Optimizer: {body.keyword}")
    try:
        return opt_pipeline.analyze(
            body.keyword, body.locale, body.draft,
            own_domain=body.own_domain, vertical=body.vertical,
        )
    except CredentialMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/geo/optimizer/rescore")
def optimizer_rescore(body: OptimizerRescoreIn, user: dict = Depends(get_current_user)) -> dict:
    """Re-score an edited draft against the PINNED snapshot — deterministic,
    no new SERP call. Refresh = run analyze again explicitly."""
    _track(user, "optimizer_rescore", f"Re-score draft vs {body.analysis_id}")
    try:
        return opt_pipeline.rescore(body.analysis_id, body.draft)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CredentialMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/geo/optimizer/analyses")
def optimizer_analyses(user: dict = Depends(get_current_user)) -> dict:
    return {"analyses": opt_pipeline.list_analyses()}


@router.get("/geo/optimizer/analyses/{analysis_id}")
def optimizer_analysis(analysis_id: str, user: dict = Depends(get_current_user)) -> dict:
    doc = opt_pipeline.get_analysis(analysis_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Unknown analysis")
    return doc


# ------------------------------- Action Plan (strategy) -------------------------------


class ActionStatusIn(BaseModel):
    status: str = Field(pattern="^(todo|in_progress|done|skipped)$")


@router.post("/geo/brands/{brand_id}/strategy/generate")
def strategy_generate(brand_id: str, _creator: dict = Depends(require_creator)) -> dict:
    """Full-model GEO strategy grounded in this week's measured numbers; the
    plan stores its baseline so the panel can show baseline → current later."""
    brand = _brand_or_404(brand_id)
    try:
        doc = geo_strategy.generate_strategy(brand)
    except CredentialMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _track(_creator, "strategy_generate", "Action Plan generated from measured baseline", brand)
    return doc


@router.get("/geo/brands/{brand_id}/strategy")
def strategy_get(brand_id: str, user: dict = Depends(get_current_user)) -> dict:
    _brand_or_404(brand_id)
    return geo_strategy.load_strategy(brand_id) or {"brand_id": brand_id, "current": None}


@router.put("/geo/brands/{brand_id}/strategy/actions/{action_id}")
def strategy_action_status(
    brand_id: str, action_id: str, body: ActionStatusIn,
    user: dict = Depends(get_current_user),
) -> dict:
    brand = _brand_or_404(brand_id)
    try:
        doc = geo_strategy.set_action_status(brand_id, action_id, body.status)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _track(user, "strategy_action", f"Action {action_id} → {body.status}", brand,
           usage_action="edit")
    return doc


# ------------------------------- cron -------------------------------

@router.post("/geo/cron/poll")
def cron_poll(request: Request, response: Response) -> dict:
    """Scheduled, unattended polling — the path that replaces a half-hour of
    someone holding a browser tab open.

    Fires DAILY; this decides per brand whether a sweep is actually due, from
    ``poll_interval_days`` (default 2) counted off the last *completed* sweep.
    A day-of-month cron step would double-fire across month boundaries and
    would silently skip a brand whose previous sweep never finished.

    The budget is for the WHOLE request, not per brand: Cloud Run kills a
    request at its configured timeout, so a per-brand slice multiplied by an
    unknown number of brands is a config that breaks the day someone adds one.
    Brands are swept stalest-first (never-polled before never-completed before
    oldest-completed) and each takes what is left, so the brand with the oldest
    data always gets the time. A sweep that does not finish leaves its tasks
    pending and stays due, so the next fire resumes where this one stopped --
    nothing is re-billed -- and brands that the clock never reached are
    reported by name rather than silently skipped.

    Status is honest, because Cloud Scheduler only reads the code: 200 every
    due brand swept, 207 some failed, 502 every one failed.
    """
    expected = os.environ.get("GEO_CRON_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="GEO_CRON_KEY not configured")
    if not hmac.compare_digest(request.headers.get("x-cron-key", ""), expected):
        raise HTTPException(status_code=403, detail="Bad cron key")

    budget = _cron_budget_seconds()
    deadline = time.monotonic() + budget
    results: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    unreached: list[str] = []

    due: list[tuple[float, dict]] = []
    for brand in insights.list_brands():
        if not brand.get("enabled", True):
            continue
        try:
            cfg = geo_poll.ensure_config(brand)
        except Exception as exc:  # noqa: BLE001 — unreadable config is not a sweep failure
            skipped[brand["id"]] = f"config unreadable: {exc}"
            continue
        is_due, reason = geo_poll.poll_due(cfg)
        if not is_due:
            skipped[brand["id"]] = reason
            continue
        due.append((geo_poll.staleness_rank(cfg), brand))

    # stalest first, so a tight budget starves the freshest brand, never the one
    # whose data is already oldest
    due.sort(key=lambda pair: pair[0], reverse=True)

    for _, brand in due:
        remaining = deadline - time.monotonic()
        if remaining < MIN_BRAND_SECONDS:
            unreached.append(brand["id"])
            continue
        try:
            results[brand["id"]] = geo_poll.poll_until_done(brand, budget_seconds=remaining)
        except CredentialMissing as exc:
            # no engine key is a configuration fact, not a sweep failure — it
            # must not put the scheduler into a retry loop
            skipped[brand["id"]] = f"not configured: {exc}"
        except Exception as exc:  # noqa: BLE001 — one bad brand must not kill the sweep
            logger.exception("geo cron poll failed for %s", brand["id"])
            results[brand["id"]] = {"ok": False, "error": str(exc)}

    ok = sum(1 for r in results.values() if r.get("ok", True))
    failed = len(results) - ok
    out: dict = {
        "brands": results, "skipped": skipped, "unreached": unreached,
        "ok": ok, "failed": failed, "budget_seconds": budget, "status": "ok",
    }
    if unreached:
        logger.warning(
            "GEO cron ran out of budget before %d brand(s): %s — they stay due for the next fire",
            len(unreached), ", ".join(unreached),
        )
    if results and ok == 0:
        out["status"] = "failed"
        response.status_code = 502
        logger.error("GEO cron poll FAILED: all %d brands errored", failed)
    elif failed:
        out["status"] = "partial"
        response.status_code = 207
        logger.warning("GEO cron poll degraded: %d/%d brands failed", failed, len(results))

    swept = ", ".join(f"{b}: {r.get('done')}/{r.get('total')}" for b, r in results.items())
    _track(run_tracking.CRON_USER, "cron_poll",
           f"Scheduled AI answer poll — {len(results)} swept, {len(skipped)} not due, "
           f"{len(unreached)} out of time"
           + (f" ({swept})" if swept else ""),
           usage_action="session")
    return out
