"""Graphics Designer agent — 4-stage ad-creative pipeline API (spec §4–§9).

All endpoints are namespaced under ``/api/gd``. Runs are owned by the
authenticated user; artifacts are streamed back through this router so the
frontend never needs direct storage access.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.security import get_current_user
from app.services import imaging

from graphics_designer_agent import pipeline, suggestions, variants
from graphics_designer_agent.pipeline import PipelineError
from graphics_designer_agent.prompts import CANONICAL_SHA256, load_prompt, prompt_hash
from graphics_designer_agent.runs import get_run, log_manifest, save_run
from graphics_designer_agent.tokens import ASPECT_RATIOS

router = APIRouter()
logger = logging.getLogger("agentos.gd")

CONTENT_TOKENS = ["headline", "highlight", "subtext1", "subtext2", "cta"]


# ── serialization ─────────────────────────────────────────────────────────────
def _artifact_url(run_id: str, rel: str) -> str:
    return f"/api/gd/runs/{run_id}/artifact/{rel}"


def _to_client(run: dict) -> dict:
    out = {k: v for k, v in run.items() if k != "stages"}
    stages = {}
    for n, st in run["stages"].items():
        attempts = [
            {**a, "url": _artifact_url(run["id"], a["artifact"])} for a in st["attempts"]
        ]
        approved = st["approved"]
        if approved:
            approved = {**approved, "url": _artifact_url(run["id"], approved["artifact"])}
        stages[n] = {**st, "attempts": attempts, "approved": approved}
    out["stages"] = stages
    out["tokens_ready"] = all(run["config"]["tokens_approved"].get(t) for t in CONTENT_TOKENS)
    return out


def _owned_run(run_id: str, user: dict) -> dict:
    run = get_run(run_id)
    if not run or run.get("user_id") != str(user["id"]):
        raise HTTPException(404, "Run not found")
    return run


def _guard(fn):
    try:
        return fn()
    except PipelineError as exc:
        raise HTTPException(409, str(exc)) from exc


# ── static config for the studio UI ───────────────────────────────────────────
@router.get("/gd/config")
def gd_config(_user: dict = Depends(get_current_user)) -> dict:
    return {
        "stage1_variants": variants.STAGE1_VARIANTS,
        "stage2_variants": variants.STAGE2_VARIANTS,
        "stage2_categories": variants.STAGE2_CATEGORIES,
        "fonts": variants.FONTS,
        "font_family": variants.FONT_FAMILY,
        "font_variants": variants.FONT_VARIANTS,
        "text_placements": variants.TEXT_PLACEMENTS,
        "cta_placements": variants.CTA_PLACEMENTS,
        "aspect_ratios": variants.ASPECT_RATIO_PRESETS,
        "brand_kit_block": variants.BRAND_KIT_BLOCK,
        "locked_colors": variants.LOCKED_COLORS,
        "stage1_source_note": variants.SOURCE_NOTE_STAGE1,
        "onboarding_questions": suggestions.ONBOARDING_QUESTIONS,
        "content_tokens": CONTENT_TOKENS,
    }


@router.get("/gd/prompts")
def gd_prompts(_user: dict = Depends(get_current_user)) -> dict:
    """Canonical prompt integrity report (audit panel)."""
    return {
        "prompts": [
            {"filename": name, "hash": prompt_hash(name), "expected": expected,
             "ok": prompt_hash(name) == expected, "bytes": len(load_prompt(name).encode("utf-8"))}
            for name, expected in CANONICAL_SHA256.items()
        ]
    }


# ── run lifecycle ─────────────────────────────────────────────────────────────
class CreateRunBody(BaseModel):
    brand_id: str | None = None


@router.post("/gd/runs")
def create_run_endpoint(body: CreateRunBody = Body(default=CreateRunBody()),
                        user: dict = Depends(get_current_user)) -> dict:
    from graphics_designer_agent.runs import create_run

    run = create_run(user_id=str(user["id"]), brand_id=body.brand_id)
    return _to_client(run)


@router.get("/gd/runs/{run_id}")
def get_run_endpoint(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    return _to_client(_owned_run(run_id, user))


class ConfigBody(BaseModel):
    font: str | None = None
    aspect_ratio: str | None = None
    text_placement: str | None = None
    cta_placement: str | None = None
    use_ai_compositor: bool | None = None
    tokens: dict[str, str] | None = None
    # token -> {approved: bool, source: "user"|"agent", original_suggestion?: str}
    token_approvals: dict[str, dict] | None = None


@router.post("/gd/runs/{run_id}/config")
def update_config(run_id: str, body: ConfigBody, user: dict = Depends(get_current_user)) -> dict:
    run = _owned_run(run_id, user)
    cfg = run["config"]
    if body.font is not None:
        # The creative font is locked to the Causten family — reject anything else.
        if body.font not in variants.FONTS:
            raise HTTPException(
                400,
                f"Font is locked to the {variants.FONT_FAMILY} family; "
                f"'{body.font}' is not an allowed variant.",
            )
        cfg["font"] = body.font
    if body.aspect_ratio is not None and body.aspect_ratio != cfg["aspect_ratio"]:
        if body.aspect_ratio not in ASPECT_RATIOS:
            raise HTTPException(400, f"Unknown aspect ratio '{body.aspect_ratio}'")
        # Aspect ratio is chosen at Stage 1 and LOCKED once the run advances past
        # it, so every downstream stage shares one canvas size (spec §6.2). It
        # becomes editable again only by going back to Stage 1 (which invalidates
        # the downstream approvals that depend on the canvas size).
        if not run["state"].startswith("STAGE1"):
            raise HTTPException(
                409,
                "Aspect ratio is locked after Stage 1. Go back to Stage 1 to change it.",
            )
        cfg["aspect_ratio"] = body.aspect_ratio
    if body.text_placement is not None:
        allowed = {p["key"] for p in variants.TEXT_PLACEMENTS}
        if body.text_placement not in allowed:
            raise HTTPException(400, f"Unknown text placement '{body.text_placement}'")
        cfg["text_placement"] = body.text_placement
    if body.cta_placement is not None:
        allowed = {p["key"] for p in variants.CTA_PLACEMENTS}
        if body.cta_placement not in allowed:
            raise HTTPException(400, f"Unknown CTA placement '{body.cta_placement}'")
        cfg["cta_placement"] = body.cta_placement
    if body.use_ai_compositor is not None:
        cfg["use_ai_compositor"] = bool(body.use_ai_compositor)
    if body.tokens:
        for k, v in body.tokens.items():
            if k in cfg["tokens"]:
                cfg["tokens"][k] = v
    if body.token_approvals:
        for token, info in body.token_approvals.items():
            if token not in cfg["tokens_approved"]:
                continue
            approved = bool(info.get("approved"))
            cfg["tokens_approved"][token] = approved
            if approved:
                log_manifest(
                    run, token=token, source=info.get("source", "user"),
                    original_suggestion=info.get("original_suggestion"),
                    final_value=cfg["tokens"].get(token),
                )
    save_run(run)
    return _to_client(run)


# ── generation / approval ─────────────────────────────────────────────────────
class GenerateBody(BaseModel):
    stage: int = Field(ge=1, le=3)
    variant: str | None = None


@router.post("/gd/runs/{run_id}/generate")
def generate_endpoint(run_id: str, body: GenerateBody, user: dict = Depends(get_current_user)) -> dict:
    run = _owned_run(run_id, user)
    if body.stage == 3 and not all(run["config"]["tokens_approved"].get(t) for t in CONTENT_TOKENS):
        raise HTTPException(409, "Approve all content tokens before generating Stage 3.")
    attempt = _guard(lambda: pipeline.generate(run, body.stage, variant=body.variant))
    return {"attempt": {**attempt, "url": _artifact_url(run_id, attempt["artifact"])}, "run": _to_client(run)}


@router.post("/gd/runs/{run_id}/stage4")
async def stage4_endpoint(
    run_id: str,
    logo: UploadFile = File(...),
    use_ai: bool = Form(default=False),
    user: dict = Depends(get_current_user),
) -> dict:
    run = _owned_run(run_id, user)
    raw = await logo.read()
    if not raw:
        raise HTTPException(400, "Empty logo upload")
    png = imaging.to_png_logo(raw, file_name=logo.filename or "", mime=logo.content_type or "")
    if not png:
        raise HTTPException(415, f"Couldn't read '{logo.filename}' as an image (PNG/JPG/SVG).")
    attempt = _guard(lambda: pipeline.generate_stage4(run, png, use_ai=use_ai))
    return {"attempt": {**attempt, "url": _artifact_url(run_id, attempt["artifact"])}, "run": _to_client(run)}


class ApproveBody(BaseModel):
    stage: int = Field(ge=1, le=4)
    attempt: int | None = None


@router.post("/gd/runs/{run_id}/approve")
def approve_endpoint(run_id: str, body: ApproveBody, user: dict = Depends(get_current_user)) -> dict:
    run = _owned_run(run_id, user)
    _guard(lambda: pipeline.approve(run, body.stage, body.attempt))
    return _to_client(run)


class BackBody(BaseModel):
    stage: int = Field(ge=1, le=4)


@router.post("/gd/runs/{run_id}/back")
def back_endpoint(run_id: str, body: BackBody, user: dict = Depends(get_current_user)) -> dict:
    run = _owned_run(run_id, user)
    _guard(lambda: pipeline.go_back(run, body.stage))
    return _to_client(run)


# ── prompt audit (build without generating) ───────────────────────────────────
@router.get("/gd/runs/{run_id}/prompt")
def prompt_preview(run_id: str, stage: int, variant: str = "A",
                   user: dict = Depends(get_current_user)) -> dict:
    run = _owned_run(run_id, user)
    if stage == 3:
        variant = "T"
    return _guard(lambda: pipeline.build_prompt(run, stage, variant.upper() if stage in (1, 2) else variant))


# ── suggestions (approval-gated) ──────────────────────────────────────────────
class SuggestBody(BaseModel):
    kind: str  # concept | explore | aspect_ratio | hooks | font | qa
    answers: dict | None = None
    placement: str | None = None
    concept: str | None = None
    stage: int | None = None
    exclude: list[str] | None = None  # variant ids to skip in 'explore'


@router.post("/gd/runs/{run_id}/suggest")
def suggest_endpoint(run_id: str, body: SuggestBody, user: dict = Depends(get_current_user)) -> dict:
    _owned_run(run_id, user)
    if body.kind == "concept":
        return suggestions.recommend_concept(body.answers or {})
    if body.kind == "explore":
        return suggestions.explore_elements(body.answers or {}, exclude=body.exclude)
    if body.kind == "aspect_ratio":
        return suggestions.recommend_aspect_ratio(body.placement)
    if body.kind == "hooks":
        return suggestions.generate_hooks(body.concept)
    if body.kind == "font":
        return suggestions.recommend_font(body.concept)
    if body.kind == "qa":
        return suggestions.qa_critique(body.stage or 1)
    raise HTTPException(400, f"Unknown suggestion kind '{body.kind}'")


# ── artifact streaming ────────────────────────────────────────────────────────
@router.get("/gd/runs/{run_id}/artifact/{rel:path}")
def get_artifact(run_id: str, rel: str, user: dict = Depends(get_current_user)):
    run = _owned_run(run_id, user)
    from graphics_designer_agent.runs import artifact_abspath

    try:
        path = artifact_abspath(run_id, rel)
    except ValueError:
        raise HTTPException(400, "Invalid path")
    if not path.exists():
        raise HTTPException(404, "Artifact not found")
    return Response(content=path.read_bytes(), media_type="image/png")
