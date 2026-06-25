"""Creative Agent API — the dedicated rail for brochures, decks, carousels, blogs.

Standard social posts stay on the 4-stage Graphics Studio editor (``/api/gd``).
Everything else routes here (``/api/creative``): the agent plans the piece,
grounds it in the brand's prior creatives, and renders a real PDF / PPTX / image
set — manually step-by-step, or fully autonomously after the user acknowledges
the mandatory warning. Every decision is logged for audit; human override is one
call away.

Endpoints (all under ``/api/creative``):
- ``GET  /creative/types``                 routed types + 4 steps + warning + engines
- ``POST /creative/runs``                  create a run (type + brief + autonomous?)
- ``GET  /creative/runs/{id}``             fetch a run (with artifact URLs)
- ``POST /creative/runs/{id}/intent``      capture brief/answers (step 1)
- ``POST /creative/runs/{id}/acknowledge`` ack the autonomous-mode warning
- ``POST /creative/runs/{id}/plan``        build the reviewable plan (step 3)
- ``POST /creative/runs/{id}/plan/approve``approve the plan
- ``POST /creative/runs/{id}/generate``    render final artifacts (step 4)
- ``POST /creative/runs/{id}/autonomous``  run all four steps end-to-end
- ``POST /creative/runs/{id}/override``    take manual control (one click)
- ``GET  /creative/runs/{id}/decisions``   the decision log
- ``GET  /creative/runs/{id}/artifact/{name}`` download one produced artifact
"""

from __future__ import annotations

from typing import Optional

import io

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.security import get_current_user

# On sys.path via app.__init__ (agent root registered there).
from graphics_designer_agent import reference_library as rl
from graphics_designer_agent.creative import document_builder as db
from graphics_designer_agent.creative import pipeline
from graphics_designer_agent.creative import runs as cruns
from graphics_designer_agent.creative import types as ctypes

router = APIRouter()


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #

def _artifact_url(run_id: str, name: str) -> str:
    return f"/api/creative/runs/{run_id}/artifact/{name}"


def _to_client(run: dict) -> dict:
    out = dict(run)
    out["artifacts"] = [
        {**a, "url": _artifact_url(run["id"], a["name"])}
        for a in run.get("artifacts", [])
    ]
    return out


def _owned(run_id: str, user: dict) -> dict:
    run = cruns.get_run(run_id)
    if not run or run.get("user_id") != str(user["id"]):
        raise HTTPException(404, "Run not found")
    return run


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #

@router.get("/creative/types")
def list_types(_user: dict = Depends(get_current_user)) -> dict:
    """The types the Creative Agent owns, the 4-step model, the mandatory
    autonomous warning, and which output engines this host can run."""
    return {
        "types": ctypes.creative_agent_types(),
        "steps": ctypes.STEPS,
        "autonomous_warning": ctypes.AUTONOMOUS_WARNING,
        "engines": db.engine_status(),
    }


# --------------------------------------------------------------------------- #
# Run lifecycle
# --------------------------------------------------------------------------- #

class CreateBody(BaseModel):
    creative_type: str
    brand_id: Optional[str] = None
    brief: str = ""
    autonomous: bool = False


@router.post("/creative/runs")
def create(body: CreateBody, user: dict = Depends(get_current_user)) -> dict:
    if not rl.is_known_type(body.creative_type):
        raise HTTPException(400, f"Unknown creative type: {body.creative_type}")
    if not rl.routes_to_creative_agent(body.creative_type):
        raise HTTPException(
            400,
            f"'{body.creative_type}' is a standard social post — use the Graphics "
            f"Studio editor, not the Creative Agent.",
        )
    run = cruns.create_run(
        str(user["id"]), body.creative_type,
        brand_id=body.brand_id, brief=body.brief, autonomous=body.autonomous,
    )
    return _to_client(run)


@router.get("/creative/runs/{run_id}")
def get(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    return _to_client(_owned(run_id, user))


class IntentBody(BaseModel):
    brief: Optional[str] = None
    answers: Optional[dict] = None


@router.post("/creative/runs/{run_id}/intent")
def intent(run_id: str, body: IntentBody, user: dict = Depends(get_current_user)) -> dict:
    run = _owned(run_id, user)
    return _to_client(pipeline.gather_intent(run, brief=body.brief, answers=body.answers))


@router.post("/creative/runs/{run_id}/acknowledge")
def acknowledge(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    run = _owned(run_id, user)
    return _to_client(pipeline.acknowledge(run))


class PlanBody(BaseModel):
    count: Optional[int] = None
    use_llm: bool = True


@router.post("/creative/runs/{run_id}/plan")
def plan(run_id: str, body: PlanBody = Body(default=PlanBody()),
         user: dict = Depends(get_current_user)) -> dict:
    run = _owned(run_id, user)
    try:
        return _to_client(pipeline.make_plan(run, count=body.count, use_llm=body.use_llm))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/creative/runs/{run_id}/plan/approve")
def approve(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    run = _owned(run_id, user)
    try:
        return _to_client(pipeline.approve_plan(run))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/creative/runs/{run_id}/generate")
def generate(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    run = _owned(run_id, user)
    try:
        return _to_client(pipeline.produce(run))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:  # engine missing (reportlab/python-pptx)
        raise HTTPException(503, str(exc)) from exc


class AutoBody(BaseModel):
    count: Optional[int] = None
    use_llm: bool = True


@router.post("/creative/runs/{run_id}/autonomous")
def autonomous(run_id: str, body: AutoBody = Body(default=AutoBody()),
               user: dict = Depends(get_current_user)) -> dict:
    run = _owned(run_id, user)
    try:
        return _to_client(pipeline.run_autonomous(run, count=body.count, use_llm=body.use_llm))
    except pipeline.AutonomyError as exc:
        # Acknowledgement required first — 428 Precondition Required.
        raise HTTPException(428, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/creative/runs/{run_id}/override")
def override(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    run = _owned(run_id, user)
    return _to_client(pipeline.take_manual_control(run))


@router.get("/creative/runs/{run_id}/decisions")
def decisions(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    run = _owned(run_id, user)
    return {"decisions": run.get("decision_log", [])}


@router.get("/creative/runs/{run_id}/artifact/{name}")
def artifact(run_id: str, name: str, user: dict = Depends(get_current_user)) -> Response:
    run = _owned(run_id, user)
    meta = next((a for a in run.get("artifacts", []) if a["name"] == name), None)
    if not meta:
        raise HTTPException(404, "Unknown artifact")
    try:
        data = cruns.read_artifact(run_id, meta["ref"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"Artifact unavailable: {exc}") from exc
    # Stream rather than buffer: Cloud Run caps a buffered response at 32 MiB,
    # which a large brochure/carousel can exceed — the download then fails
    # client-side with "Failed to fetch". A StreamingResponse with NO
    # Content-Length uses chunked transfer encoding, which Cloud Run does not cap.
    return StreamingResponse(
        io.BytesIO(data),
        media_type=meta.get("mime", "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
