"""SEO agent (a2) API — brands, insights, traffic-estimated to-dos, blog topics.

Mounted under ``/api/seo-geo``. Auth: any signed-in user reads and runs; only a
Creator edits the brand registry; the cron entry is gated by ``x-cron-key``
(matched against ``SEO_CRON_KEY``, endpoint is inert until that env var is set).
"""
from __future__ import annotations

import hmac
import logging
import os
import re

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.security import get_current_user, require_creator
from seo_geo_agent import advisor as seo_advisor
from seo_geo_agent import audit as seo_audit
from seo_geo_agent import briefs as seo_briefs
from seo_geo_agent import competitors as seo_competitors
from seo_geo_agent import insights, keywords as seo_keywords, sources
from seo_geo_agent.sources import CredentialMissing

router = APIRouter()
logger = logging.getLogger("agentos.seo_geo")

SEO_AGENT_ID = "a2"  # "SEO Analyst" slot in the frontend agent catalog
TODO_STATUSES = {"todo", "assigned", "done"}


class BrandIn(BaseModel):
    id: str = ""  # slug; derived from name when omitted
    name: str
    domain: str
    gsc_property: str = ""
    seeds: list[str] = []
    enabled: bool = True


class TodoStatusIn(BaseModel):
    status: str


class CompetitorsIn(BaseModel):
    domains: list[str]


class QueryIn(BaseModel):
    query: str


class KeywordIn(BaseModel):
    keyword: str


class PageIn(BaseModel):
    page: str


class DraftIn(BaseModel):
    text: str
    keyword: str


class AskIn(BaseModel):
    question: str


def _rows_28d(brand: dict) -> tuple[list, list[str]]:
    """Latest 28-day GSC rows, degrading to empty + a note when access is missing."""
    prop = brand.get("gsc_property") or f"sc-domain:{brand['domain']}"
    end = date.today()
    try:
        return sources.gsc_fetch(prop, end - timedelta(days=28), end), []
    except CredentialMissing as exc:
        return [], [f"Search Console: {exc}"]


def _brand_or_404(brand_id: str) -> dict:
    brand = next((b for b in insights.list_brands() if b["id"] == brand_id), None)
    if not brand:
        raise HTTPException(status_code=404, detail="Unknown brand")
    return brand


@router.get("/seo-geo/overview")
def overview(user=Depends(get_current_user)):
    cards = []
    for brand in insights.list_brands():
        run = insights.latest_run(brand["id"])
        cards.append({
            "brand": brand,
            "last_run": run and {
                "at": run["at"],
                "summary": run["summary"],
                "degraded": run["degraded"],
                "todo_count": len(run["todos"]),
                "topic_count": len(run["topics"]),
            },
        })
    return {
        "sources": {"gsc": sources.gsc_available(), "serp": sources.serper_available()},
        "brands": cards,
    }


@router.post("/seo-geo/brands")
def save_brand(payload: BrandIn, user=Depends(require_creator)):
    slug = re.sub(r"[^a-z0-9]+", "-", (payload.id or payload.name).lower()).strip("-")
    if not slug:
        raise HTTPException(status_code=422, detail="Brand needs a name")
    domain = re.sub(r"^https?://", "", payload.domain.lower()).strip("/ ").removeprefix("www.")
    if "." not in domain:
        raise HTTPException(status_code=422, detail="Enter the site domain, e.g. brand.com")
    brand = {
        "id": slug,
        "name": payload.name.strip() or slug,
        "domain": domain,
        "gsc_property": payload.gsc_property.strip() or f"sc-domain:{domain}",
        "seeds": [s.strip() for s in payload.seeds if s.strip()][:10],
        "enabled": payload.enabled,
    }
    return {"brands": insights.upsert_brand(brand)}


@router.delete("/seo-geo/brands/{brand_id}")
def remove_brand(brand_id: str, user=Depends(require_creator)):
    _brand_or_404(brand_id)
    return {"brands": insights.delete_brand(brand_id)}


@router.post("/seo-geo/run/{brand_id}")
def run_brand(brand_id: str, user=Depends(get_current_user)):
    brand = _brand_or_404(brand_id)
    run = insights.run_brand(brand, trigger=f"manual:{user['email']}")
    return {"at": run["at"], "summary": run["summary"], "degraded": run["degraded"],
            "todo_count": len(run["todos"]), "topic_count": len(run["topics"])}


@router.get("/seo-geo/brands/{brand_id}")
def brand_detail(brand_id: str, user=Depends(get_current_user)):
    brand = _brand_or_404(brand_id)
    return {"brand": brand, "run": insights.latest_run(brand_id)}


@router.post("/seo-geo/todos/{brand_id}/{todo_id}")
def set_todo_status(brand_id: str, todo_id: str, payload: TodoStatusIn, user=Depends(get_current_user)):
    _brand_or_404(brand_id)
    if payload.status not in TODO_STATUSES:
        raise HTTPException(status_code=422, detail=f"Status must be one of {sorted(TODO_STATUSES)}")
    insights.set_todo_status(brand_id, todo_id, payload.status)
    return {"id": todo_id, "status": payload.status}


# ------------------------- keyword lab -------------------------

@router.post("/seo-geo/keywords/{brand_id}/run")
def run_keyword_lab(brand_id: str, user=Depends(get_current_user)):
    brand = _brand_or_404(brand_id)
    rows, notes = _rows_28d(brand)
    return seo_keywords.run_keyword_lab(
        brand, rows, trigger=f"manual:{user['email']}", extra_notes=notes
    )


@router.get("/seo-geo/keywords/{brand_id}")
def get_keywords(brand_id: str, user=Depends(get_current_user)):
    _brand_or_404(brand_id)
    return {"lab": seo_keywords.latest(brand_id)}


# ------------------------- competitors & SERP -------------------------

@router.get("/seo-geo/competitors/{brand_id}")
def get_competitors(brand_id: str, user=Depends(get_current_user)):
    brand = _brand_or_404(brand_id)
    from seo_geo_agent import state as seo_state

    ranks_doc = seo_state.load(f"ranks-{brand_id}") or {}
    sitemap_doc = seo_state.load(f"sitemaps-{brand_id}") or {}
    return {
        "tracked": brand.get("competitors", []),
        "suggested": ranks_doc.get("suggested_competitors", []),
        "shifts": seo_competitors.rank_shifts(brand_id),
        "feed": sitemap_doc.get("last_feed", {}),
    }


@router.put("/seo-geo/competitors/{brand_id}")
def set_competitors(brand_id: str, payload: CompetitorsIn, user=Depends(require_creator)):
    brand = _brand_or_404(brand_id)
    brand["competitors"] = [d.strip().lower() for d in payload.domains if d.strip()][:8]
    insights.upsert_brand(brand)
    return {"tracked": brand["competitors"]}


@router.post("/seo-geo/competitors/{brand_id}/track")
def track_competitors(brand_id: str, user=Depends(get_current_user)):
    """Take a rank snapshot + check competitor sitemaps for new content, now."""
    brand = _brand_or_404(brand_id)
    degraded: list[str] = []
    try:
        seo_competitors.rank_snapshot(brand)
    except CredentialMissing as exc:
        degraded.append(str(exc))
    feed = {}
    try:
        feed = seo_competitors.sitemap_watch(brand)
    except CredentialMissing as exc:
        degraded.append(f"Sitemap watch: {exc}")
    return {"shifts": seo_competitors.rank_shifts(brand_id), "feed": feed, "degraded": degraded}


@router.post("/seo-geo/serp/{brand_id}")
def serp_xray(brand_id: str, payload: QueryIn, user=Depends(get_current_user)):
    """Reverse-engineer the top of the SERP for any query, on demand."""
    brand = _brand_or_404(brand_id)
    try:
        return seo_competitors.serp_deep_dive(brand, payload.query.strip())
    except CredentialMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ------------------------- briefs & decay plans -------------------------

@router.get("/seo-geo/briefs/{brand_id}")
def get_briefs(brand_id: str, user=Depends(get_current_user)):
    _brand_or_404(brand_id)
    return {"briefs": seo_briefs.list_briefs(brand_id)}


@router.post("/seo-geo/briefs/{brand_id}")
def build_brief(brand_id: str, payload: KeywordIn, user=Depends(get_current_user)):
    brand = _brand_or_404(brand_id)
    rows, _ = _rows_28d(brand)
    try:
        return seo_briefs.build_brief(brand, payload.keyword.strip(), rows)
    except CredentialMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/seo-geo/update-plan/{brand_id}")
def build_update_plan(brand_id: str, payload: PageIn, user=Depends(get_current_user)):
    brand = _brand_or_404(brand_id)
    rows, _ = _rows_28d(brand)
    try:
        return seo_briefs.update_plan(brand, payload.page.strip(), rows)
    except CredentialMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ------------------------- audit & draft scoring -------------------------

@router.post("/seo-geo/audit/{brand_id}/run")
def run_audit(brand_id: str, user=Depends(get_current_user)):
    brand = _brand_or_404(brand_id)
    try:
        return seo_audit.site_audit(brand)
    except CredentialMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/seo-geo/audit/{brand_id}")
def get_audit(brand_id: str, user=Depends(get_current_user)):
    _brand_or_404(brand_id)
    return {"report": seo_audit.latest_audit(brand_id)}


@router.post("/seo-geo/draft-score/{brand_id}")
def draft_score(brand_id: str, payload: DraftIn, user=Depends(get_current_user)):
    brand = _brand_or_404(brand_id)
    brief = next(
        (b for b in seo_briefs.list_briefs(brand_id)
         if b["keyword"].lower() == payload.keyword.strip().lower()),
        None,
    )
    return seo_audit.score_draft(brand, payload.text, payload.keyword.strip(), brief)


@router.post("/seo-geo/ask/{brand_id}")
def ask_expert(brand_id: str, payload: AskIn, user=Depends(get_current_user)):
    """Grounded SEO-strategist chat over everything the agent knows about the brand."""
    brand = _brand_or_404(brand_id)
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="Ask a question")
    try:
        return seo_advisor.ask(brand, question)
    except CredentialMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ------------------------------- cron -------------------------------

@router.post("/seo-geo/cron/run")
def cron_run(request: Request):
    expected = os.environ.get("SEO_CRON_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="SEO_CRON_KEY not configured")
    if not hmac.compare_digest(request.headers.get("x-cron-key", ""), expected):
        raise HTTPException(status_code=403, detail="Bad cron key")
    results = {}
    for brand in insights.list_brands():
        if not brand.get("enabled", True):
            continue
        try:
            run = insights.run_brand(brand, trigger="cron")
            entry = {"ok": True, "todo_count": len(run["todos"])}
            # Tracking extras are best-effort: missing keys must not fail the sweep.
            try:
                seo_competitors.rank_snapshot(brand)
                entry["ranks"] = "updated"
            except Exception as exc:  # noqa: BLE001
                entry["ranks"] = f"skipped: {exc}"
            try:
                seo_competitors.sitemap_watch(brand)
                entry["sitemaps"] = "updated"
            except Exception as exc:  # noqa: BLE001
                entry["sitemaps"] = f"skipped: {exc}"
            results[brand["id"]] = entry
        except Exception as exc:  # noqa: BLE001 — one bad brand must not kill the sweep
            logger.exception("seo cron failed for %s", brand["id"])
            results[brand["id"]] = {"ok": False, "error": str(exc)}
    return {"brands": results}
