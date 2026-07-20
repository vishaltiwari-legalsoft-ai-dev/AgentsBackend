"""SEO + GEO agent API (card a2). Endpoints under ``/api/seo``.

P1: benchmarks (analyze once, paid) + /score (pure-function hot path, free).
P2: GEO runs + overview. Config: scoring weights, brands, question sets.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.security import get_current_user

from seo_agent import store
from seo_agent.analyze import AnalysisError, run_analysis
from seo_agent.briefs import build_brief
from seo_agent.config import effective_config
from seo_agent.geo.runner import run_geo_capture
from seo_agent.schemas import Benchmark
from seo_agent.scoring import score_draft

router = APIRouter()
logger = logging.getLogger("agentos.seo")

SEO_AGENT_ID = "a2"


class AnalyzeBody(BaseModel):
    keyword: str
    location: str = ""
    brand: str = ""


class ScoreBody(BaseModel):
    benchmark_id: str
    draft_text: str


def _benchmark_or_404(bid: str) -> Benchmark:
    doc = store.get_benchmark(bid)
    if doc is None:
        raise HTTPException(404, "Benchmark not found")
    return Benchmark.model_validate(doc)


@router.post("/seo/benchmarks")
def analyze(body: AnalyzeBody, user=Depends(get_current_user)):
    """Run the paid SERP→crawl→terms→topics pipeline. Synchronous in slice 1."""
    try:
        benchmark = run_analysis(body.keyword.strip(), body.location.strip(), body.brand.strip())
    except AnalysisError as exc:
        raise HTTPException(502, str(exc))
    return benchmark.model_dump()


@router.get("/seo/benchmarks")
def benchmarks_list(user=Depends(get_current_user)):
    return {"benchmarks": store.list_benchmarks()}


@router.get("/seo/benchmarks/{bid}")
def benchmark_detail(bid: str, user=Depends(get_current_user)):
    return _benchmark_or_404(bid).model_dump()


@router.post("/seo/benchmarks/{bid}/refresh")
def benchmark_refresh(bid: str, user=Depends(get_current_user)):
    old = _benchmark_or_404(bid)
    try:
        benchmark = run_analysis(old.keyword, old.location, old.brand)
    except AnalysisError as exc:
        raise HTTPException(502, str(exc))
    return benchmark.model_dump()


@router.post("/seo/score")
def score(body: ScoreBody, user=Depends(get_current_user)):
    """The live hot path: pure function, no LLM/SERP calls."""
    benchmark = _benchmark_or_404(body.benchmark_id)
    cfg = effective_config(store.load_config())
    return score_draft(body.draft_text, None, benchmark, cfg).model_dump()


@router.get("/seo/briefs/{bid}")
def brief(bid: str, user=Depends(get_current_user)):
    return build_brief(_benchmark_or_404(bid))


def _cron_authorized(request: Request) -> bool:
    """Header-guarded scheduler access (MR pattern): X-Cron-Key must match env."""
    expected = os.environ.get("SEO_CRON_KEY", "")
    provided = request.headers.get("X-Cron-Key", "")
    return bool(expected) and hmac.compare_digest(expected, provided)


@router.post("/seo/geo/capture")
def geo_capture(request: Request):
    """Weekly GEO run — Cloud Scheduler target AND the UI 'Run now' button.

    ``get_current_user`` is a FastAPI dependency: it takes parsed bearer
    credentials, not a ``Request``, so it can't be called with ``request``
    directly. For the interactive fallback we parse the Authorization header
    ourselves (mirroring what the ``HTTPBearer`` dependency does) and invoke
    ``get_current_user`` as a plain function with the resulting credentials —
    it still raises 401 for a missing/invalid/expired session, same as when
    used via ``Depends``.
    """
    if not _cron_authorized(request):
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        credentials = (
            HTTPAuthorizationCredentials(scheme=scheme, credentials=token)
            if scheme.lower() == "bearer" and token
            else None
        )
        get_current_user(credentials)  # raises 401 when neither cron key nor session is valid
    runs = run_geo_capture()
    return {"runs": [r.model_dump() for r in runs]}


@router.get("/seo/geo/overview")
def geo_overview(user=Depends(get_current_user)):
    brands: dict[str, dict] = {}
    for run in store.list_geo_runs():          # newest first
        slug = run.get("brand", "")
        entry = brands.setdefault(slug, {"brand": slug, "latest": None, "history": []})
        if entry["latest"] is None:
            entry["latest"] = run
        entry["history"].append({"week": run.get("week"), "score": run.get("score"),
                                 "id": run.get("id")})
    return {"brands": list(brands.values())}


@router.get("/seo/geo/runs/{rid}")
def geo_run_detail(rid: str, user=Depends(get_current_user)):
    run = store.get_geo_run(rid)
    if run is None:
        raise HTTPException(404, "GEO run not found")
    return run


@router.get("/seo/config")
def config_get(user=Depends(get_current_user)):
    return effective_config(store.load_config())


@router.put("/seo/config")
def config_put(overrides: dict, user=Depends(get_current_user)):
    store.save_config(overrides)
    return effective_config(store.load_config())
