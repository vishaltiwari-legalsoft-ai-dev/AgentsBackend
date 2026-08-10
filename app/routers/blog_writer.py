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
from app.services import run_tracking
from seo_geo_agent import insights
from seo_geo_agent.sources import CredentialMissing

from blog_writer_agent import drafting, export, inventory, research, visuals
from blog_writer_agent import voice as bw_voice

router = APIRouter()
logger = logging.getLogger("agentos.blog_writer")

BLOG_AGENT_ID = "a9"  # "Blog Writer" slot in the frontend agent catalog
BLOG_AGENT_NAME = "Blog Writer"


def _track(user: dict, action: str, task: str, run: dict | None = None,
           *, usage_action: str = "generate") -> None:
    """Mandatory usage trail → agent_runs__a9 + master runs (admin DB panel)."""
    run_tracking.record_activity(
        user, agent_id=BLOG_AGENT_ID, agent_name=BLOG_AGENT_NAME, category="copy",
        action=action, task=task,
        brand_id=(run or {}).get("brand_id"), run_id=(run or {}).get("id"),
        usage_action=usage_action,
    )

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


def _run(run_id: str, user: dict | None = None) -> dict:
    run = research.load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
    # Ownership: runs created before user stamping (no user_id) stay visible to
    # everyone; stamped runs are owner + admin only.
    if user is not None:
        owner = run.get("user_id") or ""
        if owner and owner != user.get("id") and not user.get("is_admin"):
            raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
    return run


@router.get("/blog/brands")
def list_brands(user: dict = Depends(get_current_user)) -> dict:
    brands = []
    for brand in insights.list_brands():
        if not brand.get("enabled", True):
            continue
        inv = inventory.latest(brand["id"])
        vc = bw_voice.latest(brand["id"])
        brands.append(
            {
                "id": brand["id"],
                "name": brand.get("name", brand["id"]),
                "domain": brand.get("domain", ""),
                "inventory": {"counts": inv["counts"], "scanned": inv["scanned"]} if inv else None,
                "voice": {"studied": vc["studied"], "count": vc["count"]} if vc else None,
            }
        )
    return {"brands": brands}


@router.get("/blog/brands/{brand_id}/voice")
def get_voice(brand_id: str, user: dict = Depends(get_current_user)) -> dict:
    _brand(brand_id)
    vc = bw_voice.latest(brand_id)
    if not vc:
        raise HTTPException(status_code=404, detail="voice not studied yet — run a study first")
    return vc


@router.post("/blog/brands/{brand_id}/voice")
def study_voice(brand_id: str, user: dict = Depends(get_current_user)) -> dict:
    brand = _brand(brand_id)
    try:
        vc = bw_voice.study(brand, inventory.latest(brand_id))
    except CredentialMissing as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _track(user, "voice_study", f"Studied brand voice for {brand.get('name', brand_id)}",
           {"brand_id": brand_id})
    return vc


@router.get("/blog/brands/{brand_id}/inventory")
def get_inventory(brand_id: str, user: dict = Depends(get_current_user)) -> dict:
    _brand(brand_id)
    inv = inventory.latest(brand_id)
    if not inv:
        raise HTTPException(status_code=404, detail="not scanned yet — run a scan first")
    return inv


@router.post("/blog/brands/{brand_id}/inventory")
def scan_inventory(brand_id: str, user: dict = Depends(get_current_user)) -> dict:
    inv = inventory.scan(_brand(brand_id))
    _track(user, "inventory_scan", f"Scanned content inventory for {brand_id}",
           {"brand_id": brand_id})
    return inv


@router.get("/blog/runs")
def list_runs(user: dict = Depends(get_current_user)) -> dict:
    runs = [
        r
        for r in research.list_runs()
        if not r.get("user_id")
        or r["user_id"] == user.get("id")
        or user.get("is_admin")
    ]
    return {"runs": runs}


@router.get("/blog/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    return _run(run_id, user)


@router.post("/blog/runs")
def create_run(body: RunIn, user: dict = Depends(get_current_user)) -> dict:
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(status_code=422, detail="topic is required")
    run = research.new_run(
        _brand(body.brand_id), topic, body.notes.strip(), user_id=str(user.get("id") or "")
    )
    _track(user, "run_created", f"New blog run: “{topic}”", run, usage_action="session")
    return run


@router.post("/blog/runs/{run_id}/research/step")
def research_step(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    run = _run(run_id, user)
    try:
        result = research.research_step(run)
    except CredentialMissing as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc
    _track(user, "research_step", f"Research step on “{run.get('topic', '')}”", run)
    return result


@router.post("/blog/runs/{run_id}/draft")
def build_draft(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    run = _run(run_id, user)
    if not run["ledger"]:
        raise HTTPException(status_code=409, detail="no evidence yet — run research first")
    try:
        result = drafting.build_draft(
            run, inventory.latest(run["brand_id"]), voice=bw_voice.latest(run["brand_id"])
        )
    except CredentialMissing as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _track(user, "draft", f"Drafted “{run.get('topic', '')}”", run)
    return result


@router.post("/blog/runs/{run_id}/blocks/{block_id}/comment")
def comment_block(run_id: str, block_id: str, body: CommentIn, user: dict = Depends(get_current_user)) -> dict:
    run = _run(run_id, user)
    comment = body.comment.strip()
    if not comment:
        raise HTTPException(status_code=422, detail="comment is required")
    if not run.get("draft"):
        raise HTTPException(status_code=409, detail="no draft yet — build the draft first")
    try:
        result = drafting.revise_block(run, block_id, comment, voice=bw_voice.latest(run["brand_id"]))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown block: {block_id}") from exc
    except CredentialMissing as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc
    _track(user, "revise_block", f"Revision: {comment}", run)
    return result


@router.post("/blog/runs/{run_id}/visuals")
def plan_visuals(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    run = _run(run_id, user)
    try:
        result = visuals.plan_visuals(run, _brand(run["brand_id"]))
    except CredentialMissing as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _track(user, "visuals", f"Planned visuals for “{run.get('topic', '')}”", run)
    return result


@router.get("/blog/runs/{run_id}/export")
def export_run(run_id: str, format: str = "md", user: dict = Depends(get_current_user)) -> PlainTextResponse:
    run = _run(run_id, user)
    if format not in _EXPORTS:
        raise HTTPException(status_code=422, detail=f"unknown format: {format}")
    render, media_type, name_tpl = _EXPORTS[format]
    try:
        content = render(run)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    slug = (run.get("draft") or {}).get("meta", {}).get("slug") or run["id"]
    filename = name_tpl.format(slug=slug)
    _track(user, f"export:{format}", f"Exported “{slug}” as {format}", run)
    return PlainTextResponse(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
