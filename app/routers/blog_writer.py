"""Blog Writer agent (a9) API — brand catalogue, inventory, deep-research desk.

Mounted under ``/api/blog``. Auth: any signed-in user. Spec:
docs/superpowers/specs/2026-08-03-blog-writer-rebuild-design.md

Error contract: missing credentials surface as 424 with the real message
(never fabricated research); stage-order violations are 409; unknown
run/brand/block/stage are 404.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.security import get_current_user
from seo_geo_agent import insights
from seo_geo_agent.sources import CredentialMissing

from blog_writer_agent import drafting, export, inventory, research, visuals

router = APIRouter()
logger = logging.getLogger("agentos.blog_writer")

BLOG_AGENT_ID = "a9"  # "Blog Writer" slot in the frontend agent catalog

_EXPORTS = {
    "md": (export.to_markdown, "text/markdown", "{slug}.md"),
    "html": (export.to_html, "text/html", "{slug}.html"),
    "txt": (export.to_text, "text/plain", "{slug}.txt"),
    "visuals-md": (export.visuals_markdown, "text/markdown", "{slug}-visual-prompts.md"),
    "visuals-txt": (export.visuals_text, "text/plain", "{slug}-visual-prompts.txt"),
}


class RunIn(BaseModel):
    brand_id: str
    topic: str
    notes: str = ""


class CommentIn(BaseModel):
    comment: str


def _brand(brand_id: str) -> dict:
    for brand in insights.list_brands():
        if brand["id"] == brand_id and brand.get("enabled", True):
            return brand
    raise HTTPException(status_code=404, detail=f"unknown brand: {brand_id}")


def _run(run_id: str) -> dict:
    run = research.load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
    return run


@router.get("/blog/brands")
def list_brands(user: dict = Depends(get_current_user)) -> dict:
    brands = []
    for brand in insights.list_brands():
        if not brand.get("enabled", True):
            continue
        inv = inventory.latest(brand["id"])
        brands.append(
            {
                "id": brand["id"],
                "name": brand.get("name", brand["id"]),
                "domain": brand.get("domain", ""),
                "inventory": {"counts": inv["counts"], "scanned": inv["scanned"]} if inv else None,
            }
        )
    return {"brands": brands}


@router.get("/blog/brands/{brand_id}/inventory")
def get_inventory(brand_id: str, user: dict = Depends(get_current_user)) -> dict:
    _brand(brand_id)
    inv = inventory.latest(brand_id)
    if not inv:
        raise HTTPException(status_code=404, detail="not scanned yet — run a scan first")
    return inv


@router.post("/blog/brands/{brand_id}/inventory")
def scan_inventory(brand_id: str, user: dict = Depends(get_current_user)) -> dict:
    return inventory.scan(_brand(brand_id))


@router.get("/blog/runs")
def list_runs(user: dict = Depends(get_current_user)) -> dict:
    return {"runs": research.list_runs()}


@router.get("/blog/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    return _run(run_id)


@router.post("/blog/runs")
def create_run(body: RunIn, user: dict = Depends(get_current_user)) -> dict:
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(status_code=422, detail="topic is required")
    return research.new_run(_brand(body.brand_id), topic, body.notes.strip())


@router.post("/blog/runs/{run_id}/research/step")
def research_step(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    run = _run(run_id)
    try:
        return research.research_step(run)
    except CredentialMissing as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc


@router.post("/blog/runs/{run_id}/draft")
def build_draft(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    run = _run(run_id)
    if not run["ledger"]:
        raise HTTPException(status_code=409, detail="no evidence yet — run research first")
    try:
        return drafting.build_draft(run, inventory.latest(run["brand_id"]))
    except CredentialMissing as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/blog/runs/{run_id}/blocks/{block_id}/comment")
def comment_block(run_id: str, block_id: str, body: CommentIn, user: dict = Depends(get_current_user)) -> dict:
    run = _run(run_id)
    comment = body.comment.strip()
    if not comment:
        raise HTTPException(status_code=422, detail="comment is required")
    if not run.get("draft"):
        raise HTTPException(status_code=409, detail="no draft yet — build the draft first")
    try:
        return drafting.revise_block(run, block_id, comment)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown block: {block_id}") from exc
    except CredentialMissing as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc


@router.post("/blog/runs/{run_id}/visuals")
def plan_visuals(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    run = _run(run_id)
    try:
        return visuals.plan_visuals(run, _brand(run["brand_id"]))
    except CredentialMissing as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/blog/runs/{run_id}/export")
def export_run(run_id: str, format: str = "md", user: dict = Depends(get_current_user)) -> PlainTextResponse:
    run = _run(run_id)
    if format not in _EXPORTS:
        raise HTTPException(status_code=422, detail=f"unknown format: {format}")
    render, media_type, name_tpl = _EXPORTS[format]
    try:
        content = render(run)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    slug = (run.get("draft") or {}).get("meta", {}).get("slug") or run["id"]
    filename = name_tpl.format(slug=slug)
    return PlainTextResponse(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
