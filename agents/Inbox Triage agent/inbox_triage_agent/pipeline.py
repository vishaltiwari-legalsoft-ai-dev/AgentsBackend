"""The connection's lifecycle and the per-user fire.

Everything that reads or writes ``inbox_triage/{user_id}`` lives here, so the
document's shape has one author. The router maps exceptions to statuses and
nothing else.

One fire, in order:

1. Load the connection and take the lease (one transaction). Not connected,
   no sheet, or a sheet check that is not ``ok`` → skip. The sheet is
   re-checked — ownership included — when the last check failed, is over an
   hour old, or was made for a different address than the one firing.
2. Open the sealed refresh token and mint an access token. ``invalid_grant``
   → the connection is marked revoked with the reason, and the fire stops.
3. **New mail first**: history since the checkpoint (or, when Gmail no longer
   holds that history, a re-listing of the last day), fetched, summarised,
   collected as rows.
4. Then the ``needs_review`` rows due for another try, then the **backfill**
   with whatever budget is left: the 90-day inbox listing newest-first, up to
   200 messages per fire, skipping ids already on the sheet.
5. Write: one append for the new rows, one batch update for the retried rows.
6. Only then persist: per-message tracking, the counters, ``last_poll`` and
   the new checkpoint.

Write-then-persist is duplicate-over-drop: a fire that dies between 5 and 6
re-reads the same mail next time and the sheet's id map skips it, so nothing
is lost and nothing doubles. A ``lease_until`` on the document keeps an
overrunning fire and the next one from working the same inbox at once.

What fails the fire loudly with no rows written: no model key, a revoked
grant, Gmail or Sheets refusing, or the model failing three messages in a
row. What does not: one message the model cannot read — that gets its row as
``needs_review`` and up to three later tries.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError

from app.services import firestore_repo

from . import gmail_client, gmail_oauth, sheet_writer, summarise, user_label
from .gmail_client import GmailUnavailable, HistoryExpired, MessageGone
from .gmail_oauth import ExchangeFailed, RevokedGrant, TokenKeyMissing, TokenRefreshFailed
from .sheet_layout import RowFacts, agent_values, parse_sheet_ref, sheet_url
from .sheet_writer import CHECK_MR_SOURCE, CHECK_OK, SheetCheck, SheetsUnavailable
from .summarise import ModelCallFailed, ModelUnavailable
from .triage import NEEDS_REVIEW, Rejected, Verdict

logger = logging.getLogger("agentos.inbox.pipeline")

BACKFILL_DAYS = 90
#: Messages one fire will pull from the backfill, after new mail is done.
BACKFILL_PER_FIRE = 200
#: ``needs_review`` rows one fire will re-ask, so a day the model was down
#: cannot starve new mail for the rest of the week.
RETRIES_PER_FIRE = 50
#: Later fires a ``needs_review`` message is re-asked on.
MAX_RETRIES = 3
#: Consecutive model-call failures that mean "the model is down", not "this
#: email is odd" — the fire is abandoned before anything is written.
MODEL_FAILURE_TRIP = 3
#: Wall clock for one user's fire. The scheduler's attempt deadline is 300s;
#: the cron endpoint keeps its whole request under ~270s.
FIRE_BUDGET_SECONDS = 240.0
#: Kept back from the budget for the two writes and the persist at the end.
WRITE_RESERVE_SECONDS = 20.0
LEASE_SECONDS = 300
POLL_INTERVAL_SECONDS = 300
SHEET_RECHECK_SECONDS = 3600
#: When the history checkpoint is unusable: re-list from the last poll, less
#: a day, and let the id map drop what is already on the sheet.
FALLBACK_LOOKBACK_SECONDS = 86400
FALLBACK_PAGES = 5
RECENT_FIRES_WINDOW_SECONDS = 86400

BACKFILL_NOT_STARTED = "not_started"
BACKFILL_RUNNING = "running"
BACKFILL_DONE = "done"

STATUS_OK = "ok"
STATUS_NEEDS_REVIEW = "needs_review"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Lifecycle — connect, sheet, disconnect, status
# --------------------------------------------------------------------------- #

def _fresh_backfill(now: datetime) -> dict:
    return {
        "state": BACKFILL_NOT_STARTED,
        "cursor": None,
        "done": 0,
        "total": None,
        # Fixed at connect time so every page of the listing sees one window.
        "since_epoch": int((now - timedelta(days=BACKFILL_DAYS)).timestamp()),
    }


def _same_address(a: str, b: str) -> bool:
    return bool(a) and bool(b) and a.strip().lower() == b.strip().lower()


def connect(user_id: str, *, code: str, state: str, email: str) -> dict:
    """Finish the consent: read the profile for the address and the history
    checkpoint, prove the mailbox is the caller's own, then seal the token
    and store the connection. Reads no mail.

    ``email`` is the caller's verified sign-in address. A grant for any other
    mailbox is revoked at Google (best effort) and refused before anything is
    sealed or saved; the refusal never names the other mailbox."""
    gmail_oauth.require_token_key()  # fail before Google is involved
    tokens = gmail_oauth.complete(user_id, code=code, state=state)
    creds = gmail_oauth.credentials(tokens.refresh_token, access_token=tokens.access_token)
    profile = gmail_client.profile(gmail_client.service(creds))
    if not _same_address(str(profile.get("email") or ""), email):
        gmail_oauth.revoke(tokens.refresh_token)  # do not leave a stray grant behind
        logger.warning("a12: connect refused for user %s — the mailbox is not the caller's", user_label(user_id))
        raise ExchangeFailed(
            "That Google account is not the one you are signed in with — "
            f"connect the mailbox for {email} instead."
        )
    sealed = gmail_oauth.seal(tokens.refresh_token)
    now = _utcnow()
    doc = firestore_repo.save_inbox_connection(user_id, {
        "gmail": {
            "connected": True,
            "address": profile["email"],
            "connected_at": _iso(now),
            "revoked_at": None,
        },
        "refresh_token_enc": sealed,
        "checkpoint": {"history_id": profile["history_id"], "updated_at": _iso(now)},
        "backfill": _fresh_backfill(now),
        "needs_review": 0,
        "recent_fires": [],
        "last_poll": None,
        "lease_until": None,
    })
    return _activate_backfill(user_id, doc)


def set_sheet(user_id: str, ref: str, *, email: str) -> dict:
    spreadsheet_id = parse_sheet_ref(ref)
    if not spreadsheet_id:
        raise ValueError("That does not look like a Google Sheet link or id.")
    return _check_and_store(user_id, spreadsheet_id, email=email)


def recheck_sheet(user_id: str, *, email: str) -> dict:
    doc = firestore_repo.get_inbox_connection(user_id) or {}
    spreadsheet_id = (doc.get("sheet") or {}).get("id")
    if not spreadsheet_id:
        raise ValueError("No sheet has been set yet.")
    return _check_and_store(user_id, spreadsheet_id, email=email)


def is_mr_sheet(spreadsheet_id: str) -> bool:
    """Is this Marketing Research's primary tracker or a sheet connected to
    it? Read through MR's own config and registry — this agent keeps no copy.
    A registry that cannot be read is not a "no": the check fails loudly and
    nothing is written."""
    try:
        from marketing_research_agent import config as mr_config
        from marketing_research_agent import sources_registry

        if spreadsheet_id == str(mr_config.SHEETS_SPREADSHEET_ID or ""):
            return True
        return sources_registry.find_source(spreadsheet_id) is not None
    except Exception as exc:  # noqa: BLE001 — fail closed, with the reason logged
        logger.error("a12: could not read the MR sheet registry: %s", type(exc).__name__)
        raise SheetsUnavailable(
            "Could not confirm this sheet is not a Marketing Research tracker; nothing was written."
        ) from exc


def _check_and_store(user_id: str, spreadsheet_id: str, *, email: str) -> dict:
    """MR's sheets are refused before Google is asked anything; every other
    sheet goes through ``sheet_writer.check``, which proves the caller owns
    or edits it before its first write. ``checked_for`` records whose
    address the answer holds for, so a fire for anyone else re-checks."""
    if is_mr_sheet(spreadsheet_id):
        result = SheetCheck(CHECK_MR_SOURCE, "")
    else:
        result = sheet_writer.check(spreadsheet_id, caller_email=email)
    doc = firestore_repo.save_inbox_connection(user_id, {
        "sheet": {
            "id": spreadsheet_id,
            "url": sheet_url(spreadsheet_id),
            "title": result.title if result.status == CHECK_OK else "",
            "check": result.status,
            "checked_at": _iso(_utcnow()),
            "checked_for": str(email or "").strip().lower(),
        },
    })
    return _activate_backfill(user_id, doc)


def _activate_backfill(user_id: str, doc: dict) -> dict:
    """Gmail connected and the sheet ok → the backfill is ``running`` so the
    panel says so now; the cron does the work."""
    connected = bool((doc.get("gmail") or {}).get("connected"))
    sheet_ok = (doc.get("sheet") or {}).get("check") == CHECK_OK
    backfill = dict(doc.get("backfill") or _fresh_backfill(_utcnow()))
    if connected and sheet_ok and backfill.get("state", BACKFILL_NOT_STARTED) == BACKFILL_NOT_STARTED:
        backfill["state"] = BACKFILL_RUNNING
        doc = firestore_repo.save_inbox_connection(user_id, {"backfill": backfill})
    return doc


@dataclass(frozen=True)
class Disconnected:
    doc: dict
    #: ``True`` only when Google confirmed the grant is revoked. ``False``
    #: covers offline, unreachable, refused, and "no usable token was stored".
    google_revoked: bool


def _revoke_stored_grant(doc: dict) -> bool:
    sealed = doc.get("refresh_token_enc")
    if not sealed:
        return False
    try:
        plain = gmail_oauth.open_(str(sealed))
    except (RevokedGrant, TokenKeyMissing):
        return False
    return gmail_oauth.revoke(plain)


def disconnect(user_id: str) -> Disconnected:
    """Revoke the grant at Google (best effort), then delete the hub's
    records and the token — whatever Google answered — and keep the sheet
    reference. The result says whether Google confirmed the revoke, so the
    panel never claims a permission was removed when it was not."""
    revoked = _revoke_stored_grant(firestore_repo.get_inbox_connection(user_id) or {})
    firestore_repo.delete_inbox_messages(user_id)
    doc = firestore_repo.save_inbox_connection(
        user_id,
        {
            "needs_review": 0,
            "recent_fires": [],
            "last_poll": None,
            "lease_until": None,
            "backfill": _fresh_backfill(_utcnow()),
        },
        clear=("refresh_token_enc", "gmail", "checkpoint"),
    )
    return Disconnected(doc=doc, google_revoked=revoked)


def purge_without_access(user_ids: list[str], *, deadline: float) -> int:
    """Disconnect — revoke and clear, exactly as :func:`disconnect` — each of
    ``user_ids``: connections whose user the caller found to have lost access
    to a12 (deleted, no longer admitted at sign-in, or confined to GEO). Their
    token must not sit sealed and silently resume polling if access ever
    returns. Stops at ``deadline`` (a ``time.monotonic()`` value); the rest are
    still connected, so the next fire finds them again. Returns how many were
    disconnected."""
    purged = 0
    for user_id in user_ids:
        if time.monotonic() >= deadline:
            break
        result = disconnect(user_id)
        purged += 1
        logger.warning(
            "a12: disconnected user %s who no longer has access (google_revoked=%s)",
            user_label(user_id), result.google_revoked,
        )
    return purged


def rows_in_window(recent_fires: list, now: datetime) -> int:
    floor = now - timedelta(seconds=RECENT_FIRES_WINDOW_SECONDS)
    total = 0
    for entry in recent_fires or []:
        at = _parse_iso((entry or {}).get("at"))
        if at and at >= floor:
            total += int((entry or {}).get("rows") or 0)
    return total


def _trim_recent(recent_fires: list, now: datetime) -> list[dict]:
    floor = now - timedelta(seconds=RECENT_FIRES_WINDOW_SECONDS)
    kept = []
    for entry in recent_fires or []:
        at = _parse_iso((entry or {}).get("at"))
        if at and at >= floor:
            kept.append({"at": (entry or {}).get("at"), "rows": int((entry or {}).get("rows") or 0)})
    return kept


def status_payload(user_id: str, *, now: datetime | None = None) -> dict:
    """What the panel renders, in one read of the caller's own document.
    Every key is present for every caller — a user who never connected gets
    nulls. ``enabled`` is always true: any signed-in user who reaches this may
    use a12 (a GEO-only account is refused by the scope wall before it gets
    here). The field stays because the console reads it. The sealed token is
    never here."""
    now = now or _utcnow()
    doc = firestore_repo.get_inbox_connection(user_id) or {}
    gmail = doc.get("gmail") or {}
    sheet = doc.get("sheet") or {}
    backfill = doc.get("backfill") or {}
    last_poll = doc.get("last_poll") or {}
    connected = bool(gmail.get("connected"))
    last_at = _parse_iso(last_poll.get("at"))
    next_poll = None
    if connected:
        next_poll = _iso(last_at + timedelta(seconds=POLL_INTERVAL_SECONDS)) if last_at else _iso(now)
    return {
        "enabled": True,
        "service_account_email": sheet_writer.service_account_email(),
        "gmail": {
            "connected": connected,
            "address": gmail.get("address") or None,
            "connected_at": gmail.get("connected_at") or None,
        },
        "sheet": {
            "id": sheet.get("id") or None,
            "url": sheet.get("url") or None,
            "title": sheet.get("title") or None,
            "check": sheet.get("check") or None,
            "checked_at": sheet.get("checked_at") or None,
        },
        "backfill": {
            "state": backfill.get("state") or None,
            "done": int(backfill.get("done") or 0),
            "total": backfill.get("total"),
        },
        "last_poll": {
            "at": last_poll.get("at") or None,
            "ok": last_poll.get("ok"),
            "messages_read": int(last_poll.get("messages_read") or 0),
            "error": last_poll.get("error") or None,
        },
        "next_poll_at": next_poll,
        "rows_24h": rows_in_window(doc.get("recent_fires") or [], now),
        "needs_review": int(doc.get("needs_review") or 0),
        "generated_at": _iso(now),
    }


# --------------------------------------------------------------------------- #
# The fire
# --------------------------------------------------------------------------- #

@dataclass
class FireReport:
    """What one user's fire did, in counts. No addresses, no content."""

    user_id: str
    ok: bool = True
    skipped: str | None = None
    error: str | None = None
    new_rows: int = 0
    retried_rows: int = 0
    messages_read: int = 0
    needs_review_added: int = 0
    backfill_state: str | None = None
    backfill_done: int = 0
    backfill_total: int | None = None
    unreached: bool = False
    seconds: float = 0.0

    @property
    def idle(self) -> bool:
        return self.ok and self.skipped is None and not (
            self.messages_read or self.new_rows or self.retried_rows
        )

    def as_dict(self) -> dict:
        out = asdict(self)
        out.pop("user_id")
        return out


@dataclass
class _Work:
    """The state one fire accumulates before it writes anything."""

    user_id: str
    gmail: object
    llm: object
    deadline: float
    id_map: dict[str, int] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)
    new_rows: list[RowFacts] = field(default_factory=list)
    updated_rows: dict[int, RowFacts] = field(default_factory=dict)
    retry_docs: dict[str, dict] = field(default_factory=dict)
    messages_read: int = 0
    needs_review_added: int = 0
    recovered: int = 0
    model_failures: int = 0
    unreached: bool = False

    def out_of_time(self) -> bool:
        if time.monotonic() >= self.deadline:
            self.unreached = True
            return True
        return False

    def take(self, message_id: str) -> bool:
        """Claim an id for this fire; false if the sheet or this fire has it."""
        if not message_id or message_id in self.id_map or message_id in self.seen:
            return False
        self.seen.add(message_id)
        return True

    def triage(self, message_id: str) -> RowFacts | None:
        """Fetch and summarise one message; ``None`` if it no longer exists."""
        try:
            message = gmail_client.fetch(self.gmail, message_id)
        except MessageGone:
            return None
        self.messages_read += 1
        try:
            verdict: Verdict | Rejected = summarise.summarise(message, llm=self.llm)
            self.model_failures = 0
        except ModelCallFailed as exc:
            self.model_failures += 1
            if self.model_failures >= MODEL_FAILURE_TRIP:
                raise ModelUnavailable(
                    f"The model failed on {MODEL_FAILURE_TRIP} messages in a row; "
                    "this poll was abandoned and nothing was written."
                ) from exc
            verdict = Rejected(str(exc))
        return _facts(message, verdict)

    def add_new(self, facts: RowFacts) -> None:
        self.new_rows.append(facts)
        if facts.category == NEEDS_REVIEW:
            self.needs_review_added += 1


def _facts(message: gmail_client.Message, verdict: Verdict | Rejected) -> RowFacts:
    if isinstance(verdict, Verdict):
        return RowFacts(
            message_id=message.id, received_at=message.received_at, sender=message.from_,
            subject=message.subject, category=verdict.category, summary=verdict.summary,
            deadline=verdict.deadline,
        )
    return RowFacts(
        message_id=message.id, received_at=message.received_at, sender=message.from_,
        subject=message.subject, category=NEEDS_REVIEW, summary="", deadline=None,
    )


def _message_doc(facts: RowFacts, *, attempts: int, sheet_row: int | None) -> dict:
    status = STATUS_NEEDS_REVIEW if facts.category == NEEDS_REVIEW else STATUS_OK
    return {
        "status": status,
        "attempts": attempts,
        "sheet_row": sheet_row,
        "retry_due": status == STATUS_NEEDS_REVIEW and attempts < 1 + MAX_RETRIES,
    }


def fire(
    user_id: str, *, email: str, budget_seconds: float = FIRE_BUDGET_SECONDS,
    now: datetime | None = None,
) -> FireReport:
    """One user's poll. Never raises for a reason the panel should show; those
    land in ``last_poll`` and in the report. A programming error propagates.

    ``email`` is the user's current address from their user record: the
    sheet's ownership is re-proved against it whenever the sheet is
    re-checked."""
    started = time.monotonic()
    now = now or _utcnow()
    report = FireReport(user_id=user_id)
    doc = firestore_repo.get_inbox_connection(user_id)
    if not doc or not (doc.get("gmail") or {}).get("connected"):
        report.skipped = "gmail not connected"
        return report
    # Read-and-take in one transaction: two overlapping fires cannot both see
    # the lease free. The document it returns is the one the fire works from.
    leased = firestore_repo.take_inbox_lease(
        user_id, now=now, until=now + timedelta(seconds=LEASE_SECONDS)
    )
    if leased is None:
        report.skipped = "previous fire still running"
        return report
    doc = leased
    try:
        _fire_leased(user_id, doc, report, email=email, deadline=started + budget_seconds, now=now)
    except RevokedGrant as exc:
        _mark_revoked(user_id, doc, str(exc), now)
        report.ok, report.error = False, str(exc)
    except (GmailUnavailable, SheetsUnavailable, ModelUnavailable, TokenRefreshFailed, HttpError) as exc:
        reason = _panel_reason(exc)
        logger.warning("a12 fire for user %s failed: %s", user_label(user_id), reason)
        firestore_repo.save_inbox_connection(user_id, {
            "last_poll": {"at": _iso(now), "ok": False, "messages_read": 0, "error": reason},
        })
        report.ok, report.error = False, reason
    finally:
        try:
            firestore_repo.save_inbox_connection(user_id, {"lease_until": None})
        except Exception:  # noqa: BLE001 — the lease expires on its own; say so
            logger.exception("a12: could not clear the fire lease for user %s", user_label(user_id))
        report.seconds = round(time.monotonic() - started, 1)
    return report


def _panel_reason(exc: BaseException) -> str:
    """The sentence ``last_poll`` and the cron envelope carry. A raw
    ``HttpError`` (a seam that let one through) is reduced to its status:
    its ``str`` embeds the request URL, and with it the spreadsheet id."""
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        return f"Google refused a Sheets request: HTTP {status}. Re-check the sheet on the panel."
    return str(exc)


def _fire_leased(
    user_id: str, doc: dict, report: FireReport, *, email: str, deadline: float, now: datetime
) -> None:
    doc = _ensure_sheet_checked(user_id, doc, now, email=email)
    sheet = doc.get("sheet") or {}
    if not sheet.get("id"):
        report.skipped = "no sheet set"
        return
    if sheet.get("check") != CHECK_OK:
        report.skipped = f"sheet check: {sheet.get('check')}"
        return

    creds = _credentials(doc)
    llm = summarise.build_llm()  # no key → loud, before any mail is read
    gmail = gmail_client.service(creds)
    sheets = sheet_writer.service()
    spreadsheet_id = str(sheet["id"])
    work = _Work(user_id=user_id, gmail=gmail, llm=llm, deadline=deadline - WRITE_RESERVE_SECONDS)
    work.id_map = sheet_writer.id_rows(spreadsheet_id, svc=sheets)

    # 3. new mail first
    new_ids, new_history_id = _new_mail_ids(gmail, doc, now)
    checkpoint_advances = True
    for message_id in new_ids:
        if work.out_of_time():
            # Unprocessed ids would be lost with the checkpoint; keep the old
            # one and let the id map skip the processed ones next fire.
            checkpoint_advances = False
            break
        if not work.take(message_id):
            continue
        facts = work.triage(message_id)
        if facts:
            work.add_new(facts)

    # 4a. rows due another try
    due = firestore_repo.list_inbox_messages(user_id, retry_due=True)[:RETRIES_PER_FIRE]
    _retry(work, due, now)

    # 4b. the backfill, with what is left
    backfill = dict(doc.get("backfill") or _fresh_backfill(now))
    if backfill.get("state", BACKFILL_NOT_STARTED) == BACKFILL_NOT_STARTED:
        backfill["state"] = BACKFILL_RUNNING
    if backfill.get("state") == BACKFILL_RUNNING:
        _backfill(work, backfill, now)

    # 5. write — one append, one batch update
    row_numbers = sheet_writer.append(
        spreadsheet_id, [agent_values(f) for f in work.new_rows], svc=sheets
    )
    sheet_writer.update(
        spreadsheet_id, {row: agent_values(f) for row, f in work.updated_rows.items()}, svc=sheets
    )

    # 6. persist — tracking rows, counters, the poll, the checkpoint
    _persist(
        user_id, doc, work, backfill=backfill, row_numbers=row_numbers,
        checkpoint=new_history_id if checkpoint_advances else None, now=now,
    )
    _fill_report(report, work, backfill)


def _persist(
    user_id: str, doc: dict, work: _Work, *, backfill: dict, row_numbers: list[int],
    checkpoint: str | None, now: datetime,
) -> None:
    """Step 6, after the sheet has the rows: what the next fire and the panel
    read. ``checkpoint`` is ``None`` when the new-mail pass was cut short, so
    the old one stays and the id map does the de-duplication next time."""
    message_docs: dict[str, dict] = {}
    for index, facts in enumerate(work.new_rows):
        row = row_numbers[index] if index < len(row_numbers) else None
        message_docs[facts.message_id] = _message_doc(facts, attempts=1, sheet_row=row)
    message_docs.update(work.retry_docs)
    if message_docs:
        firestore_repo.save_inbox_messages(user_id, message_docs)

    recent = _trim_recent(doc.get("recent_fires") or [], now)
    if work.new_rows:
        recent.append({"at": _iso(now), "rows": len(work.new_rows)})
    patch: dict = {
        "backfill": backfill,
        "needs_review": max(
            0, int(doc.get("needs_review") or 0) + work.needs_review_added - work.recovered
        ),
        "recent_fires": recent,
        "last_poll": {
            "at": _iso(now), "ok": True, "messages_read": work.messages_read, "error": None,
        },
    }
    if checkpoint:
        patch["checkpoint"] = {"history_id": checkpoint, "updated_at": _iso(now)}
    firestore_repo.save_inbox_connection(user_id, patch)


def _fill_report(report: FireReport, work: _Work, backfill: dict) -> None:
    report.new_rows = len(work.new_rows)
    report.retried_rows = len(work.updated_rows)
    report.messages_read = work.messages_read
    report.needs_review_added = work.needs_review_added
    report.backfill_state = backfill.get("state")
    report.backfill_done = int(backfill.get("done") or 0)
    report.backfill_total = backfill.get("total")
    report.unreached = work.unreached


def _ensure_sheet_checked(user_id: str, doc: dict, now: datetime, *, email: str) -> dict:
    """Re-check — MR refusal and ownership included — when the last check
    failed, is over an hour old, or was made for another address (including
    a document written before ``checked_for`` existed). Otherwise the stored
    ``ok`` stands: one Drive read an hour, not one per five-minute fire."""
    sheet = doc.get("sheet") or {}
    if not sheet.get("id"):
        return doc
    checked_at = _parse_iso(sheet.get("checked_at"))
    stale = checked_at is None or (now - checked_at).total_seconds() > SHEET_RECHECK_SECONDS
    same_caller = sheet.get("checked_for") == str(email or "").strip().lower()
    if sheet.get("check") == CHECK_OK and not stale and same_caller:
        return doc
    return _check_and_store(user_id, str(sheet["id"]), email=email)


def _credentials(doc: dict):
    sealed = doc.get("refresh_token_enc")
    if not sealed:
        raise RevokedGrant("No Gmail token is stored for this connection — connect again.")
    creds = gmail_oauth.credentials(gmail_oauth.open_(str(sealed)))
    gmail_oauth.refresh(creds)
    return creds


def _mark_revoked(user_id: str, doc: dict, reason: str, now: datetime) -> None:
    """The grant is gone: say so on the document, drop the token, keep the
    address so the panel can name what was disconnected."""
    gmail = dict(doc.get("gmail") or {})
    gmail.update({"connected": False, "revoked_at": _iso(now)})
    firestore_repo.save_inbox_connection(
        user_id,
        {
            "gmail": gmail,
            "last_poll": {"at": _iso(now), "ok": False, "messages_read": 0, "error": reason},
        },
        clear=("refresh_token_enc",),
    )


def _last_seen_epoch(doc: dict, now: datetime) -> int:
    for value in (
        (doc.get("last_poll") or {}).get("at"),
        (doc.get("checkpoint") or {}).get("updated_at"),
        (doc.get("gmail") or {}).get("connected_at"),
    ):
        moment = _parse_iso(value)
        if moment:
            return int(moment.timestamp())
    return int(now.timestamp())


def _new_mail_ids(gmail, doc: dict, now: datetime) -> tuple[list[str], str | None]:
    """``(ids added since the checkpoint, checkpoint to save)``.

    The fallback takes a FRESH checkpoint from the profile before it lists, so
    a message arriving between the two is in the next fire's history rather
    than in nobody's.
    """
    checkpoint = (doc.get("checkpoint") or {}).get("history_id")
    if checkpoint:
        try:
            return gmail_client.history_since(gmail, str(checkpoint))
        except HistoryExpired as exc:
            logger.warning("a12: history checkpoint unusable (%s); re-listing the last day", exc)
    fresh = gmail_client.profile(gmail)["history_id"]
    since = _last_seen_epoch(doc, now) - FALLBACK_LOOKBACK_SECONDS
    ids: list[str] = []
    token: str | None = None
    for _ in range(FALLBACK_PAGES):
        page, token, _estimate = gmail_client.list_inbox(gmail, after_epoch=since, page_token=token)
        ids.extend(page)
        if not token:
            break
    return ids, fresh


def _retry(work: _Work, due: list[dict], now: datetime) -> None:
    """Re-ask the rows the model could not read. The row is found through the
    id map read this fire — never the remembered ``sheet_row`` — because she
    reorders the sheet freely."""
    for entry in due:
        if work.out_of_time():
            break
        message_id = str(entry.get("message_id") or "")
        attempts = int(entry.get("attempts") or 0)
        row = work.id_map.get(message_id)
        if not message_id or not row:
            # She deleted the row, or it was never written: stop tracking it.
            work.retry_docs[message_id or "?"] = {
                **_message_doc(_placeholder(message_id), attempts=attempts, sheet_row=None),
                "retry_due": False,
            }
            continue
        facts = work.triage(message_id)
        if facts is None:
            work.retry_docs[message_id] = {
                **_message_doc(_placeholder(message_id), attempts=attempts, sheet_row=row),
                "retry_due": False,
            }
            continue
        attempts += 1
        if facts.category != NEEDS_REVIEW:
            work.updated_rows[row] = facts
            work.recovered += 1
        work.retry_docs[message_id] = _message_doc(facts, attempts=attempts, sheet_row=row)


def _placeholder(message_id: str) -> RowFacts:
    return RowFacts(
        message_id=message_id, received_at=_utcnow(), sender="", subject="",
        category=NEEDS_REVIEW, summary="", deadline=None,
    )


def _backfill(work: _Work, backfill: dict, now: datetime) -> None:
    """Newest-first pages of the 90-day window. A page is only "done" — and
    the cursor only moves — once every id on it was handled; a page cut short
    by the budget is re-listed next fire and the id map skips its done ids."""
    since = int(backfill.get("since_epoch") or (now - timedelta(days=BACKFILL_DAYS)).timestamp())
    cursor = backfill.get("cursor") or None
    handled = 0
    while handled < BACKFILL_PER_FIRE:
        if work.out_of_time():
            break
        page_size = min(gmail_client.LIST_PAGE_MAX, BACKFILL_PER_FIRE - handled)
        ids, next_token, estimate = gmail_client.list_inbox(
            work.gmail, after_epoch=since, page_token=cursor, max_results=page_size
        )
        if estimate:
            backfill["total"] = max(int(backfill.get("total") or 0), int(estimate))
        page_complete = True
        for message_id in ids:
            if work.out_of_time():
                page_complete = False
                break
            if work.take(message_id):
                facts = work.triage(message_id)
                if facts:
                    work.add_new(facts)
            handled += 1
        if not page_complete:
            break
        backfill["done"] = int(backfill.get("done") or 0) + len(ids)
        cursor = next_token
        backfill["cursor"] = cursor
        if not cursor:
            backfill["state"] = BACKFILL_DONE
            break
    if backfill.get("total") is not None:
        backfill["total"] = max(int(backfill["total"]), int(backfill.get("done") or 0))
