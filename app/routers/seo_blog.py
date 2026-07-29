"""SEO Blog Writer API — the content team's 12-step process as a 3-gate pipeline.

Mounted under ``/api/seo-blog``. Auth: any signed-in user. Spec:
docs/superpowers/specs/2026-07-30-seo-blog-agent-design.md
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.security import get_current_user
from seo_blog_agent import ahrefs_paste, research, rules, state
from seo_geo_agent.sources import CredentialMissing

router = APIRouter()
logger = logging.getLogger("agentos.seo_blog")

BLOG_AGENT_ID = "a9"  # "SEO Blog Writer" slot in the frontend agent catalog


class RunIn(BaseModel):
    keyword: str
    metrics_paste: str = ""
    competitor_keywords_paste: dict[str, str] = {}


class SheetIn(BaseModel):
    sheet: dict


def _get_run(run_id: str) -> dict:
    run = state.load(f"run-{run_id}")
    if not run:
        raise HTTPException(404, "Run not found")
    return run


def _save_run(run: dict) -> dict:
    state.save(f"run-{run['id']}", run)
    index = state.load("runs-index") or {"runs": []}
    entry = {"id": run["id"], "keyword": run["keyword"], "created": run["created"], "stage": run["stage"]}
    index["runs"] = [entry] + [r for r in index["runs"] if r["id"] != run["id"]]
    state.save("runs-index", index)
    return run


@router.post("/seo-blog/runs")
def kickoff(payload: RunIn, user=Depends(get_current_user)):
    keyword = payload.keyword.strip()
    if not keyword:
        raise HTTPException(422, "keyword is required")
    pasted = {
        "metrics": ahrefs_paste.parse_metrics(payload.metrics_paste),
        "competitor_keywords": {url: ahrefs_paste.parse_competitor_csv(text)
                                for url, text in payload.competitor_keywords_paste.items()},
        "dr": {},
    }
    try:
        sheet = research.build_research(keyword, pasted)
    except CredentialMissing as exc:
        raise HTTPException(503, f"Research data source unavailable: {exc}") from exc
    run_id = hashlib.sha1(f"{keyword.lower()}|{date.today().isoformat()}".encode()).hexdigest()[:10]
    run = {"id": run_id, "keyword": keyword, "created": date.today().isoformat(),
           "stage": "research", "gates": {"keywords": False, "outline": False},
           "pasted": pasted, "sheet": sheet, "outline_doc": None, "citations": None, "draft": None}
    return _save_run(run)


@router.get("/seo-blog/runs")
def list_runs(user=Depends(get_current_user)):
    return {"runs": (state.load("runs-index") or {"runs": []})["runs"]}


@router.get("/seo-blog/runs/{run_id}")
def get_run(run_id: str, user=Depends(get_current_user)):
    return _get_run(run_id)


@router.post("/seo-blog/runs/{run_id}/approve-keywords")
def approve_keywords(run_id: str, payload: SheetIn, user=Depends(get_current_user)):
    run = _get_run(run_id)
    run["sheet"] = payload.sheet
    run["gates"]["keywords"] = True
    run["stage"] = "outline"
    return _save_run(run)
