"""The Sheets writes, with the hub's own identity (ADC on the spreadsheets
scope). She shares her sheet with the service account as an editor; the hub
never holds her Google identity for Sheets.

What this module never does: sort, delete, or write outside the agent's
columns A:H (the header row is the one exception, written once and repaired
only in A:H). Her Status and Notes columns and her row order are hers. Rows
are found again through the hidden Message ID column — the reconciliation
key — never by remembered row numbers, because she reorders freely.

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
from dataclasses import dataclass

from app.services.google_http import (
    _RETRY_ATTEMPTS, cached_credentials, execute_with_retry, refresh_if_stale, timed_http,
)

from . import offline, refuse_if_offline
from .sheet_layout import (
    AGENT_COLUMNS, AGENT_RANGE, COL_MESSAGE_ID, COL_STATUS, HEADERS, INBOX_TAB,
    MESSAGE_ID_RANGE, STATUS_OPTIONS, UPCOMING_FORMULA, UPCOMING_TAB,
)

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


class SheetsRefused(SheetsUnavailable):
    """Google answered a Sheets or Drive call with a non-transient HTTP
    status. ``status`` lets :func:`check` map 403/404 to the panel's words;
    everyone else can treat it as the :class:`SheetsUnavailable` it is."""

    def __init__(self, message: str, *, status: int | None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SheetCheck:
    status: str  # one of the CHECK_* values
    title: str


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
        setup(spreadsheet_id, svc=svc, meta=meta)
    except SheetsRefused as exc:
        if exc.status == 403:
            return SheetCheck(CHECK_NOT_EDITABLE, "")
        raise
    return SheetCheck(CHECK_OK, title)


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
            spreadsheetId=spreadsheet_id, fields="properties.title,sheets.properties"
        ),
        what="metadata",
    )


def setup(spreadsheet_id: str, *, svc=None, meta: dict | None = None) -> None:
    """Idempotent: the two tabs exist, row 1 carries the headers, row 1 is
    frozen, the Message ID column is hidden, Status has its dropdown, and
    Upcoming!A2 holds the formula view. Re-running changes nothing that is
    already right; the layout write always happens and is what proves the
    hub can edit the sheet.

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
    _run(
        svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": _layout_requests(inbox_sheet_id)},
        ),
        what="layout",
    )

    read = _run(
        svc.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[f"{INBOX_TAB}!A1:J1", f"{UPCOMING_TAB}!A1:J1", f"{UPCOMING_TAB}!A2"],
        ),
        what="header read",
    )
    ranges = read.get("valueRanges") or [{}, {}, {}]
    inbox_head = _first_row(ranges[0] if len(ranges) > 0 else {})
    upcoming_head = _first_row(ranges[1] if len(ranges) > 1 else {})
    upcoming_a2 = _first_row(ranges[2] if len(ranges) > 2 else {})

    header_writes: list[dict] = []
    if not inbox_head:
        header_writes.append({"range": f"{INBOX_TAB}!A1:J1", "values": [list(HEADERS)]})
    elif inbox_head[: len(AGENT_COLUMNS)] != list(AGENT_COLUMNS):
        # Repair the agent's own header cells only; hers (I, J) are never touched.
        header_writes.append({"range": f"{INBOX_TAB}!A1:H1", "values": [list(AGENT_COLUMNS)]})
    if not upcoming_head:
        header_writes.append({"range": f"{UPCOMING_TAB}!A1:J1", "values": [list(HEADERS)]})
    if header_writes:
        _run(
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": header_writes},
            ),
            what="header write",
        )
    if not upcoming_a2:
        _run(
            svc.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{UPCOMING_TAB}!A2",
                valueInputOption="USER_ENTERED",  # a formula, so it must be entered as one
                body={"values": [[UPCOMING_FORMULA]]},
            ),
            what="formula write",
        )


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
    fire. The first occurrence wins if she ever duplicated a row by hand."""
    svc = svc or service()
    data = _run(
        svc.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=MESSAGE_ID_RANGE),
        what="id column read",
    )
    out: dict[str, int] = {}
    for row_number, row in enumerate(data.get("values") or [], start=1):
        if row_number == 1:
            continue  # the header
        value = str(row[0]).strip() if row else ""
        if value and value not in out:
            out[value] = row_number
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
            range=f"{INBOX_TAB}!A:H",
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
    """One ``values.batchUpdate`` for the retried rows: ``{row: eight cells}``
    written to ``A{row}:H{row}`` and nowhere else."""
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
