"""GEO agent (a10) API — AI answer-engine visibility for a2's brands.

Mounted under ``/api``. Conventions follow the a2 SEO router: any signed-in
user can read and poll; registry-shaping mutations (prompts, config) are
Creator-only; ``CredentialMissing`` surfaces as 503 with the real message
(never fabricated data); unknown brand → 404; bad state → 409/422.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.security import get_current_user, require_creator
from app.services import run_tracking
from final_geo_agent import geo_engines, geo_metrics, geo_poll, geo_prompts, geo_strategy, opt_pipeline
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


class PollIn(BaseModel):
    engines: list[str] | None = None
    runs: int = Field(default=geo_poll.DEFAULT_RUNS, ge=1, le=5)
    batch_size: int = Field(default=geo_poll.DEFAULT_BATCH, ge=1, le=50)


@router.get("/geo/config")
def geo_config(user: dict = Depends(get_current_user)) -> dict:
    return {
        "engines": geo_engines.available_engines(),
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
    _brand_or_404(brand_id)
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
