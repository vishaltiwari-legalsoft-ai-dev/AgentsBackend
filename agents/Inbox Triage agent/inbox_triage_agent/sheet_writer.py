"""The Sheets writes, with the hub's own identity (ADC on the spreadsheets
scope). She shares her sheet with the service account as an editor; the hub
never holds her Google identity for Sheets.

What this module never does on the Inbox tab: delete, or write outside the
agent's columns A:I (the header row is the one exception, written once and
repaired only in A:I). Her Status and Notes columns are hers —
:func:`read_inbox` reads them, nothing writes them. Rows are found again
through the hidden Message ID column — the reconciliation key — never by
remembered row numbers, because she reorders freely: :func:`write_rows` reads
where every row is immediately before it writes, and writes in one batch.

New rows are INSERTED, newest first (``sheet_layout.place``), never written
over anything. And the tab is sorted exactly once per sheet
(:func:`_order_once`), whole rows with every column of hers, to bring a sheet
from before that rule into the same order; a marker on the sheet records it,
and after that her row order is hers again.

Upcoming and Worktree are different in kind: they are the agent's own views,
with no cell of hers in them, so their headers are rewritten whole and
Worktree's body is replaced on every build. A tab named Worktree that the
agent did not create is hers, is detected by the absence of its marker, and
is never written to at all.

One structural write exists: a sheet still in the first release's layout
(no Action column) gets the column INSERTED in place, with its header, in
one atomic ``batchUpdate`` — every existing cell, hers included, shifts
right intact. Nothing is ever written to a sheet whose row 1 does not
match the current layout: :func:`id_rows` and :func:`write_rows` check it
and raise :class:`LayoutMismatch` instead.

Before the first write to any sheet — and again on every re-check — the hub
proves the sheet is the CALLER's: Drive metadata, read as the service
account, must list the caller's signed-in address as an owner or as a
``writer``/``owner`` user permission (:func:`caller_may_edit`). The service
account can edit every sheet anyone ever shared with it, and sheet ids are
not secrets, so "the hub can edit it" proves nothing about who asked.

Values are written ``RAW``. ``USER_ENTERED`` would evaluate a subject line
that starts with ``=`` as a formula, and subject lines are third-party text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.services.google_http import (
    _RETRY_ATTEMPTS, cached_credentials, execute_with_retry, refresh_if_stale, timed_http,
)

from . import offline, refuse_if_offline, sheet_style
from .sheet_layout import (
    AGENT_COLUMNS, CATEGORY_LABELS, COL_ACTION, COL_CATEGORY, COL_DATE, COL_MESSAGE_ID,
    COL_STATUS, DATE_RANGE, HEADER_CURRENT, HEADER_EMPTY, HEADER_LEGACY, HEADERS,
    INBOX_HEADER_RANGE, INBOX_ROWS_RANGE, INBOX_TAB, LAST_AGENT_COL, LAST_COL, MESSAGE_ID_RANGE,
    MIGRATION_INSERTS, ORDER_VERSION, STATUS_OPTIONS, UPCOMING_FORMULA, UPCOMING_HEADERS,
    UPCOMING_TAB, WORKTREE_HEADER_RANGE, WORKTREE_HEADERS, WORKTREE_MARKER_KEY,
    WORKTREE_ROWS_RANGE, WORKTREE_TAB, column_letter, header_state, insert_requests,
    is_newest_first, landed, order_requests, place, pushed, same_formula, update_requests,
)
from .triage import NEEDS_REVIEW

logger = logging.getLogger("agentos.inbox.sheets")

#: Socket deadline for each Sheets call — see ``app.services.google_http``.
SHEETS_TIMEOUT_SECONDS = 30
WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
#: Read-only Drive METADATA — owners and permissions, never file content.
DRIVE_METADATA_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
#: Socket deadline for the one Drive metadata call per check.
DRIVE_TIMEOUT_SECONDS = 30

CHECK_OK = "ok"
CHECK_NOT_FOUND = "not_found"
CHECK_NOT_SHARED = "not_shared"
CHECK_NOT_EDITABLE = "not_editable"
#: The hub can reach the sheet, but Drive does not show the caller as its
#: owner or an editor (or the permissions could not be read). Nothing written.
CHECK_NOT_YOURS = "not_yours"
#: The sheet is Marketing Research's tracker or one of its connected sources.
#: Decided in ``pipeline`` (it owns the cross-agent read); nothing written.
CHECK_MR_SOURCE = "mr_source"
#: Roles that let a person edit a Drive file.
_EDIT_ROLES = frozenset({"writer", "owner"})

class SheetsUnavailable(RuntimeError):
    """A Sheets call could not be completed, or was refused for a reason that
    is not one of the panel's check words. The message never carries the
    spreadsheet id or a request URL — it reaches ``last_poll`` and the cron
    envelope."""


class LayoutMismatch(SheetsUnavailable):
    """Row 1 of Inbox is not the current layout, so a positional write could
    land in the wrong columns. Nothing is written; the fire re-runs set-up
    (which migrates a legacy sheet) and reads again."""


class ReorderFailed(SheetsUnavailable):
    """The one-time newest-first sort did not happen. The batch is atomic, so
    no row moved and no marker was written; the next poll tries again."""


class SheetsRefused(SheetsUnavailable):
    """Google answered a Sheets or Drive call with a non-transient HTTP
    status. ``status`` lets :func:`check` map 403/404 to the panel's words;
    everyone else can treat it as the :class:`SheetsUnavailable` it is."""

    def __init__(self, message: str, *, status: int | None):
        super().__init__(message)
        self.status = status


#: What set-up did about the sheet's look (see ``sheet_style``). Recorded on
#: the connection doc, so a refused formatting pass is on the record, not
#: only in a log line.
FORMAT_APPLIED = "applied"
FORMAT_ALREADY = "already"
FORMAT_FAILED = "failed"

#: What set-up did about the Inbox tab's row order (see :func:`_order_once`).
#: ``applied``: sorted newest first on this pass. ``already``: the marker says
#: it was done before. ``pending``: not done yet, and this pass was not asked
#: to — only a poll, which holds the connection's lease, may move rows.
ORDER_APPLIED = "applied"
ORDER_ALREADY = "already"
ORDER_PENDING = "pending"
#: The answers that mean the sheet is in newest-first order.
ORDER_SETTLED = frozenset({ORDER_APPLIED, ORDER_ALREADY})

#: The Worktree tab is the agent's: it created it and may rewrite it.
WORKTREE_OURS = "ours"
#: The sheet already had a tab called Worktree that the agent did not create.
#: It is hers. Nothing is ever written to it, nothing is styled on it, and the
#: connection document records this so the panel can say why the tab is empty.
WORKTREE_CLAIMED = "claimed"


@dataclass(frozen=True)
class Setup:
    """What one set-up pass did beyond proving the sheet is writable."""

    formatting: str
    worktree: str
    #: The Worktree tab's sheet id when it is the agent's; ``None`` when hers.
    worktree_sheet_id: int | None = None
    #: One of the ORDER_* values.
    ordering: str = ORDER_PENDING


@dataclass(frozen=True)
class SheetCheck:
    status: str  # one of the CHECK_* values
    title: str
    #: One of the FORMAT_* values when set-up ran; "" otherwise. Not part of
    #: equality: it is a report on the pass, not on the sheet's standing.
    formatting: str = field(default="", compare=False)
    #: ``ours`` / ``claimed`` when set-up ran; "" otherwise. Same reason.
    worktree: str = field(default="", compare=False)
    #: One of the ORDER_* values when set-up ran; "" otherwise. Same reason.
    ordering: str = field(default="", compare=False)


def service():
    refuse_if_offline("Google Sheets")
    from googleapiclient.discovery import build

    creds = cached_credentials([WRITE_SCOPE])
    return build(
        "sheets", "v4", http=timed_http(creds, SHEETS_TIMEOUT_SECONDS), cache_discovery=False
    )


def service_account_email() -> str:
    """The identity she must share the sheet with, read off the credentials
    — the key file's ``client_email`` locally, the attached identity on Cloud
    Run (which reports itself only after a refresh). Never hardcoded. An
    empty string means it could not be resolved, and the log says why."""
    if offline():
        return ""
    try:
        creds = cached_credentials([WRITE_SCOPE])
        email = str(getattr(creds, "service_account_email", "") or "")
        if not email or email == "default":
            refresh_if_stale(creds)
            email = str(getattr(creds, "service_account_email", "") or "")
    except Exception as exc:  # noqa: BLE001 — reported, not raised: status must render
        logger.error("a12: could not resolve the hub's service-account identity: %s", exc)
        return ""
    return "" if email == "default" else email


def _status_of(exc: BaseException) -> int | None:
    return getattr(getattr(exc, "resp", None), "status", None)


def drive_service():
    """Drive v3 on the metadata-only scope, for :func:`caller_may_edit`."""
    refuse_if_offline("Google Drive")
    from googleapiclient.discovery import build

    creds = cached_credentials([DRIVE_METADATA_SCOPE])
    return build(
        "drive", "v3", http=timed_http(creds, DRIVE_TIMEOUT_SECONDS), cache_discovery=False
    )


def _cause_of(exc: BaseException | None) -> str:
    """A cause that names no resource: an HTTP status or an exception class.
    ``str(HttpError)`` embeds the request URL, which carries the spreadsheet
    id — that must not reach the panel or the scheduler's logs."""
    status = _status_of(exc) if exc is not None else None
    if status is not None:
        return f"HTTP {status}"
    if isinstance(exc, TimeoutError):
        return "timed out"
    return type(exc).__name__ if exc is not None else "unknown"


def _run(request, *, what: str, service: str = "Sheets"):
    """``execute`` with the shared retry, and every failure mapped to this
    module's exceptions — callers never see a raw ``HttpError`` (the same
    contract as ``gmail_client._run``), and no message names the sheet."""
    from googleapiclient.errors import HttpError

    try:
        return execute_with_retry(request, what=f"{service} {what}", unavailable=SheetsUnavailable)
    except HttpError as exc:
        status = _status_of(exc)
        raise SheetsRefused(f"{service} {what} was refused: HTTP {status}", status=status) from exc
    except SheetsUnavailable as exc:
        raise SheetsUnavailable(
            f"{service} {what} failed after {_RETRY_ATTEMPTS} attempts ({_cause_of(exc.__cause__)})"
        ) from exc.__cause__


# --------------------------------------------------------------------------- #
# Check and set up
# --------------------------------------------------------------------------- #

def check(
    spreadsheet_id: str, *, caller_email: str, svc=None, drive=None, reorder: bool = False,
) -> SheetCheck:
    """Is this the caller's sheet, and can the hub see and edit it?

    In order, and nothing is written until the third step:

    1. The metadata read answers ``not_found`` and ``not_shared``.
    2. Drive answers ``not_yours`` unless it shows ``caller_email`` as an
       owner or editor (:func:`caller_may_edit`, fail-closed).
    3. The set-up writes answer ``not_editable``.

    Anything else the API refuses is :class:`SheetsUnavailable` with the
    status. ``ok`` means the sheet is also set up, since the same writes
    prove both. The title is returned only with ``ok``: for any other answer
    the caller has not shown the sheet is theirs, so its name is not theirs
    to read either.

    ``reorder`` lets set-up run the one-time newest-first sort. Only a poll
    passes it: a poll holds the connection's lease, so no other write of the
    agent's can be half-way through the rows while they move."""
    svc = svc or service()
    try:
        meta = _metadata(spreadsheet_id, svc)
    except SheetsRefused as exc:
        if exc.status == 404:
            return SheetCheck(CHECK_NOT_FOUND, "")
        if exc.status == 403:
            return SheetCheck(CHECK_NOT_SHARED, "")
        raise
    if not caller_may_edit(spreadsheet_id, caller_email, drive=drive):
        return SheetCheck(CHECK_NOT_YOURS, "")
    title = str((meta.get("properties") or {}).get("title") or "")
    try:
        done = setup(spreadsheet_id, svc=svc, meta=meta, reorder=reorder)
    except SheetsRefused as exc:
        if exc.status == 403:
            return SheetCheck(CHECK_NOT_EDITABLE, "")
        raise
    return SheetCheck(CHECK_OK, title, done.formatting, done.worktree, done.ordering)


def caller_may_edit(spreadsheet_id: str, caller_email: str, *, drive=None) -> bool:
    """Does Drive show ``caller_email`` as an owner of the file, or as a
    ``user`` permission with role ``writer``/``owner``? Case-insensitive.

    Fail-closed on missing information: no caller address, a permissions
    list Drive did not return (a shared-drive file, or a sharing setting
    that hides it from editors), or Drive answering 404 (the service account
    cannot see the file through Drive) are all ``False``. A 403 or an outage
    is not an answer about ownership, so it is raised as
    :class:`SheetsUnavailable` — the check fails loudly and writes nothing."""
    wanted = str(caller_email or "").strip().lower()
    if not wanted:
        return False
    drive = drive or drive_service()
    try:
        meta = _run(
            drive.files().get(
                fileId=spreadsheet_id,
                fields="owners(emailAddress),permissions(emailAddress,role,type)",
                supportsAllDrives=True,
            ),
            what="permission lookup",
            service="Drive",
        )
    except SheetsRefused as exc:
        if exc.status == 404:
            return False
        raise
    permissions = meta.get("permissions")
    if not isinstance(permissions, list):
        logger.warning("a12: Drive returned no permission list for a sheet check; refusing")
        return False
    owners = {
        str(o.get("emailAddress") or "").strip().lower()
        for o in meta.get("owners") or [] if isinstance(o, dict)
    }
    if wanted in owners:
        return True
    return any(
        isinstance(p, dict)
        and p.get("type") == "user"
        and p.get("role") in _EDIT_ROLES
        and str(p.get("emailAddress") or "").strip().lower() == wanted
        for p in permissions
    )


def _metadata(spreadsheet_id: str, svc) -> dict:
    return _run(
        svc.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            # developerMetadata carries the "formatted" marker: reading it
            # here keeps the hourly re-check at the calls it already made.
            fields="properties.title,sheets.properties,"
                   "developerMetadata(metadataId,metadataKey,metadataValue)",
        ),
        what="metadata",
    )


def worktree_standing(meta: dict, tabs: dict[str, int]) -> tuple[str, int | None]:
    """``(WORKTREE_* state, sheet id)`` for the Worktree tab, decided before
    anything is created.

    No tab yet → it will be the agent's. A tab whose id the agent's marker
    names → still the agent's. A tab called Worktree that no marker names →
    HERS: she made it, or renamed something onto that name, and the agent
    neither writes nor styles it. The marker carries the sheet id rather than
    a bare "yes" for exactly that reason."""
    sheet_id = tabs.get(WORKTREE_TAB)
    if sheet_id is None:
        return WORKTREE_OURS, None
    for entry in (meta or {}).get("developerMetadata") or []:
        if entry.get("metadataKey") == WORKTREE_MARKER_KEY:
            if str(entry.get("metadataValue") or "") == str(sheet_id):
                return WORKTREE_OURS, int(sheet_id)
    return WORKTREE_CLAIMED, None


def _worktree_marker_requests(meta: dict, sheet_id: int) -> list[dict]:
    """Claim a freshly created Worktree tab: drop any stale marker (she
    renamed or deleted the old tab), then record this one's id."""
    requests: list[dict] = [
        {"deleteDeveloperMetadata": {"dataFilter": {
            "developerMetadataLookup": {"metadataId": entry["metadataId"]}}}}
        for entry in (meta or {}).get("developerMetadata") or []
        if entry.get("metadataKey") == WORKTREE_MARKER_KEY and entry.get("metadataId") is not None
    ]
    requests.append({"createDeveloperMetadata": {"developerMetadata": {
        "metadataKey": WORKTREE_MARKER_KEY,
        "metadataValue": str(sheet_id),
        "location": {"spreadsheet": True},
        "visibility": "DOCUMENT",
    }}})
    return requests


def setup(
    spreadsheet_id: str, *, svc=None, meta: dict | None = None, reorder: bool = False,
) -> Setup:
    """Idempotent: the three tabs exist, row 1 carries the headers, row 1 is
    frozen, the Message ID column is hidden, Status has its dropdown, and
    Upcoming!A2 holds the current formula view — rewritten whenever what is
    stored differs, so a drifted view repairs itself on the next check. A
    legacy Inbox (no Action column) is migrated in place first. Re-running
    changes nothing that is already right; the layout write always happens
    and is what proves the hub can edit the sheet.

    The third tab is Worktree, added to an existing sheet without touching
    Inbox or Upcoming — unless the sheet already had a tab of that name that
    the agent did not create, which is hers (:func:`worktree_standing`): it is
    left exactly as it is, the state comes back as
    :data:`WORKTREE_CLAIMED`, and the connection document records it.

    Last, the look (:mod:`sheet_style`) — once: on a new, just-migrated or
    just-given-a-Worktree sheet, or one the marker says was never formatted at
    this version. A refused formatting pass never raises.

    Very last, the row order (:func:`_order_once`) — once per sheet, and only
    when ``reorder`` is passed. Unlike the look, a sort that fails RAISES
    (:class:`ReorderFailed`): it is a correction to her data, not decoration,
    and a fire that skipped it must not report itself a success.

    Raises :class:`SheetsRefused` with the status so :func:`check` can map
    a 403 to ``not_editable``. Only :func:`check` calls this, after the
    ownership proof."""
    svc = svc or service()
    meta = meta or _metadata(spreadsheet_id, svc)
    tabs = {
        str(s["properties"]["title"]): int(s["properties"]["sheetId"])
        for s in meta.get("sheets") or []
        if s.get("properties")
    }
    worktree_state, worktree_sheet_id = worktree_standing(meta, tabs)
    wanted = [INBOX_TAB, UPCOMING_TAB]
    if worktree_state == WORKTREE_OURS:
        wanted.append(WORKTREE_TAB)
    missing = [tab for tab in wanted if tab not in tabs]
    if missing:
        created = _run(
            svc.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab}}} for tab in missing]},
            ),
            what="tab creation",
        )
        for reply in created.get("replies") or []:
            props = (reply.get("addSheet") or {}).get("properties") or {}
            if props.get("title"):
                tabs[str(props["title"])] = int(props.get("sheetId") or 0)

    inbox_sheet_id = tabs[INBOX_TAB]
    worktree_created = worktree_state == WORKTREE_OURS and WORKTREE_TAB in missing
    if worktree_state == WORKTREE_OURS:
        worktree_sheet_id = tabs[WORKTREE_TAB]
    if worktree_state == WORKTREE_CLAIMED:
        logger.warning(
            "a12: this sheet already has a Worktree tab the agent did not create; "
            "it is left untouched and no worktree is written"
        )
    # Read first: the layout indexes below are for the CURRENT columns, so a
    # legacy sheet must be migrated in the same batch, ahead of them.
    read = _run(
        svc.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[
                INBOX_HEADER_RANGE, f"{UPCOMING_TAB}!A1:Z1", f"{UPCOMING_TAB}!A2",
                *([WORKTREE_HEADER_RANGE] if worktree_state == WORKTREE_OURS else []),
            ],
            valueRenderOption="FORMULA",  # A2 is compared as a formula, not its result
        ),
        what="header read",
    )
    ranges = read.get("valueRanges") or [{}, {}, {}]
    inbox_head = _first_row(ranges[0] if len(ranges) > 0 else {})
    upcoming_head = _first_row(ranges[1] if len(ranges) > 1 else {})
    upcoming_a2 = _first_row(ranges[2] if len(ranges) > 2 else {})
    worktree_head = _first_row(ranges[3] if len(ranges) > 3 else {})

    state = header_state(inbox_head)
    requests: list[dict] = []
    if state == HEADER_LEGACY:
        requests += _migration_requests(inbox_sheet_id)
        logger.warning(
            "a12: migrating a legacy Inbox layout in place (inserting %s)",
            ", ".join(name for _, name in MIGRATION_INSERTS),
        )
    requests += _layout_requests(inbox_sheet_id)
    if worktree_created:
        requests += _worktree_marker_requests(meta, int(worktree_sheet_id or 0))
    _run(
        svc.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}),
        what="layout",
    )

    header_writes: list[dict] = []
    if state == HEADER_EMPTY:
        header_writes.append({"range": f"{INBOX_TAB}!A1:{LAST_COL}1", "values": [list(HEADERS)]})
    elif state not in (HEADER_CURRENT, HEADER_LEGACY):
        # Repair the agent's own header cells only; hers (J, K) are never touched.
        header_writes.append(
            {"range": f"{INBOX_TAB}!A1:{LAST_AGENT_COL}1", "values": [list(AGENT_COLUMNS)]}
        )
    if [c.strip() for c in upcoming_head] != list(UPCOMING_HEADERS):
        # Upcoming is the agent's view, not hers: its header is rewritten
        # whole, and cells left over from a wider old header are blanked.
        width = max(len(upcoming_head), len(UPCOMING_HEADERS))
        row = list(UPCOMING_HEADERS) + [""] * (width - len(UPCOMING_HEADERS))
        header_writes.append(
            {"range": f"{UPCOMING_TAB}!A1:{column_letter(width)}1", "values": [row]}
        )
    if worktree_state == WORKTREE_OURS and [c.strip() for c in worktree_head] != list(
        WORKTREE_HEADERS
    ):
        # Worktree, like Upcoming, is the agent's view: header rewritten whole.
        width = max(len(worktree_head), len(WORKTREE_HEADERS))
        row = list(WORKTREE_HEADERS) + [""] * (width - len(WORKTREE_HEADERS))
        header_writes.append(
            {"range": f"{WORKTREE_TAB}!A1:{column_letter(width)}1", "values": [row]}
        )
    if header_writes:
        _run(
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": header_writes},
            ),
            what="header write",
        )
    if not same_formula(upcoming_a2[0] if upcoming_a2 else ""):
        # Empty, drifted (Sheets shifts row-numbered references when rows are
        # inserted), an older view, or hand-edited: the view is rewritten.
        _run(
            svc.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{UPCOMING_TAB}!A2",
                valueInputOption="USER_ENTERED",  # a formula, so it must be entered as one
                body={"values": [[UPCOMING_FORMULA]]},
            ),
            what="formula write",
        )
    formatting = _format_once(
        spreadsheet_id, svc, meta, inbox_sheet_id=inbox_sheet_id,
        upcoming_sheet_id=tabs[UPCOMING_TAB], worktree_sheet_id=worktree_sheet_id,
        restyle=state == HEADER_LEGACY or worktree_created,
    )
    ordering = _order_once(
        spreadsheet_id, svc, meta, inbox_sheet_id=inbox_sheet_id, reorder=reorder
    )
    return Setup(
        formatting=formatting, worktree=worktree_state, worktree_sheet_id=worktree_sheet_id,
        ordering=ordering,
    )


def _order_once(
    spreadsheet_id: str, svc, meta: dict, *, inbox_sheet_id: int, reorder: bool
) -> str:
    """Bring the Inbox tab into newest-first order, once per sheet.

    Sheets written before new rows were inserted newest-first hold them in
    two runs: the backfill newest-first from the top, and every message since
    appended oldest-first underneath. One ``sortRange`` over every row below
    the header — no columns named, so a row moves with every cell in it —
    makes that one order, and the marker written by the same atomic batch
    means it never runs again. After that her row order is hers.

    One request, whatever the size of the sheet: Sheets sorts on its side, so
    there is nothing to page through and nothing to resume half-way. What can
    be interrupted is the ANSWER — the batch applied, the reply lost — so a
    failure is checked against the sheet before it is believed: if the marker
    is there, the sort is too. Otherwise :class:`ReorderFailed`, with the
    sheet exactly as it was."""
    if is_newest_first(meta):
        return ORDER_ALREADY
    if not reorder:
        return ORDER_PENDING
    try:
        _run(
            svc.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": order_requests(meta, inbox_sheet_id)},
            ),
            what="row reorder",
        )
    except SheetsUnavailable as exc:
        try:
            done = is_newest_first(_metadata(spreadsheet_id, svc))
        except SheetsUnavailable:
            done = False
        if not done:
            raise ReorderFailed(
                "The one-time reorder of the Inbox tab (newest mail first) did not complete: "
                f"{exc}. No row was moved; it is tried again on the next poll."
            ) from exc
        logger.warning("a12: the row reorder's reply was lost, but the sheet shows it applied")
    logger.info("a12: Inbox rows sorted newest first (%s)", ORDER_VERSION)
    return ORDER_APPLIED


def _format_once(
    spreadsheet_id: str, svc, meta: dict, *, inbox_sheet_id: int, upcoming_sheet_id: int,
    worktree_sheet_id: int | None, restyle: bool,
) -> str:
    """Apply the look unless the marker says it is already there at this
    version. ``restyle`` forces a pass on a sheet that just gained a column
    or a tab, whatever the marker says.

    Its own batch, after the layout batch and the header/formula writes, for
    two reasons: the layout batch carries the migration and is the proof of
    editability, and a formatting request Google refuses must neither roll
    the migration back nor turn into ``not_editable``; and the header cells
    must exist before they are styled. The batch itself is atomic with the
    marker as its last request, so a refusal leaves no half-look and no
    marker — the next hourly check tries again. A refusal is logged (with
    no sheet id: ``_run``'s messages never carry one) and returned as
    :data:`FORMAT_FAILED`; rows are still written."""
    if sheet_style.is_formatted(meta) and not restyle:
        return FORMAT_ALREADY
    try:
        live = _run(
            svc.spreadsheets().get(
                spreadsheetId=spreadsheet_id, fields=sheet_style.FORMAT_READ_FIELDS
            ),
            what="format read",
        )
        requests = sheet_style.format_requests(
            live, inbox_sheet_id=inbox_sheet_id, upcoming_sheet_id=upcoming_sheet_id,
            worktree_sheet_id=worktree_sheet_id,
        )
        _run(
            svc.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests}
            ),
            what="formatting",
        )
    except SheetsUnavailable as exc:
        logger.warning(
            "a12: the sheet's formatting was not applied (%s); rows are still written and "
            "formatting is retried on the next check", exc,
        )
        return FORMAT_FAILED
    logger.info("a12: sheet formatting applied (version %s)", sheet_style.FORMAT_VERSION)
    return FORMAT_APPLIED


def _migration_requests(inbox_sheet_id: int) -> list[dict]:
    """Legacy → current, as structural requests applied in order at the head
    of the layout batch: each new column inserted at its index (existing
    cells, formatting, the hidden id column and her Status dropdown all shift
    right with it), then row 1 rewritten to the current agent header. One
    ``batchUpdate`` is atomic — the sheet is never left half-migrated."""
    requests: list[dict] = [
        {
            "insertDimension": {
                "range": {
                    "sheetId": inbox_sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": index,
                    "endIndex": index + 1,
                },
                "inheritFromBefore": True,
            }
        }
        for index, _name in MIGRATION_INSERTS
    ]
    requests.append({
        "updateCells": {
            "start": {"sheetId": inbox_sheet_id, "rowIndex": 0, "columnIndex": 0},
            "rows": [{"values": [
                {"userEnteredValue": {"stringValue": name}} for name in AGENT_COLUMNS
            ]}],
            "fields": "userEnteredValue",
        }
    })
    return requests


def _layout_requests(inbox_sheet_id: int) -> list[dict]:
    return [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": inbox_sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": inbox_sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": COL_MESSAGE_ID - 1,
                    "endIndex": COL_MESSAGE_ID,
                },
                "properties": {"hiddenByUser": True},
                "fields": "hiddenByUser",
            }
        },
        {
            "setDataValidation": {
                "range": {
                    "sheetId": inbox_sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": COL_STATUS - 1,
                    "endColumnIndex": COL_STATUS,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": option} for option in STATUS_OPTIONS],
                    },
                    "showCustomUi": True,
                    "strict": False,
                },
            }
        },
    ]


def _first_row(value_range: dict) -> list[str]:
    values = value_range.get("values") or []
    return [str(cell) for cell in values[0]] if values else []


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #

def id_rows(spreadsheet_id: str, *, svc=None) -> dict[str, int]:
    """``{message_id: 1-based row}`` from the hidden column, read once per
    fire together with row 1. The first occurrence wins if she ever
    duplicated a row by hand.

    Row 1 must be the current agent header: every write is by column
    position, and a legacy or edited header means the id column is not
    where the layout looks. That is :class:`LayoutMismatch`, raised before
    anything is written — never an empty map that would write every message
    a second time.

    The row numbers are for deciding what is already on the sheet. They are
    not addresses: :func:`write_rows` reads its own, at the moment it writes."""
    svc = svc or service()
    data = _run(
        svc.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[INBOX_HEADER_RANGE, MESSAGE_ID_RANGE],
        ),
        what="id column read",
    )
    ranges = data.get("valueRanges") or []
    _require_current_header(_first_row(ranges[0] if ranges else {}))
    out: dict[str, int] = {}
    ids = (ranges[1] if len(ranges) > 1 else {}).get("values") or []
    for row_number, row in enumerate(ids, start=1):
        if row_number == 1:
            continue  # the header
        value = str(row[0]).strip() if row else ""
        if value and value not in out:
            out[value] = row_number
    return out


def _require_current_header(head: list[str]) -> None:
    if header_state(head) != HEADER_CURRENT:
        raise LayoutMismatch(
            "The Inbox tab's header row does not match the agent's columns; nothing was written."
        )


def read_inbox(spreadsheet_id: str, *, svc=None) -> list[list[str]]:
    """Every Inbox row from 2 down, as raw cells, HER columns included.

    The one read in this module that looks at Status and Notes, and it is
    read-only: the Worktree needs to know what she has already marked Done or
    Ignore, and no rule can know that from the agent's own columns. Row 1 is
    dropped, and rows come back ragged exactly as Sheets returns them —
    :func:`worktree.from_row` reads by column index and treats a short row as
    blank cells.

    Called only by the Worktree pass, and only when that pass has decided a
    rebuild is warranted; the write path reads two narrow columns instead."""
    svc = svc or service()
    data = _run(
        svc.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id, ranges=[INBOX_HEADER_RANGE, INBOX_ROWS_RANGE],
        ),
        what="inbox read",
    )
    ranges = data.get("valueRanges") or []
    _require_current_header(_first_row(ranges[0] if ranges else {}))
    rows = (ranges[1] if len(ranges) > 1 else {}).get("values") or []
    return [[str(cell) for cell in row] for row in rows[1:]]


def write_worktree(
    spreadsheet_id: str, rows: list[list[str]], *, previous_rows: int = 0, svc=None
) -> int:
    """Rewrite the Worktree tab's body in ONE ``values.update``.

    The tab is derived data with no cell of hers in it, so it is replaced
    rather than reconciled. Rows left over from a longer previous build are
    blanked in the same rectangle — ``previous_rows`` is what the connection
    document remembers writing — so no stale process is left below the new
    list and no second ``values.clear`` call is needed. Nothing outside
    ``Worktree!A2:H`` is ever touched, and the values are RAW because a
    Process title is a subject line.

    Returns the number of real rows written."""
    width = len(WORKTREE_HEADERS)
    height = max(len(rows), int(previous_rows or 0))
    if height <= 0:
        return 0
    padded = [list(row) + [""] * (width - len(row)) for row in rows]
    padded += [[""] * width for _ in range(height - len(rows))]
    svc = svc or service()
    _run(
        svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=WORKTREE_ROWS_RANGE.format(last=height + 1),
            valueInputOption="RAW",
            body={"values": padded},
        ),
        what="worktree write",
    )
    return len(rows)


_UNREAD_LABELS = frozenset({NEEDS_REVIEW, CATEGORY_LABELS[NEEDS_REVIEW]})


def blank_action_ids(spreadsheet_id: str, *, svc=None) -> list[str]:
    """Message ids whose row has a Category but an empty Action — rows
    written before the Action column existed — in sheet order. Rows still
    marked needs review belong to the retry path. Read only while the
    one-time re-triage of old rows is running."""
    svc = svc or service()
    columns = (COL_MESSAGE_ID, COL_ACTION, COL_CATEGORY)
    data = _run(
        svc.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[f"{INBOX_TAB}!{column_letter(c)}:{column_letter(c)}" for c in columns],
        ),
        what="action column read",
    )
    ranges = data.get("valueRanges") or []

    def column(i: int) -> list[str]:
        values = (ranges[i] if i < len(ranges) else {}).get("values") or []
        return [str(r[0]).strip() if r else "" for r in values]

    ids, actions, categories = column(0), column(1), column(2)
    out: list[str] = []
    for index in range(1, len(ids)):  # index 0 is the header row
        message_id = ids[index]
        action = actions[index] if index < len(actions) else ""
        category = categories[index] if index < len(categories) else ""
        if message_id and not action and category and category not in _UNREAD_LABELS:
            if message_id not in out:
                out.append(message_id)
    return out


@dataclass(frozen=True)
class Written:
    """Where one fire's rows are after :func:`write_rows`."""

    #: 1-based row of each new row, in the order the rows were given.
    new_rows: list[int] = field(default_factory=list)
    #: ``message id -> 1-based row`` for every update that found its row.
    updated: dict[str, int] = field(default_factory=dict)
    #: Ids whose row is no longer on the sheet — she deleted it while the fire
    #: was working. Nothing was written for them.
    missing: list[str] = field(default_factory=list)


def _column(value_range: dict) -> list[str]:
    return [str(row[0]).strip() if row else "" for row in value_range.get("values") or []]


def _id_of(row: list[str]) -> str:
    return str(row[COL_MESSAGE_ID - 1]).strip() if len(row) >= COL_MESSAGE_ID else ""


def _inbox_sheet_id(spreadsheet_id: str, svc) -> int:
    meta = _run(
        svc.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties(sheetId,title)"
        ),
        what="tab lookup",
    )
    for sheet in meta.get("sheets") or []:
        props = sheet.get("properties") or {}
        if str(props.get("title") or "") == INBOX_TAB:
            return int(props.get("sheetId") or 0)
    raise SheetsUnavailable("The Inbox tab is missing from the sheet; nothing was written.")


def write_rows(
    spreadsheet_id: str, new_rows: list[list[str]],
    updates: dict[str, list[str]] | None = None, *, svc=None,
) -> Written:
    """Everything one fire writes to Inbox, as ONE atomic ``batchUpdate``.

    ``new_rows`` are the agent's cells of messages not yet on the sheet;
    ``updates`` is ``{message id: the agent's cells}`` for rows that are.

    Nothing here trusts a row number from earlier in the fire. The Date and
    Message ID columns are read now, immediately before the write, and from
    that read alone: every update is addressed to the row its id is on at
    this moment, and every new row is given its place newest-first
    (``sheet_layout.place``). New rows are inserted, so no existing cell is
    ever written over and a row of hers only ever moves down, whole. Only
    columns A:I are written, as text that is never evaluated.

    One batch means all of it lands or none of it does. A refusal or a lost
    connection raises with the sheet as it was, and the fire — which persists
    nothing until this returns — runs the same mail again next time.

    A new row whose id turns out to be on the sheet already is not written a
    second time; it is reported on the row it already has."""
    updates = dict(updates or {})
    if not new_rows and not updates:
        return Written()
    svc = svc or service()
    inbox_sheet_id = _inbox_sheet_id(spreadsheet_id, svc)
    data = _run(
        svc.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[INBOX_HEADER_RANGE, DATE_RANGE, MESSAGE_ID_RANGE],
        ),
        what="row position read",
    )
    ranges = data.get("valueRanges") or []
    _require_current_header(_first_row(ranges[0] if ranges else {}))
    dates = _column(ranges[1] if len(ranges) > 1 else {})
    ids = _column(ranges[2] if len(ranges) > 2 else {})
    end = max(len(dates), len(ids), 1)
    where: dict[str, int] = {}  # message id -> 0-based row index, first occurrence
    for index, message_id in enumerate(ids):
        if index and message_id and message_id not in where:
            where[message_id] = index

    fresh = [position for position, row in enumerate(new_rows) if _id_of(row) not in where]
    blocks = place(dates, [str(new_rows[p][COL_DATE - 1]) for p in fresh], end=end)
    found = {message_id: where[message_id] for message_id in updates if message_id in where}
    requests = update_requests(
        inbox_sheet_id, {index: updates[message_id] for message_id, index in found.items()}
    ) + insert_requests(inbox_sheet_id, blocks, [new_rows[p] for p in fresh])
    if requests:
        _run(
            svc.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests}
            ),
            what="row write",
        )

    rows_of_new = [0] * len(new_rows)
    for position, row in zip(fresh, landed(blocks, len(fresh))):
        rows_of_new[position] = row
    for position, row in enumerate(new_rows):
        if not rows_of_new[position]:
            rows_of_new[position] = pushed(where[_id_of(row)], blocks) + 1
    return Written(
        new_rows=rows_of_new,
        updated={message_id: pushed(index, blocks) + 1 for message_id, index in found.items()},
        missing=[message_id for message_id in updates if message_id not in where],
    )
