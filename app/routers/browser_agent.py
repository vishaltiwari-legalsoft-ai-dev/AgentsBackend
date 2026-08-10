"""Browser Agent (a11) API — the brain half of the Chrome-extension web copilot.

The extension owns the act loop: it POSTs one observation per step and executes
exactly one returned action (HTTP step-polling, same shape as a10 GEO's
``poll/step``). One LLM call per request keeps every response well under Cloud
Run's timeout. Any signed-in user may run; runs are private to their owner
(admins may read all). The kill switch (``BROWSER_AGENT_DISABLED=1``) turns
every endpoint into an honest 403 naming the flag.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.security import get_current_user
from app.services import run_tracking
from browser_agent import actions as browser_actions
from browser_agent import runs as browser_runs

router = APIRouter()
logger = logging.getLogger("agentos.browser")

BROWSER_AGENT_ID = "a11"
BROWSER_AGENT_NAME = "Browser Agent"


def _guard() -> None:
    if browser_runs.disabled():
        raise HTTPException(
            403, "Browser Agent is disabled on this deployment (BROWSER_AGENT_DISABLED)."
        )


def _track(user: dict, action: str, task: str, *, run_id: str | None = None,
           status: str = "completed", usage_action: str = "generate") -> None:
    """Mandatory usage trail → agent_runs__a11 + master runs (admin DB panel)."""
    run_tracking.record_activity(
        user, agent_id=BROWSER_AGENT_ID, agent_name=BROWSER_AGENT_NAME,
        category="data", action=action, task=task, run_id=run_id,
        status=status, usage_action=usage_action,
    )


def _run_or_404(run_id: str, user: dict) -> dict:
    run = browser_runs.get_run(run_id)
    # 404 (not 403) for other users' runs — don't leak that the id exists.
    if not run or (run.get("user_id") != str(user.get("id")) and not user.get("is_admin")):
        raise HTTPException(status_code=404, detail="Unknown run")
    return run


def _track_end_once(run: dict, user: dict) -> None:
    """Log the terminal event exactly once, even across replayed steps."""
    if run["status"] in browser_runs.TERMINAL and not run.get("tracked_end"):
        run["tracked_end"] = True
        browser_runs._persist(run)
        task = run.get("summary") or run.get("fail_reason") or run.get("goal", "")
        _track(user, f"run_{run['status']}", str(task), run_id=run["id"],
               status=run["status"])


class RunIn(BaseModel):
    goal: str = Field(min_length=3, max_length=2000)
    mode: str = Field(default="act", pattern="^(act|monitor)$")
    start_url: str | None = None


class StepIn(BaseModel):
    protocol: int
    seq: int = Field(ge=1)
    tab: dict | None = None
    tabs: list[dict] = Field(default_factory=list)
    dom: dict | None = None
    last_result: dict | None = None
    user_reply: str | None = Field(default=None, max_length=2000)
    confirmed: bool = False


@router.get("/browser/status")
def browser_status(user: dict = Depends(get_current_user)) -> dict:
    """Auth ping for the extension side panel ("Connected as …")."""
    _guard()
    pol = browser_runs.policy()
    return {
        "ok": True,
        "email": user.get("email"),
        "protocol": browser_actions.PROTOCOL,
        **pol,
    }


@router.post("/browser/runs")
def create_run(body: RunIn, user: dict = Depends(get_current_user)) -> dict:
    _guard()
    run = browser_runs.create_run(user, body.goal, body.mode, body.start_url)
    _track(user, "run_start", f"{body.mode}: {body.goal}", run_id=run["id"],
           usage_action="session")
    return {
        "run_id": run["id"],
        "protocol": browser_actions.PROTOCOL,
        "step_cap": run["step_cap"],
        "allowed": run["allowed"],
        "blocked": run["blocked"],
        "sensitive_confirm": run["sensitive_confirm"],
        "status": run["status"],
    }


@router.post("/browser/runs/{run_id}/step")
def run_step(run_id: str, body: StepIn, user: dict = Depends(get_current_user)) -> dict:
    _guard()
    run = _run_or_404(run_id, user)
    try:
        run, response = browser_runs.step(run, body.model_dump())
    except browser_runs.ProtocolMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except browser_runs.OutOfSync as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _track_end_once(run, user)
    return response


@router.post("/browser/runs/{run_id}/stop")
def stop_run(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    _guard()
    run = _run_or_404(run_id, user)
    run = browser_runs.stop_run(run)
    _track_end_once(run, user)
    return {"run_id": run["id"], "status": run["status"]}


@router.get("/browser/runs")
def list_runs(user: dict = Depends(get_current_user)) -> dict:
    _guard()
    user_id = None if user.get("is_admin") else str(user.get("id"))
    return {"runs": browser_runs.list_runs(user_id=user_id)}


@router.get("/browser/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    _guard()
    run = _run_or_404(run_id, user)
    # The full doc minus the replay cache — steps carry everything the UI shows.
    return {k: v for k, v in run.items() if k != "last_decision"}
