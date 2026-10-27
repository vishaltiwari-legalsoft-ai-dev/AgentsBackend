"""PR Writer agent (a13) API — mounted under ``/api/pr``.

The contract is ``docs/pr-writer-api.md``; this file is its thin HTTP edge.
Every route needs a signed-in person (``get_current_user``) and none is in
the GEO scope, so a GEO-only account is refused by the wall. Runs are
participant-scoped: a non-participant (an admin included) gets the same 404
as for a run that does not exist. Every refusal raised here is coded:
``{"detail": {"code", "message", ...}}``.

Nothing here sends anything outside the service, and no route accepts an
actor in the body: every human act (verify, sign off, approve, record a quote
approval, clear an escalation) is recorded against the request's principal.
The logic lives in ``app/services/pr_writer.py``.
"""
from __future__ import annotations

from typing import Any, Callable, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.security import get_current_user
from app.services import pr_writer as svc
from app.services.run_tracking import CHANGE, JOB, Activity, ActivityTrail

router = APIRouter()

PR_AGENT_ID = "a13"  # "PR Writer" slot in the frontend agent catalog
PR_AGENT_NAME = "PR Writer"

trail = ActivityTrail(agent_id=PR_AGENT_ID, agent_name=PR_AGENT_NAME, category="copy")


def _call(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except svc.PrError as err:
        raise HTTPException(status_code=err.status, detail=err.detail(), headers=err.headers) from err


def _note(act: Activity, run: dict, summary: str, meter: Any = None) -> dict:
    """Record a FIXED summary. Draft text (news, comments, file names) never
    goes into the trail: ``agent_runs__a13`` and ``runs`` are readable by every
    admin through the database panel, and drafts can be embargoed."""
    act.note(summary, brand_id=run.get("brand_id"), brand=run.get("brand_name"), run_id=run.get("id"),
             usage=meter.as_dict() if meter is not None else None)
    return run


# --------------------------------------------------------------------------- #
# Bodies
# --------------------------------------------------------------------------- #

class RunIn(BaseModel):
    brand_id: str = Field(max_length=100)
    news: str = Field(max_length=5000)
    facts: str = Field(default="", max_length=50000)


class CardIn(BaseModel):
    base_revision: int = Field(ge=0)
    card: dict[str, Any]
    boilerplate_approval: Optional[dict[str, Any]] = None


class ClarifyIn(BaseModel):
    mode: Literal["answers", "placeholders"]
    answers: dict[str, str] = Field(default_factory=dict)
    content_type: Optional[str] = None


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=20000)


class VersionIn(BaseModel):
    version: str = Field(max_length=20)


class ReviseIn(VersionIn):
    comment: str = Field(max_length=20000)


class VerifyIn(VersionIn):
    note: str = Field(default="", max_length=20000)
    value: Optional[str] = Field(default=None, max_length=20000)


class AskIn(VersionIn):
    note: str = Field(default="", max_length=20000)


class QuoteApprovalIn(BaseModel):
    evidence: str = Field(default="", max_length=20000)
    text_hash: str = Field(max_length=128)


class ParticipantsIn(BaseModel):
    user_ids: list[str] = Field(max_length=100)


# --------------------------------------------------------------------------- #
# Brands, cards, members
# --------------------------------------------------------------------------- #

@router.get("/pr/brands")
def list_brands(user: dict = Depends(get_current_user)) -> dict:
    return _call(svc.list_brands)


@router.get("/pr/brands/{brand_id}/card")
def get_card(brand_id: str, user: dict = Depends(get_current_user)) -> dict:
    return _call(lambda: svc.card_view(brand_id, user))


@router.put("/pr/brands/{brand_id}/card")
def put_card(brand_id: str, body: CardIn, user: dict = Depends(get_current_user),
             act: Activity = trail.records("card_saved", "Saved a brand card", unit=CHANGE)) -> dict:
    card = _call(lambda: svc.save_card(brand_id, user, body.base_revision, body.card, body.boilerplate_approval))
    act.note(f"Saved the {card['brand_name']} brand card (revision {card['revision']})", brand_id=brand_id,
             brand=card["brand_name"])
    return card


@router.get("/pr/members")
def list_members(user: dict = Depends(get_current_user)) -> dict:
    return {"members": _call(svc.members)}


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #

@router.get("/pr/runs")
def list_runs(brand_id: Optional[str] = Query(default=None, max_length=100),
              user: dict = Depends(get_current_user)) -> dict:
    return _call(lambda: svc.list_runs(user, brand_id))


@router.post("/pr/runs", status_code=201)
def create_run(body: RunIn, user: dict = Depends(get_current_user),
               act: Activity = trail.records("run_created", "New PR draft", unit=JOB)) -> dict:
    meter = svc.new_meter()
    run = _call(lambda: svc.create_run(user, body.brand_id, body.news, body.facts, meter))
    return _note(act, run, "New PR draft", meter)


@router.get("/pr/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    return _call(lambda: svc.get_run(run_id, user))


@router.get("/pr/runs/{run_id}/versions/{version}")
def get_version(run_id: str, version: str, user: dict = Depends(get_current_user)) -> dict:
    return _call(lambda: svc.get_version(run_id, version, user))


@router.post("/pr/runs/{run_id}/steps/{step}")
def run_step(run_id: str, step: str, user: dict = Depends(get_current_user),
             act: Activity = trail.records("step", "PR Writer step")) -> dict:
    meter = svc.new_meter()
    run = _call(lambda: svc.run_step(run_id, step, user, meter))
    return _note(act, run, f"PR Writer {step} step", meter)  # step is one of six fixed ids (404 otherwise)


@router.post("/pr/runs/{run_id}/clarify")
def answer_clarify(run_id: str, body: ClarifyIn, user: dict = Depends(get_current_user),
                   act: Activity = trail.records("clarify", "Answered PR details", unit=CHANGE)) -> dict:
    meter = svc.new_meter()
    run = _call(lambda: svc.clarify(run_id, user, body.mode, body.answers, body.content_type, meter))
    return _note(act, run, f"PR details: {body.mode}", meter)


# --------------------------------------------------------------------------- #
# Escalation
# --------------------------------------------------------------------------- #

def _escalation(run_id: str, user: dict, action: str, note: str, act: Activity, summary: str) -> dict:
    run = _call(lambda: svc.escalation_action(run_id, user, action, note))
    return _note(act, run, summary)


@router.post("/pr/runs/{run_id}/escalation/clear")
def clear_escalation(run_id: str, body: NoteIn, user: dict = Depends(get_current_user),
                     act: Activity = trail.records("escalation_clear", "Cleared an escalation", unit=CHANGE)) -> dict:
    return _escalation(run_id, user, "clear", body.note, act, "Cleared a PR escalation")


@router.post("/pr/runs/{run_id}/escalation/keep-paused")
def keep_paused(run_id: str, body: NoteIn, user: dict = Depends(get_current_user),
                act: Activity = trail.records("escalation_keep", "Kept an escalation paused", unit=CHANGE)) -> dict:
    return _escalation(run_id, user, "keep_paused", body.note, act, "Kept a PR escalation paused")


@router.post("/pr/runs/{run_id}/escalation/close")
def close_escalation(run_id: str, body: NoteIn, user: dict = Depends(get_current_user),
                     act: Activity = trail.records("escalation_close", "Closed a run without drafting",
                                                   unit=CHANGE)) -> dict:
    return _escalation(run_id, user, "close", body.note, act, "Closed a PR request without drafting")


@router.post("/pr/runs/{run_id}/escalation/withdraw")
def withdraw(run_id: str, body: NoteIn, user: dict = Depends(get_current_user),
             act: Activity = trail.records("withdraw", "Withdrew a PR request", unit=CHANGE)) -> dict:
    return _escalation(run_id, user, "withdraw", body.note, act, "Withdrew a PR request")


@router.post("/pr/runs/{run_id}/escalation/notes")
def escalation_note(run_id: str, body: NoteIn, user: dict = Depends(get_current_user),
                    act: Activity = trail.records("escalation_note", "Added a note for reviewers",
                                                  unit=CHANGE)) -> dict:
    return _escalation(run_id, user, "note", body.note, act, "Added a note for PR reviewers")


# --------------------------------------------------------------------------- #
# Draft edits
# --------------------------------------------------------------------------- #

@router.post("/pr/runs/{run_id}/blocks/{block_id}/revise")
def revise_block(run_id: str, block_id: str, body: ReviseIn, user: dict = Depends(get_current_user),
                 act: Activity = trail.records("revise", "Revised a PR draft block")) -> dict:
    meter = svc.new_meter()
    run = _call(lambda: svc.revise(run_id, block_id, user, body.comment, body.version, meter))
    return _note(act, run, "Revised a PR draft block", meter)


@router.post("/pr/runs/{run_id}/claims/{claim_id}/verify")
def verify_claim(run_id: str, claim_id: str, body: VerifyIn, user: dict = Depends(get_current_user),
                 act: Activity = trail.records("verify", "Verified a fact", unit=CHANGE)) -> dict:
    run = _call(lambda: svc.verify_claim(run_id, claim_id, user, body.version, body.note, body.value))
    return _note(act, run, "Verified a fact in a PR draft")


@router.post("/pr/runs/{run_id}/claims/{claim_id}/remove")
def remove_claim(run_id: str, claim_id: str, body: VersionIn, user: dict = Depends(get_current_user),
                 act: Activity = trail.records("remove_claim", "Removed a claim")) -> dict:
    meter = svc.new_meter()
    run = _call(lambda: svc.remove_claim(run_id, claim_id, user, body.version, meter))
    return _note(act, run, "Removed a claim from a PR draft", meter)


# --------------------------------------------------------------------------- #
# Review, sign-off, approval — people only
# --------------------------------------------------------------------------- #

@router.post("/pr/runs/{run_id}/review")
def send_for_review(run_id: str, body: VersionIn, user: dict = Depends(get_current_user),
                    act: Activity = trail.records("review", "Sent a PR draft for review", unit=CHANGE)) -> dict:
    run = _call(lambda: svc.send_for_review(run_id, user, body.version))
    return _note(act, run, "Sent a PR draft for review")


@router.post("/pr/runs/{run_id}/signoffs/{row_id}/sign-off")
def sign_off(run_id: str, row_id: str, body: VersionIn, user: dict = Depends(get_current_user),
             act: Activity = trail.records("sign_off", "Signed off a PR draft", unit=CHANGE)) -> dict:
    run = _call(lambda: svc.signoff(run_id, row_id, user, body.version, "approve", None))
    return _note(act, run, f"Signed off a PR draft as {row_id}")  # row_id validated


@router.post("/pr/runs/{run_id}/signoffs/{row_id}/ask-changes")
def ask_changes(run_id: str, row_id: str, body: AskIn, user: dict = Depends(get_current_user),
                act: Activity = trail.records("ask_changes", "Asked for changes", unit=CHANGE)) -> dict:
    run = _call(lambda: svc.signoff(run_id, row_id, user, body.version, "reject", body.note))
    return _note(act, run, f"Asked for changes on a PR draft as {row_id}")


@router.post("/pr/runs/{run_id}/approve")
def approve(run_id: str, body: VersionIn, user: dict = Depends(get_current_user),
            act: Activity = trail.records("approve", "Marked a PR draft approved", unit=CHANGE)) -> dict:
    run = _call(lambda: svc.approve(run_id, user, body.version))
    return _note(act, run, "Marked a PR draft approved")


@router.post("/pr/runs/{run_id}/quotes/{quote_id}/approval")
def record_quote_approval(run_id: str, quote_id: str, body: QuoteApprovalIn, user: dict = Depends(get_current_user),
                          act: Activity = trail.records("quote_approval", "Recorded a quote approval",
                                                        unit=CHANGE)) -> dict:
    run = _call(lambda: svc.approve_quote(run_id, quote_id, user, body.evidence, body.text_hash))
    return _note(act, run, "Recorded a quote approval")


@router.post("/pr/runs/{run_id}/participants")
def add_participants(run_id: str, body: ParticipantsIn, user: dict = Depends(get_current_user),
                     act: Activity = trail.records("participants", "Added people to a PR draft",
                                                   unit=CHANGE)) -> dict:
    run = _call(lambda: svc.add_participants(run_id, user, body.user_ids))
    return _note(act, run, "Added people to a PR draft")


@router.get("/pr/runs/{run_id}/export")
def export_run(run_id: str, format: str = "docx", v: Optional[str] = Query(default=None, max_length=20),
               user: dict = Depends(get_current_user),
               act: Activity = trail.records("export", "Exported a PR draft")) -> Response:
    data, media, name = _call(lambda: svc.export_run(run_id, user, format, v))
    act.note("Exported a PR draft", action=f"export:{format}", run_id=run_id)  # format validated; no filename (it carries the news)
    return Response(content=data, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
