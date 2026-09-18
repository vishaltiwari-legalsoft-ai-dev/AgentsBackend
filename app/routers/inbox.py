"""Inbox Triage (a12) API — connect one Gmail inbox, point it at a sheet, let
the scheduler do the rest.

Mounted under ``/api/inbox``. Auth: every panel route is any signed-in user
(``get_current_user``) acting on their own ``inbox_triage/{user id}`` — a12 is
open to everyone with platform access. A GEO-only account is refused on all
of them by the scope wall (``deny_outside_geo``, attached at include time).
The cron endpoint carries no auth dependency and checks ``INBOX_CRON_KEY`` in
the handler like the other crons. Every write answers with the same status shape as the GET, so the
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

from app.routers.auth import is_allowed_email
from app.security import get_current_user, is_geo_only
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
#: The least a reached user's fire is given when the budget is split. With
#: many backlogged users this caps how many one fire reaches; the rotation in
#: :func:`_round_robin` is what makes sure the ones it did not reach go first
#: on a later fire.
FAIR_SHARE_FLOOR_SECONDS = 60.0
#: Wall clock the disconnect of users who lost access may take, before any
#: polling. Each is a Google revoke call; they must not eat the poll.
PURGE_BUDGET_SECONDS = 30.0


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
    return pipeline.status_payload(str(user["id"]))


# ------------------------------- the panel --------------------------------

@router.get("/inbox/status")
def status(user: dict = Depends(get_current_user)) -> dict:
    """One read for the whole panel: the caller's own document, ``enabled``
    always true (see :func:`pipeline.status_payload`)."""
    with _mapped_errors():
        return _status(user)


@router.post("/inbox/oauth/start")
@silent("issues a consent URL only; the connection is recorded when oauth/complete lands")
def oauth_start(user: dict = Depends(get_current_user)) -> dict:
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
    user: dict = Depends(get_current_user),
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
    user: dict = Depends(get_current_user),
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
    user: dict = Depends(get_current_user),
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
    user: dict = Depends(get_current_user),
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


#: The cron's clock — a module name so a test can drive the budget split
#: without patching ``time`` for the whole process.
_monotonic = time.monotonic

#: One handle for logs and the envelope, shared with the pipeline.
_label = user_label


def _has_access(email: str) -> bool:
    """Whether the account behind a connection could still reach a12 today —
    the cron's mirror of what a request has to pass, built from the SAME two
    rules rather than a third copy: ``auth.is_allowed_email`` (what sign-in
    admits, and what ``get_current_user`` re-applies on every request) and
    ``security.is_geo_only`` (what ``deny_outside_geo`` refuses on every a12
    route)."""
    return is_allowed_email(email) and not is_geo_only(email)


def _rotation_offset(count: int, now: float | None = None) -> int:
    """Which connected user goes first this fire. Advances by one every poll
    interval, so over ``count`` fires every user leads once — no user is
    always last behind the same heavy backfill. Stateless on purpose: a
    persisted cursor would be one more write per fire to keep a rotation."""
    if not count:
        return 0
    return int((time.time() if now is None else now) // pipeline.POLL_INTERVAL_SECONDS) % count


def _round_robin(items: list) -> list:
    offset = _rotation_offset(len(items))
    return items[offset:] + items[:offset]


@router.post("/inbox/cron/poll")
def cron_poll(
    request: Request,
    response: Response,
    act: Activity = trail.records("cron_poll", "Scheduled inbox poll", unit=JOB, actor=CRON),
) -> dict:
    """Every five minutes: one fire per connected user who still has access.

    Who: every connection with Gmail connected, each resolved to its user
    record for the CURRENT address (the ownership and mailbox checks run
    against it). A connection whose user was deleted, is no longer admitted
    at sign-in, or is confined to GEO is disconnected — revoked and cleared,
    as a disconnect — and never polled.

    Fairness: the budget is split, not first-come. Each user's fire gets an
    equal share of what is left (at least ``FAIR_SHARE_FLOOR_SECONDS``), so a
    huge backfill cannot take the whole fire; what an idle user leaves is
    re-split among the rest; and the starting user rotates every fire, so the
    ones a crowded fire could not reach lead later ones.

    Status is honest, because Cloud Scheduler only reads the code: 200 every
    fire ok, 207 some failed, 502 every one failed, 503 when who-is-connected
    could not be read at all. An idle fire — nothing connected, nothing new,
    or the previous fire still holding the lease — records no trail row: 288
    fires a day must not become 288 rows a day.
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
    deadline = _monotonic() + budget

    # Who, in two reads (the connections, then every user record in one
    # batch). A failure here fails the fire closed: neither polling nor
    # disconnecting on a guess about who still has access.
    eligible: list[tuple[str, str]] = []
    lost: list[str] = []
    try:
        connected = sorted(set(firestore_repo.list_connected_inbox_user_ids()))
        accounts = firestore_repo.get_users_by_ids(connected)
        for user_id in connected:
            email = str((accounts.get(user_id) or {}).get("email") or "").strip().lower()
            if email and _has_access(email):
                eligible.append((user_id, email))
            else:
                lost.append(user_id)
    except Exception as exc:  # noqa: BLE001 — reported as a failed fire, not a crash
        logger.exception("a12 cron could not resolve connected users")
        raise HTTPException(
            status_code=503,
            detail="Inbox Triage could not read who is connected; nothing was polled.",
        ) from exc

    # Lost access: revoke and clear their grant, the same as a disconnect, so
    # regaining access never silently resumes polling. First, on its own small
    # budget, so it can neither be starved by the poll nor starve it. A
    # failure here must not fail the poll; the next fire finds them again.
    disconnected = 0
    if lost:
        try:
            disconnected = pipeline.purge_without_access(
                lost, deadline=min(deadline, _monotonic() + PURGE_BUDGET_SECONDS)
            )
        except Exception:  # noqa: BLE001 — retried on the next fire; logged loudly
            logger.exception("a12 cron could not disconnect users who lost access")

    users: list[dict] = []
    unreached = 0
    order = _round_robin(eligible)
    for index, (user_id, email) in enumerate(order):
        remaining = deadline - _monotonic()
        if remaining < MIN_USER_SECONDS:
            unreached = len(order) - index
            break
        share = max(FAIR_SHARE_FLOOR_SECONDS, remaining / (len(order) - index))
        try:
            report = pipeline.fire(
                user_id, email=email,
                budget_seconds=min(share, remaining, pipeline.FIRE_BUDGET_SECONDS),
            )
        except Exception as exc:  # noqa: BLE001 — one user must not kill the sweep
            logger.exception("a12 cron fire failed for user %s", _label(user_id))
            users.append({"user": _label(user_id), "ok": False, "error": str(exc), "skipped": None})
            continue
        users.append({"user": _label(user_id), **report.as_dict()})

    failed = sum(1 for u in users if not u.get("ok", True))
    skipped = sum(1 for u in users if u.get("skipped"))
    worked = len(users) - failed - skipped
    out: dict = {
        "status": "ok", "ok": worked, "failed": failed, "skipped": skipped,
        "unreached": unreached, "no_access": len(lost), "disconnected": disconnected,
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
        logger.warning("a12 cron ran out of budget before %d user(s); they lead a later fire", unreached)

    rows = sum(int(u.get("new_rows") or 0) for u in users)
    retried = sum(int(u.get("retried_rows") or 0) for u in users)
    read = sum(int(u.get("messages_read") or 0) for u in users)
    if not failed and not rows and not retried and not read and not disconnected:
        reasons = {u.get("skipped") for u in users if u.get("skipped")}
        act.skip(
            "previous fire still running" if reasons == {"previous fire still running"}
            else "idle fire: nothing connected or no new mail"
        )
    else:
        act.note(
            f"Scheduled inbox poll — {rows} rows added, {retried} re-checked, "
            f"{read} messages read, {failed} failed, "
            f"{disconnected} disconnected for lost access",
            status=out["status"],
        )
    return out
