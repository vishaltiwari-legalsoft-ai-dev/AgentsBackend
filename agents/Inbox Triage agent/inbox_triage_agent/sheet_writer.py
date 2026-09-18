"""The Sheets writes, with the hub's own identity (ADC on the spreadsheets
scope). She shares her sheet with the service account as an editor; the hub
never holds her Google identity for Sheets.

What this module never does: sort, delete, or write outside the agent's
columns A:I (the header row is the one exception, written once and repaired
only in A:I). Her Status and Notes columns and her row order are hers. Rows
are found again through the hidden Message ID column — the reconciliation
key — never by remembered row numbers, because she reorders freely.

One structural write exists: a sheet still in the first release's layout
(no Action column) gets the column INSERTED in place, with its header, in
one atomic ``batchUpdate`` — every existing cell, hers included, shifts
right intact. Nothing is ever appended to a sheet whose row 1 does not
match the current layout: :func:`id_rows` checks it on every fire and
raises :class:`LayoutMismatch` instead.

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
import re
from dataclasses import dataclass, field

from app.services.google_http import (
    _RETRY_ATTEMPTS, cached_credentials, execute_with_retry, refresh_if_stale, timed_http,
)

from . import offline, refuse_if_offline, sheet_style
from .sheet_layout import (
    AGENT_COLUMNS, AGENT_RANGE, CATEGORY_LABELS, COL_ACTION, COL_CATEGORY, COL_MESSAGE_ID,
    COL_STATUS, HEADER_CURRENT, HEADER_EMPTY, HEADER_LEGACY, HEADERS, INBOX_HEADER_RANGE,
    INBOX_TAB, LAST_AGENT_COL, LAST_COL, MESSAGE_ID_RANGE, MIGRATION_INSERTS, STATUS_OPTIONS,
    UPCOMING_FORMULA, UPCOMING_HEADERS, UPCOMING_TAB, column_letter, header_state, same_formula,
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

_A1_FIRST_ROW = re.compile(r"![A-Z]+(\d+)")


class SheetsUnavailable(RuntimeError):
    """A Sheets call could not be completed, or was refused for a reason that
    is not one of the panel's check words. The message never carries the
    spreadsheet id or a request URL — it reaches ``last_poll`` and the cron
    envelope."""


class LayoutMismatch(SheetsUnavailable):
    """Row 1 of Inbox is not the current layout, so a positional write could
    land in the wrong columns. Nothing is written; the fire re-runs set-up
    (which migrates a legacy sheet) and reads again."""


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


@dataclass(frozen=True)
class SheetCheck:
    status: str  # one of the CHECK_* values
    title: str
    #: One of the FORMAT_* values when set-up ran; "" otherwise. Not part of
    #: equality: it is a report on the pass, not on the sheet's standing.
    formatting: str = field(default="", compare=False)


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

def check(spreadsheet_id: str, *, caller_email: str, svc=None, drive=None) -> SheetCheck:
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
    to read either."""
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
        formatting = setup(spreadsheet_id, svc=svc, meta=meta)
    except SheetsRefused as exc:
        if exc.status == 403:
            return SheetCheck(CHECK_NOT_EDITABLE, "")
        raise
    return SheetCheck(CHECK_OK, title, formatting)


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


def setup(spreadsheet_id: str, *, svc=None, meta: dict | None = None) -> str:
    """Idempotent: the two tabs exist, row 1 carries the headers, row 1 is
    frozen, the Message ID column is hidden, Status has its dropdown, and
    Upcoming!A2 holds the current formula view — rewritten whenever what is
    stored differs, so a drifted view repairs itself on the next check. A
    legacy Inbox (no Action column) is migrated in place first. Re-running
    changes nothing that is already right; the layout write always happens
    and is what proves the hub can edit the sheet.

    Last, the look (:mod:`sheet_style`) — once: on a new or just-migrated
    sheet, or one the marker says was never formatted (the sheets set up
    before the look existed). Returns a ``FORMAT_*`` value; a refused
    formatting pass never raises.

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
    missing = [tab for tab in (INBOX_TAB, UPCOMING_TAB) if tab not in tabs]
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
    # Read first: the layout indexes below are for the CURRENT columns, so a
    # legacy sheet must be migrated in the same batch, ahead of them.
    read = _run(
        svc.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[INBOX_HEADER_RANGE, f"{UPCOMING_TAB}!A1:Z1", f"{UPCOMING_TAB}!A2"],
            valueRenderOption="FORMULA",  # A2 is compared as a formula, not its result
        ),
        what="header read",
    )
    ranges = read.get("valueRanges") or [{}, {}, {}]
    inbox_head = _first_row(ranges[0] if len(ranges) > 0 else {})
    upcoming_head = _first_row(ranges[1] if len(ranges) > 1 else {})
    upcoming_a2 = _first_row(ranges[2] if len(ranges) > 2 else {})

    state = header_state(inbox_head)
    requests: list[dict] = []
    if state == HEADER_LEGACY:
        requests += _migration_requests(inbox_sheet_id)
        logger.warning(
            "a12: migrating a legacy Inbox layout in place (inserting %s)",
            ", ".join(name for _, name in MIGRATION_INSERTS),
        )
    requests += _layout_requests(inbox_sheet_id)
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
    return _format_once(
        spreadsheet_id, svc, meta, inbox_sheet_id=inbox_sheet_id,
        upcoming_sheet_id=tabs[UPCOMING_TAB], migrated=state == HEADER_LEGACY,
    )


def _format_once(
    spreadsheet_id: str, svc, meta: dict, *, inbox_sheet_id: int, upcoming_sheet_id: int,
    migrated: bool,
) -> str:
    """Apply the look unless the marker says it is already there.

    Its own batch, after the layout batch and the header/formula writes, for
    two reasons: the layout batch carries the migration and is the proof of
    editability, and a formatting request Google refuses must neither roll
    the migration back nor turn into ``not_editable``; and the header cells
    must exist before they are styled. The batch itself is atomic with the
    marker as its last request, so a refusal leaves no half-look and no
    marker — the next hourly check tries again. A refusal is logged (with
    no sheet id: ``_run``'s messages never carry one) and returned as
    :data:`FORMAT_FAILED`; rows are still written."""
    if sheet_style.is_formatted(meta) and not migrated:
        return FORMAT_ALREADY
    try:
        live = _run(
            svc.spreadsheets().get(
                spreadsheetId=spreadsheet_id, fields=sheet_style.FORMAT_READ_FIELDS
            ),
            what="format read",
        )
        requests = sheet_style.format_requests(
            live, inbox_sheet_id=inbox_sheet_id, upcoming_sheet_id=upcoming_sheet_id
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

    Row 1 must be the current agent header: every append and update is
    positional, and a legacy or edited header means the id column is not
    where :data:`MESSAGE_ID_RANGE` looks. That is :class:`LayoutMismatch`,
    raised before anything is written — never an empty map that would
    re-append every message."""
    svc = svc or service()
    data = _run(
        svc.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id, ranges=[INBOX_HEADER_RANGE, MESSAGE_ID_RANGE],
        ),
        what="id column read",
    )
    ranges = data.get("valueRanges") or []
    head = _first_row(ranges[0] if ranges else {})
    if header_state(head) != HEADER_CURRENT:
        raise LayoutMismatch(
            "The Inbox tab's header row does not match the agent's columns; nothing was written."
        )
    out: dict[str, int] = {}
    ids = (ranges[1] if len(ranges) > 1 else {}).get("values") or []
    for row_number, row in enumerate(ids, start=1):
        if row_number == 1:
            continue  # the header
        value = str(row[0]).strip() if row else ""
        if value and value not in out:
            out[value] = row_number
    return out


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


def append(spreadsheet_id: str, rows: list[list[str]], *, svc=None) -> list[int]:
    """One ``values.append`` for every new row of this fire. Returns the row
    numbers the API reports, in order, or ``[]`` when it reported none."""
    if not rows:
        return []
    svc = svc or service()
    resp = _run(
        svc.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{INBOX_TAB}!A:{LAST_AGENT_COL}",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ),
        what="append",
    )
    match = _A1_FIRST_ROW.search(str((resp.get("updates") or {}).get("updatedRange") or ""))
    if not match:
        return []
    first = int(match.group(1))
    return [first + offset for offset in range(len(rows))]


def update(spreadsheet_id: str, row_values: dict[int, list[str]], *, svc=None) -> int:
    """One ``values.batchUpdate`` for the retried rows: ``{row: the agent's
    cells}`` written to ``A{row}:I{row}`` and nowhere else."""
    if not row_values:
        return 0
    svc = svc or service()
    data = [
        {"range": f"{INBOX_TAB}!{AGENT_RANGE.format(row=row)}", "values": [values]}
        for row, values in sorted(row_values.items())
    ]
    _run(
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"valueInputOption": "RAW", "data": data}
        ),
        what="row update",
    )
    return len(data)
