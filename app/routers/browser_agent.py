"""Browser Agent (a11) API — the brain half of the Chrome-extension web copilot.

The extension owns the act loop: it POSTs one observation per step and executes
exactly one returned action (HTTP step-polling, same shape as a10 GEO's
``poll/step``). One LLM call per request keeps every response well under Cloud
Run's timeout. Any signed-in user may run; runs are private to their owner
(admins may read all). The kill switch (``BROWSER_AGENT_DISABLED=1``) turns
every endpoint into an honest 403 naming the flag.
"""
from __future__ import annotations

import json
import logging
import os
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.security import get_current_user
from app.services.run_tracking import CHANGE, JOB, Activity, ActivityTrail
from browser_agent import actions as browser_actions
from browser_agent import digest as browser_digest
from browser_agent import runs as browser_runs
from browser_agent import skills as browser_skills
from browser_agent import tools as browser_tools

router = APIRouter()
logger = logging.getLogger("agentos.browser")

BROWSER_AGENT_ID = "a11"
BROWSER_AGENT_NAME = "Browser Agent"

# The built extension ships inside the image (app/ is COPYd wholesale), so the
# console can hand it out without anyone needing a GitHub account.
EXTENSION_ZIP = Path(__file__).resolve().parents[1] / "static" / "agentos-browser-agent.zip"
_DEFAULT_EXT_DOMAINS = "legalsoft.com"


def _allowed_domains() -> set[str]:
    raw = os.environ.get("BROWSER_EXT_DOMAINS", _DEFAULT_EXT_DOMAINS)
    return {d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()}


def _may_download(user: dict) -> bool:
    """Company accounts get the extension; owners keep access whatever their domain.

    NOTE: signing in to the console is NOT domain-restricted, so membership can't
    be inferred from "is authenticated" — the email domain is checked here.
    """
    if user.get("is_creator") or user.get("is_admin"):
        return True
    email = str(user.get("email") or "").lower()
    domain = email.rpartition("@")[2]
    return bool(domain) and domain in _allowed_domains()


def _extension_version() -> str:
    """Version from the packaged manifest, so the panel can't advertise a stale build."""
    try:
        with zipfile.ZipFile(EXTENSION_ZIP) as bundle:
            return str(json.loads(bundle.read("manifest.json")).get("version") or "")
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return ""


def _guard() -> None:
    if browser_runs.disabled():
        raise HTTPException(
            403, "Browser Agent is disabled on this deployment (BROWSER_AGENT_DISABLED)."
        )


#: Every unit of Browser Agent work lands here — see THE RULE in run_tracking.py.
trail = ActivityTrail(agent_id=BROWSER_AGENT_ID, agent_name=BROWSER_AGENT_NAME,
                      category="data")


def _run_or_404(run_id: str, user: dict) -> dict:
    run = browser_runs.get_run(run_id)
    # 404 (not 403) for other users' runs — don't leak that the id exists.
    if not run or (run.get("user_id") != str(user.get("id")) and not user.get("is_admin")):
        raise HTTPException(status_code=404, detail="Unknown run")
    return run


def _end_once(run: dict, act: Activity) -> None:
    """Record the run's terminal event exactly once, even across replayed steps.

    A run is one unit of work, not one per step: a thirty-step run records a
    start and an end, and every step in between is deliberately silent (see THE
    RULE in run_tracking.py). ``act.skip`` is how a step that did not end the
    run says so out loud rather than by omission.
    """
    if run["status"] in browser_runs.TERMINAL and not run.get("tracked_end"):
        run["tracked_end"] = True
        browser_runs._persist(run)
        task = run.get("summary") or run.get("fail_reason") or run.get("goal", "")
        act.note(str(task), action=f"run_{run['status']}", run_id=run["id"],
                 status=str(run["status"]))
        return
    act.skip("the run has not reached a terminal state on this request")


class RunIn(BaseModel):
    goal: str = Field(min_length=3, max_length=2000)
    mode: str = Field(default="act", pattern="^(act|monitor)$")
    start_url: str | None = None


class WatchRule(BaseModel):
    id: str | None = None
    text: str = Field(min_length=2, max_length=200)
    enabled: bool = True


class ConfigIn(BaseModel):
    # None = leave untouched (same convention as the GEO/admin config endpoints).
    watch_rules: list[WatchRule] | None = None


class DigestIn(BaseModel):
    events: list[dict] = Field(default_factory=list)
    tabs: list[dict] = Field(default_factory=list)


class StepIn(BaseModel):
    protocol: int
    seq: int = Field(ge=1)
    tab: dict | None = None
    tabs: list[dict] = Field(default_factory=list)
    dom: dict | None = None
    last_result: dict | None = None
    user_reply: str | None = Field(default=None, max_length=2000)
    confirmed: bool = False
    # A viewport screenshot as a data: URL, attached only when the server asked
    # for one (see want_screenshot). ~4MB covers a full-width JPEG comfortably.
    screenshot: str | None = Field(default=None, max_length=4_000_000)


@router.get("/browser/status")
def browser_status(user: dict = Depends(get_current_user)) -> dict:
    """Auth ping for the extension side panel ("Connected as …")."""
    _guard()
    pol = browser_runs.policy()
    return {
        "ok": True,
        "email": user.get("email"),
        "protocol": browser_actions.PROTOCOL,
        "can_download": _may_download(user),
        "extension_version": _extension_version(),
        **pol,
    }


@router.get("/browser/extension")
def download_extension(user: dict = Depends(get_current_user),
                       act: Activity = trail.records("extension_download",
                                                     "Downloaded the extension")) -> FileResponse:
    """Hand out the built extension to company accounts — no GitHub account needed."""
    _guard()
    if not _may_download(user):
        raise HTTPException(
            status_code=403,
            detail=(
                "The Browser Agent extension is only available to "
                f"{', '.join(sorted(_allowed_domains()))} accounts."
            ),
        )
    if not EXTENSION_ZIP.is_file():
        raise HTTPException(
            status_code=503,
            detail="The extension bundle is missing from this deployment.",
        )
    act.note(f"Downloaded the extension ({_extension_version() or 'unknown version'})")
    return FileResponse(
        EXTENSION_ZIP,
        media_type="application/zip",
        filename="agentos-browser-agent.zip",
    )


@router.get("/browser/tools")
def list_tools(user: dict = Depends(get_current_user)) -> dict:
    """Which APIs the agent can reach right now, and who to share sheets with."""
    _guard()
    ready = browser_tools.availability()
    return {
        "tools": [
            {"name": name, "available": bool(ready.get(name)), "help": spec["help"]}
            for name, spec in browser_tools.TOOLS.items()
        ],
        "service_account": ready["service_account"],
        "default_tab": ready["default_tab"],
        # Stated plainly because it is the whole safety story for sheet writes.
        "write_policy": "append-only into its own tab; existing cells cannot be changed",
    }


@router.post("/browser/runs")
def create_run(body: RunIn, user: dict = Depends(get_current_user),
               act: Activity = trail.records("run_start", "Started a browser run",
                                             unit=JOB)) -> dict:
    _guard()
    run = browser_runs.create_run(user, body.goal, body.mode, body.start_url)
    act.note(f"{body.mode}: {body.goal}", run_id=run["id"])
    return {
        "run_id": run["id"],
        "protocol": browser_actions.PROTOCOL,
        "step_cap": run["step_cap"],
        "allowed": run["allowed"],
        "blocked": run["blocked"],
        "sensitive_confirm": run["sensitive_confirm"],
        "status": run["status"],
        "plan": run["plan"],
        # Named up front so the panel can say "using what I learned last time"
        # before the first step rather than after it.
        "skill": (
            {"name": run["skill"]["name"], "steps": len(run["skill"]["steps"]),
             "match_score": run["skill"].get("match_score")}
            if run.get("skill") else None
        ),
    }


@router.post("/browser/runs/{run_id}/step")
def run_step(run_id: str, body: StepIn, user: dict = Depends(get_current_user),
             act: Activity = trail.records("run_step", "Browser run step")) -> dict:
    _guard()
    run = _run_or_404(run_id, user)
    try:
        run, response = browser_runs.step(run, body.model_dump())
    except browser_runs.ProtocolMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except browser_runs.OutOfSync as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _end_once(run, act)
    return response


@router.post("/browser/runs/{run_id}/stop")
def stop_run(run_id: str, user: dict = Depends(get_current_user),
             act: Activity = trail.records("run_stop", "Stopped a browser run")) -> dict:
    _guard()
    run = _run_or_404(run_id, user)
    run = browser_runs.stop_run(run)
    _end_once(run, act)
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


# --------------------------------- Skills ---------------------------------- #
# Learned routes. Saving is always the user's decision — a run that limped to a
# finish is exactly the one you don't want to repeat forever.


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    # Either learn from a finished run, or from a recording the user performed.
    run_id: str | None = None
    steps: list[dict] | None = None
    goal: str | None = Field(default=None, max_length=300)
    host: str | None = Field(default=None, max_length=120)


@router.get("/browser/skills")
def list_skills(user: dict = Depends(get_current_user)) -> dict:
    _guard()
    user_id = None if user.get("is_admin") else str(user.get("id"))
    return {"skills": browser_skills.list_skills(user_id=user_id)}


@router.post("/browser/skills")
def save_skill(body: SkillIn, user: dict = Depends(get_current_user),
               act: Activity = trail.records("skill_saved", "Saved a skill")) -> dict:
    """Approve a flow so the agent can repeat it without thinking it out again."""
    _guard()
    goal, host, steps = body.goal or "", body.host or "", body.steps or []

    if body.run_id:
        run = _run_or_404(body.run_id, user)
        steps = browser_skills.steps_from_run(run)
        goal = goal or run.get("goal", "")
        # Where the run actually happened. Falling back to start_url matters:
        # a run on an already-open tab issues no navigate, and a host-less
        # skill would then be offered on every site.
        host = host or browser_skills.host_of(
            next((s.get("action", {}).get("url") for s in run.get("steps") or []
                  if (s.get("action") or {}).get("url")), "")
        ) or browser_skills.host_of(run.get("start_url") or "")
    try:
        skill = browser_skills.save_skill(user, body.name, goal, host, steps,
                                          source="run" if body.run_id else "recording")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    act.note(f"Learned: {skill['name']} ({len(skill['steps'])} steps)")
    return skill


@router.get("/browser/skills/{skill_id}")
def get_skill(skill_id: str, user: dict = Depends(get_current_user)) -> dict:
    _guard()
    found = browser_skills.get_skill(skill_id)
    if not found or (found.get("user_id") != str(user.get("id")) and not user.get("is_admin")):
        raise HTTPException(status_code=404, detail="Unknown skill")
    return found


@router.delete("/browser/skills/{skill_id}")
def delete_skill(skill_id: str, user: dict = Depends(get_current_user),
                 act: Activity = trail.records("skill_deleted", "Deleted a skill",
                                               unit=CHANGE)) -> dict:
    _guard()
    found = browser_skills.get_skill(skill_id)
    if not found or (found.get("user_id") != str(user.get("id")) and not user.get("is_admin")):
        raise HTTPException(status_code=404, detail="Unknown skill")
    browser_skills.delete_skill(skill_id)
    act.note(f"Deleted skill: {found.get('name') or skill_id}")
    return {"deleted": skill_id}


# ------------------------------ Tab monitoring ----------------------------- #
# The extension buffers tab events locally and posts them only on demand, so
# watching tabs all day costs nothing until someone asks what happened.


@router.post("/browser/digest")
def make_digest(body: DigestIn, user: dict = Depends(get_current_user),
                act: Activity = trail.records("digest", "Built a tab digest")) -> dict:
    _guard()
    try:
        result = browser_digest.build_digest(user, body.events, body.tabs)
    except ValueError as exc:
        # An unreadable model reply is reported as-is; we never ship a fake digest.
        raise HTTPException(status_code=502, detail=f"Couldn't build the digest: {exc}") from exc
    act.note(result["headline"] or "Tab digest")
    return result


@router.get("/browser/digests")
def list_digests(user: dict = Depends(get_current_user)) -> dict:
    _guard()
    user_id = None if user.get("is_admin") else str(user.get("id"))
    return {"digests": browser_digest.list_digests(user_id=user_id)}


@router.get("/browser/digests/{digest_id}")
def get_digest(digest_id: str, user: dict = Depends(get_current_user)) -> dict:
    _guard()
    found = browser_digest.get_digest(digest_id)
    if not found or (found.get("user_id") != str(user.get("id")) and not user.get("is_admin")):
        raise HTTPException(status_code=404, detail="Unknown digest")
    return found


@router.get("/browser/config")
def get_config(user: dict = Depends(get_current_user)) -> dict:
    """This caller's watch rules — private to them, no admin bypass.

    Unlike runs/skills/digests there is no ``is_admin`` reach here: an admin
    reading the whole deployment's rules through one un-keyed endpoint is the
    defect this used to be, not a feature worth keeping.
    """
    _guard()
    return browser_digest.load_config(browser_digest.tenant_key(user))


@router.put("/browser/config")
def put_config(body: ConfigIn, user: dict = Depends(get_current_user),
               act: Activity = trail.records("config_saved", "Edited the watch rules",
                                             unit=CHANGE)) -> dict:
    _guard()
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    saved = browser_digest.save_config(browser_digest.tenant_key(user), patch)
    act.note(f"Watch rules saved — {len(saved.get('watch_rules') or [])} rules")
    return saved
