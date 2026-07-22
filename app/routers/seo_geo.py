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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.security import get_current_user, require_creator
from seo_geo_agent import insights, sources

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
    domain = re.sub(r"^https?://", "", payload.domain.lower()).strip("/ ")
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
            results[brand["id"]] = {"ok": True, "todo_count": len(run["todos"])}
        except Exception as exc:  # noqa: BLE001 — one bad brand must not kill the sweep
            logger.exception("seo cron failed for %s", brand["id"])
            results[brand["id"]] = {"ok": False, "error": str(exc)}
    return {"brands": results}
