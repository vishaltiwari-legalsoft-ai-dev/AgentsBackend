"""SEO Blog Writer API — the content team's 12-step process as a 3-gate pipeline.

Mounted under ``/api/seo-blog``. Auth: any signed-in user. Spec:
docs/superpowers/specs/2026-07-30-seo-blog-agent-design.md
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.security import get_current_user
from seo_blog_agent import ahrefs_paste, citations, drafting, outline, research, rules, site_pool, state
from seo_geo_agent.sources import CredentialMissing

router = APIRouter()
logger = logging.getLogger("agentos.seo_blog")

BLOG_AGENT_ID = "a9"  # "SEO Blog Writer" slot in the frontend agent catalog


class RunIn(BaseModel):
    keyword: str
    metrics_paste: str = ""
    competitor_keywords_paste: dict[str, str] = {}
    website: str = ""


class SiteIn(BaseModel):
    website: str


class SheetIn(BaseModel):
    sheet: dict


class DrIn(BaseModel):
    dr_paste: str


class OutlineIn(BaseModel):
    outline: list[dict]


class DraftPatch(BaseModel):
    markdown: str


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
    run_id = hashlib.sha1(f"{keyword.lower()}|{date.today().isoformat()}".encode()).hexdigest()[:10]
    existing = state.load(f"run-{run_id}")
    if existing and (existing["gates"]["keywords"] or existing["gates"]["outline"]):
        raise HTTPException(409, "A run for this keyword already exists today with approved progress — open it from the runs list or use a different keyword")
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
    run = {"id": run_id, "keyword": keyword, "created": date.today().isoformat(),
           "stage": "research", "gates": {"keywords": False, "outline": False},
           "pasted": pasted, "sheet": sheet, "outline_doc": None, "citations": None, "draft": None}
    run["site"] = None
    if payload.website.strip():
        profile = site_pool.load_site(payload.website)
        if profile:
            run["site"] = {"domain": profile["domain"],
                           "cannibalization": site_pool.cannibalization(profile, keyword),
                           "internal_links": site_pool.internal_links(profile, keyword)}
            run["sheet"]["internal_links"] = run["site"]["internal_links"]
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


@router.post("/seo-blog/runs/{run_id}/build-outline")
def build_outline(run_id: str, user=Depends(get_current_user)):
    run = _get_run(run_id)
    if not run["gates"]["keywords"]:
        raise HTTPException(409, "Approve the keyword sheet first (Gate 1)")
    try:
        profiles = [outline.competitor_profile(t["url"]) for t in run["sheet"]["serp"]["top3"]]
        run["outline_doc"] = outline.build_outline(run["sheet"], profiles)
        run["citations"] = citations.source_citations(run["outline_doc"], run["pasted"]["dr"])
    except CredentialMissing as exc:
        raise HTTPException(503, f"Outline data source unavailable: {exc}") from exc
    return _save_run(run)


@router.post("/seo-blog/runs/{run_id}/vet-citations")
def vet_citations(run_id: str, payload: DrIn, user=Depends(get_current_user)):
    run = _get_run(run_id)
    if not run.get("citations"):
        raise HTTPException(409, "Build the outline first")
    run["pasted"]["dr"] = {**run["pasted"]["dr"], **ahrefs_paste.parse_dr(payload.dr_paste)}
    run["citations"] = citations.revet(run["citations"], run["pasted"]["dr"],
                                       run["outline_doc"]["targets"]["links"])
    return _save_run(run)


@router.post("/seo-blog/runs/{run_id}/approve-outline")
def approve_outline(run_id: str, payload: OutlineIn, user=Depends(get_current_user)):
    run = _get_run(run_id)
    if not run.get("outline_doc"):
        raise HTTPException(409, "Build the outline first")
    run["outline_doc"]["outline"] = payload.outline
    run["gates"]["outline"] = True
    run["stage"] = "draft"
    return _save_run(run)


@router.post("/seo-blog/runs/{run_id}/draft")
def make_draft(run_id: str, user=Depends(get_current_user)):
    run = _get_run(run_id)
    if not (run["gates"]["keywords"] and run["gates"]["outline"]):
        raise HTTPException(409, "Approve keywords and outline first")
    try:
        run["draft"] = drafting.build_draft(run["sheet"], run["outline_doc"], run["citations"])
    except CredentialMissing as exc:
        raise HTTPException(503, f"Draft generation unavailable: {exc}") from exc
    return _save_run(run)


@router.patch("/seo-blog/runs/{run_id}/draft")
def edit_draft(run_id: str, payload: DraftPatch, user=Depends(get_current_user)):
    run = _get_run(run_id)
    if not run.get("draft"):
        raise HTTPException(409, "No draft to edit yet")
    run["draft"]["markdown"] = payload.markdown
    run["draft"]["edited"] = True
    run["draft"]["compliance"] = drafting.check_compliance(
        payload.markdown, run["sheet"], run["outline_doc"], run["citations"])
    return _save_run(run)


@router.get("/seo-blog/runs/{run_id}/export")
def export_draft(run_id: str, format: str = "md", user=Depends(get_current_user)):
    run = _get_run(run_id)
    if not run.get("draft"):
        raise HTTPException(404, "No draft yet")
    slug = run["outline_doc"]["meta"]["slug"] or run["id"]
    if format == "docx":
        return Response(
            content=drafting.to_docx(run["draft"]["markdown"]),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{slug}.docx"'})
    return Response(content=run["draft"]["markdown"], media_type="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{slug}.md"'})


@router.get("/seo-blog/sites")
def list_sites(user=Depends(get_current_user)):
    return {"sites": site_pool.list_sites()}


@router.post("/seo-blog/sites")
def scan_site(payload: SiteIn, user=Depends(get_current_user)):
    website = payload.website.strip()
    if not website:
        raise HTTPException(422, "website is required")
    try:
        return site_pool.scan_site(website)
    except CredentialMissing as exc:
        raise HTTPException(503, f"Site scan unavailable: {exc}") from exc


@router.get("/seo-blog/sites/{domain}")
def site_detail(domain: str, user=Depends(get_current_user)):
    profile = site_pool.load_site(domain)
    if not profile:
        raise HTTPException(404, "Site not scanned yet")
    return profile


@router.post("/seo-blog/sites/{domain}/topics")
def site_topics(domain: str, user=Depends(get_current_user)):
    profile = site_pool.load_site(domain)
    if not profile:
        raise HTTPException(404, "Site not scanned yet")
    try:
        return site_pool.suggest_topics(profile)
    except CredentialMissing as exc:  # defensive; suggest_topics degrades internally
        raise HTTPException(503, f"Topic suggestion unavailable: {exc}") from exc
