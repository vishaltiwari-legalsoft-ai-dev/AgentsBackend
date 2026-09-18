"""Inbox Triage (a12) API — connect one Gmail inbox, point it at a sheet, let
the scheduler do the rest.

Mounted under ``/api/inbox``. Auth: ``GET /status`` is any signed-in user
(it answers ``enabled: false`` to everyone outside ``INBOX_TRIAGE_EMAILS``);
every other route is ``require_inbox_user``; the cron endpoint carries no
auth dependency and checks ``INBOX_CRON_KEY`` in the handler like the other
crons. Every write answers with the same status shape as the GET, so the
panel never composes two responses.

Error contract: 503 when this deployment is not configured for the feature
(OAuth client, token key, offline) — a sentence, never a stack; 400 for a
bad state or an unparseable sheet reference; 502 when Google or the model
refused; 409 when the stored grant is no longer honoured. No route ever
returns a token, and no body carries mail content.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from contextlib import contextmanager
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.config import settings
from app.security import get_current_user, require_inbox_user
from app.services import firestore_repo
from app.services.run_tracking import CHANGE, CRON, JOB, Activity, ActivityTrail, silent
from inbox_triage_agent import InboxOffline, gmail_oauth, pipeline, user_label
from inbox_triage_agent.gmail_client import GmailUnavailable
from inbox_triage_agent.gmail_oauth import (
    BadState, ExchangeFailed, OAuthNotConfigured, RevokedGrant, TokenKeyMissing,
    TokenRefreshFailed,
)
from inbox_triage_agent.sheet_writer import SheetsUnavailable

router = APIRouter()
logger = logging.getLogger("agentos.inbox")

INBOX_AGENT_ID = "a12"
INBOX_AGENT_NAME = "Inbox Triage"

#: Every unit of Inbox Triage work lands here — see THE RULE in run_tracking.py.
trail = ActivityTrail(agent_id=INBOX_AGENT_ID, agent_name=INBOX_AGENT_NAME, category="ops")

#: Wall clock ONE CRON FIRE may spend in total. Cloud Scheduler's attempt
#: deadline is 300s; staying under it keeps a slow fire from being counted
#: as a failed one and re-fired on top of itself.
CRON_BUDGET_DEFAULT_SECONDS = 270.0
#: Below this a user's fire cannot finish even one message; leave it for the
#: next fire rather than charge a partial one.
MIN_USER_SECONDS = 30.0


class OAuthCompleteIn(BaseModel):
    code: str
    state: str


class SheetIn(BaseModel):
    ref: str


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """The agent's exceptions → the honest HTTP status, with the agent's own
    sentence as the detail. Anything not listed is a 500 the catch-all logs."""
    try:
        yield
    except InboxOffline as exc:
        raise HTTPException(status_code=503, detail="Inbox Triage is switched off on this deployment.") from exc
    except (OAuthNotConfigured, TokenKeyMissing) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (BadState, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RevokedGrant as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ExchangeFailed, GmailUnavailable, SheetsUnavailable, TokenRefreshFailed) as exc:
        logger.warning("a12 upstream failure: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _status(user: dict) -> dict:
    return pipeline.status_payload(str(user["id"]), enabled=bool(user.get("is_inbox_user")))


# ------------------------------- the panel --------------------------------

@router.get("/inbox/status")
def status(user: dict = Depends(get_current_user)) -> dict:
    """One read for the whole panel. Not role-gated on purpose: an account
    outside the list gets ``enabled: false`` and the console renders that
    state instead of swallowing a 403."""
    with _mapped_errors():
        return _status(user)


@router.post("/inbox/oauth/start")
@silent("issues a consent URL only; the connection is recorded when oauth/complete lands")
def oauth_start(user: dict = Depends(require_inbox_user)) -> dict:
    """The Google consent URL for THIS caller. 503 with a sentence when the
    OAuth client or the token key is missing — checked here, so nobody is
    sent to Google for a connection the server could not store."""
    with _mapped_errors():
        gmail_oauth.require_token_key()
        url, _state = gmail_oauth.auth_url(str(user["id"]), login_hint=str(user["email"]))
    return {"url": url}


@router.post("/inbox/oauth/complete")
def oauth_complete(
    body: OAuthCompleteIn,
    user: dict = Depends(require_inbox_user),
    act: Activity = trail.records("gmail_connected", "Gmail inbox connected", unit=CHANGE),
) -> dict:
    """The frontend posts Google's ``{code, state}`` here with the caller's
    bearer; the state must have been issued to this same caller."""
    with _mapped_errors():
        pipeline.connect(
            str(user["id"]), code=body.code, state=body.state, email=str(user["email"])
        )
        payload = _status(user)
    act.note("Gmail inbox connected")  # the address is personal data; not in the trail
    return payload


@router.put("/inbox/sheet")
def set_sheet(
    body: SheetIn,
    user: dict = Depends(require_inbox_user),
    act: Activity = trail.records("sheet_set", "Inbox sheet set", unit=CHANGE),
) -> dict:
    with _mapped_errors():
        doc = pipeline.set_sheet(str(user["id"]), body.ref, email=str(user["email"]))
        payload = _status(user)
    sheet = doc.get("sheet") or {}
    act.note(f"Inbox sheet set — check: {sheet.get('check')}")
    return payload


@router.post("/inbox/sheet/check")
def check_sheet(
    user: dict = Depends(require_inbox_user),
    act: Activity = trail.records("sheet_checked", "Inbox sheet re-checked", unit=CHANGE),
) -> dict:
    with _mapped_errors():
        doc = pipeline.recheck_sheet(str(user["id"]), email=str(user["email"]))
        payload = _status(user)
    sheet = doc.get("sheet") or {}
    act.note(f"Inbox sheet re-checked — {sheet.get('check')}")
    return payload


@router.post("/inbox/disconnect")
def disconnect(
    user: dict = Depends(require_inbox_user),
    act: Activity = trail.records("gmail_disconnected", "Gmail inbox disconnected", unit=CHANGE),
) -> dict:
    """Revokes the grant at Google, then deletes the token and tracking.
    ``google_revoked`` (top level, beside the status shape) says whether
    Google confirmed the revoke, so the panel can tell the truth when it
    did not."""
    with _mapped_errors():
        result = pipeline.disconnect(str(user["id"]))
        payload = _status(user)
    act.note(
        "Gmail inbox disconnected — token and tracking deleted, sheet kept; "
        + ("Google permission revoked" if result.google_revoked else "Google revoke NOT confirmed")
    )
    return {**payload, "google_revoked": result.google_revoked}


# --------------------------------- cron -----------------------------------

def _cron_budget_seconds() -> float:
    raw = os.environ.get("INBOX_CRON_BUDGET_SECONDS", "")
    try:
        return max(10.0, min(float(raw), 840.0)) if raw else CRON_BUDGET_DEFAULT_SECONDS
    except ValueError:
        logger.warning("INBOX_CRON_BUDGET_SECONDS=%r is not a number — using default", raw)
        return CRON_BUDGET_DEFAULT_SECONDS


#: One handle for logs and the envelope, shared with the pipeline.
_label = user_label


@router.post("/inbox/cron/poll")
def cron_poll(
    request: Request,
    response: Response,
    act: Activity = trail.records("cron_poll", "Scheduled inbox poll", unit=JOB, actor=CRON),
) -> dict:
    """Every five minutes: one fire per connected user on the list.

    Status is honest, because Cloud Scheduler only reads the code: 200 every
    fire ok, 207 some failed, 502 every one failed. An idle fire — nothing
    connected, nothing new, or the previous fire still holding the lease —
    records no trail row: 288 fires a day must not become 288 rows a day.
    """
    expected = os.environ.get("INBOX_CRON_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="INBOX_CRON_KEY not configured")
    # Bytes, not str: compare_digest raises TypeError on a non-ASCII str,
    # and a header can carry anything — that must be a 403, not a 500.
    presented = request.headers.get("x-cron-key", "")
    if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Bad cron key")

    budget = _cron_budget_seconds()
    deadline = time.monotonic() + budget
    users: list[dict] = []
    unreached = 0
    no_account = 0
    allowed_ids: set[str] = set()
    for email in sorted(settings.inbox_triage_email_set):
        account = firestore_repo.get_user_by_email(email)
        if not account:
            no_account += 1
            continue
        user_id = str(account["id"])
        allowed_ids.add(user_id)
        remaining = deadline - time.monotonic()
        if remaining < MIN_USER_SECONDS:
            unreached += 1
            continue
        try:
            report = pipeline.fire(
                user_id, email=email,
                budget_seconds=min(remaining, pipeline.FIRE_BUDGET_SECONDS),
            )
        except Exception as exc:  # noqa: BLE001 — one user must not kill the sweep
            logger.exception("a12 cron fire failed for user %s", _label(user_id))
            users.append({"user": _label(user_id), "ok": False, "error": str(exc), "skipped": None})
            continue
        users.append({"user": _label(user_id), **report.as_dict()})

    # People taken off INBOX_TRIAGE_EMAILS: revoke and clear their grant, the
    # same as a disconnect, so a re-listing never silently resumes polling.
    # Best effort and budget-bound; a failure here must not fail the poll.
    delisted = 0
    if deadline - time.monotonic() >= MIN_USER_SECONDS:
        try:
            delisted = pipeline.purge_delisted(allowed_ids)
        except Exception:  # noqa: BLE001 — retried on the next fire; logged loudly
            logger.exception("a12 cron could not disconnect de-listed users")

    failed = sum(1 for u in users if not u.get("ok", True))
    skipped = sum(1 for u in users if u.get("skipped"))
    worked = len(users) - failed - skipped
    out: dict = {
        "status": "ok", "ok": worked, "failed": failed, "skipped": skipped,
        "unreached": unreached, "no_account": no_account, "delisted": delisted,
        "budget_seconds": budget, "users": users,
    }
    if users and failed == len(users):
        out["status"] = "failed"
        response.status_code = 502
        logger.error("a12 cron poll FAILED for every user")
    elif failed:
        out["status"] = "partial"
        response.status_code = 207
        logger.warning("a12 cron poll degraded: %d/%d users failed", failed, len(users))
    if unreached:
        logger.warning("a12 cron ran out of budget before %d user(s)", unreached)

    rows = sum(int(u.get("new_rows") or 0) for u in users)
    retried = sum(int(u.get("retried_rows") or 0) for u in users)
    read = sum(int(u.get("messages_read") or 0) for u in users)
    if not failed and not rows and not retried and not read and not delisted:
        reasons = {u.get("skipped") for u in users if u.get("skipped")}
        act.skip(
            "previous fire still running" if reasons == {"previous fire still running"}
            else "idle fire: nothing connected or no new mail"
        )
    else:
        act.note(
            f"Scheduled inbox poll — {rows} rows added, {retried} re-checked, "
            f"{read} messages read, {failed} failed, {delisted} de-listed disconnected",
            status=out["status"],
        )
    return out
