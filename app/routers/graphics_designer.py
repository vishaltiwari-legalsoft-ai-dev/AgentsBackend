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


def _apply_element_styles(cfg: dict, incoming: dict) -> None:
    """Validate + merge per-element Stage-3 styling into the run config.

    Each element may set ``font`` (any Causten variant), ``color`` (dark /
    gradient / white — text elements only) and ``placement`` (text or CTA
    placement key, placeable elements only). Unknown elements/attributes or
    out-of-family values are rejected so the prompt only ever sees valid input.
    """
    elements = {e["key"]: e for e in variants.STAGE3_ELEMENTS}
    color_keys = set(variants.TEXT_COLOR_KEYS)
    text_places = {p["key"] for p in variants.TEXT_PLACEMENTS}
    cta_places = {p["key"] for p in variants.CTA_PLACEMENTS}
    styles = cfg.setdefault("element_styles", {})

    for key, patch in incoming.items():
        meta = elements.get(key)
        if not meta or not isinstance(patch, dict):
            raise HTTPException(400, f"Unknown Stage-3 element '{key}'")
        cur = dict(styles.get(key) or {})
        if "font" in patch:
            if patch["font"] not in variants.FONTS:
                raise HTTPException(
                    400, f"Font is locked to the {variants.FONT_FAMILY} family; "
                    f"'{patch['font']}' is not an allowed variant.")
            cur["font"] = patch["font"]
        if "color" in patch:
            if not meta["colorable"]:
                raise HTTPException(400, f"Element '{key}' has a locked colour.")
            if patch["color"] not in color_keys:
                raise HTTPException(400, f"Unknown text colour '{patch['color']}'")
            cur["color"] = patch["color"]
        if "placement" in patch:
            if not meta["placeable"]:
                raise HTTPException(400, f"Element '{key}' has no placement control.")
            allowed = cta_places if meta["placement_kind"] == "cta" else text_places
            if patch["placement"] not in allowed:
                raise HTTPException(400, f"Unknown placement '{patch['placement']}' for '{key}'")
            cur["placement"] = patch["placement"]
        styles[key] = cur


def _apply_logo_layout(cfg: dict, patch: dict) -> None:
    """Validate + merge the Stage-4 logo placement controls into the run config."""
    from graphics_designer_agent.compositor import default_logo_layout

    positions = {p["key"] for p in variants.LOGO_POSITIONS}
    cur = {**default_logo_layout(), **(cfg.get("logo_layout") or {})}

    if "position" in patch:
        if patch["position"] not in positions:
            raise HTTPException(400, f"Unknown logo position '{patch['position']}'")
        cur["position"] = patch["position"]
    if "size_pct" in patch:
        v = patch["size_pct"]
        if v is not None:
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise HTTPException(400, "logo size_pct must be a number")
            if not 1 <= v <= 100:
                raise HTTPException(400, "logo size_pct must be between 1 and 100")
        cur["size_pct"] = v
    if "margin_pct" in patch:
        try:
            m = float(patch["margin_pct"])
        except (TypeError, ValueError):
            raise HTTPException(400, "logo margin_pct must be a number")
        if not 0 <= m <= 25:
            raise HTTPException(400, "logo margin_pct must be between 0 and 25")
        cur["margin_pct"] = m
    for axis in ("offset_x", "offset_y"):
        if axis in patch:
            try:
                cur[axis] = int(round(float(patch[axis])))
            except (TypeError, ValueError):
                raise HTTPException(400, f"logo {axis} must be an integer")
            if abs(cur[axis]) > variants.LOGO_OFFSET_PX_RANGE:
                raise HTTPException(400, f"logo {axis} out of range")
    cfg["logo_layout"] = cur


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
        "text_colors": variants.TEXT_COLORS,
        "stage3_elements": variants.STAGE3_ELEMENTS,
        "logo_positions": variants.LOGO_POSITIONS,
        "logo_size_pct_min": variants.LOGO_SIZE_PCT_MIN,
        "logo_size_pct_max": variants.LOGO_SIZE_PCT_MAX,
        "logo_offset_px_range": variants.LOGO_OFFSET_PX_RANGE,
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
    # Per-element Stage-3 styling: element key -> {font?, color?, placement?}.
    element_styles: dict[str, dict] | None = None
    # Stage-4 logo placement: {position?, size_pct?, margin_pct?, offset_x?, offset_y?}.
    logo_layout: dict | None = None
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
    if body.element_styles is not None:
        _apply_element_styles(cfg, body.element_styles)
    if body.logo_layout is not None:
        _apply_logo_layout(cfg, body.logo_layout)
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
