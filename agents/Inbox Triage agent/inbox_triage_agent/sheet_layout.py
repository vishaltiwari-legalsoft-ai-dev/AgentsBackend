"""The sheet as a contract: which columns are the agent's, which are hers,
and the one formula that keeps deadlines visible. Pure; no Sheets I/O.

She reorders and edits the "Inbox" tab freely. So the agent never deletes,
never writes outside its own columns, and finds a row again by the hidden
Message ID column — the hub's own records are the checkpoint, the sheet's id
column is the reconciliation key. That is also why a reconnect after a
disconnect updates rows instead of duplicating them.

**Newest first.** A new row is INSERTED, whole, directly above the first row
that is as old as it or older (:func:`place`), so the newest mail is the
first data row and an older message found later still lands where its date
puts it. Inserting never overwrites a cell: every existing row, hers
included, moves down intact. The agent sorts the tab exactly ONCE per sheet
(:func:`order_requests`) — whole rows, every column — to bring a sheet
written before this rule into the same order, and records that it did in a
developer-metadata marker so it never does it again.

"Upcoming" is a formula view over "Inbox": nearest deadline first, blanks
excluded, rows she has marked Done hidden, overdue ones marked and listed
after the open ones, only the columns needed to act. Nothing the agent writes can
disturb her ordering, and nothing she reorders can break the view.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from .triage import NEEDS_REVIEW

INBOX_TAB = "Inbox"
UPCOMING_TAB = "Upcoming"

#: Agent-owned columns, in sheet order (A..I). ``Message ID`` is hidden.
#: Read left to right they answer: when, who, about what, what kind, what it
#: says, what I have to do, by when.
AGENT_COLUMNS: tuple[str, ...] = (
    "Date",
    "From",
    "Subject",
    "Category",
    "Summary",
    "Action",
    "Deadline",
    "Link",
    "Message ID",
)
#: Hers, after the agent's (J, K). Written once as headers; never touched again.
USER_COLUMNS: tuple[str, ...] = ("Status", "Notes")
HEADERS: tuple[str, ...] = AGENT_COLUMNS + USER_COLUMNS

#: The agent's columns as the first release wrote them (A..H), before Action.
#: A sheet whose row 1 starts with exactly these is migrated in place by
#: inserting :data:`MIGRATION_INSERTS` — see ``sheet_writer.setup``.
LEGACY_AGENT_COLUMNS: tuple[str, ...] = (
    "Date", "From", "Subject", "Category", "Summary", "Deadline", "Link", "Message ID",
)
#: ``(0-based column index, header)`` inserted into a legacy sheet, left to
#: right. Inserting a whole column shifts every cell to its right — her Status
#: and Notes, the hidden id, their formatting — intact.
MIGRATION_INSERTS: tuple[tuple[int, str], ...] = ((AGENT_COLUMNS.index("Action"), "Action"),)

STATUS_OPTIONS: tuple[str, ...] = ("New", "In progress", "Done", "Ignore")
STATUS_DONE = "Done"
STATUS_IN_PROGRESS = "In progress"


def column_letter(col: int) -> str:
    """1-based column number → A1 letter (A..Z is all this sheet needs)."""
    if not 1 <= col <= 26:
        raise ValueError(f"column {col} is outside A..Z")
    return chr(ord("A") + col - 1)


#: 1-based sheet indexes, by name — nothing below hard-codes a letter.
COL_DATE = AGENT_COLUMNS.index("Date") + 1  # A
COL_FROM = AGENT_COLUMNS.index("From") + 1  # B
COL_SUBJECT = AGENT_COLUMNS.index("Subject") + 1  # C
COL_CATEGORY = AGENT_COLUMNS.index("Category") + 1  # D
COL_SUMMARY = AGENT_COLUMNS.index("Summary") + 1  # E
COL_ACTION = AGENT_COLUMNS.index("Action") + 1  # F
COL_DEADLINE = AGENT_COLUMNS.index("Deadline") + 1  # G
COL_LINK = AGENT_COLUMNS.index("Link") + 1  # H
COL_MESSAGE_ID = AGENT_COLUMNS.index("Message ID") + 1  # I
COL_STATUS = len(AGENT_COLUMNS) + USER_COLUMNS.index("Status") + 1  # J
LAST_COL = column_letter(len(HEADERS))  # K
LAST_AGENT_COL = column_letter(len(AGENT_COLUMNS))  # I

#: The Message ID column, whole, for the id → row map.
MESSAGE_ID_RANGE = f"{INBOX_TAB}!{column_letter(COL_MESSAGE_ID)}:{column_letter(COL_MESSAGE_ID)}"
#: The Date column, whole — read together with the ids when rows are placed.
DATE_RANGE = f"{INBOX_TAB}!{column_letter(COL_DATE)}:{column_letter(COL_DATE)}"
#: Every column of Inbox, whole — the agent's cells AND hers. Read only by
#: the Worktree pass, which needs her Status to know what is already done.
INBOX_ROWS_RANGE = f"{INBOX_TAB}!A:{LAST_COL}"
#: Row 1 of Inbox, wide enough to see a legacy or current header and hers.
INBOX_HEADER_RANGE = f"{INBOX_TAB}!A1:Z1"

#: Upcoming shows what someone needs to act, nearest deadline first: a
#: computed "Due" column, then these Inbox columns in the order CHOOSECOLS
#: picks them.
UPCOMING_COLUMNS: tuple[tuple[str, int], ...] = (
    ("Deadline", COL_DEADLINE),
    ("Action", COL_ACTION),
    ("From", COL_FROM),
    ("Subject", COL_SUBJECT),
    ("Summary", COL_SUMMARY),
    ("Received", COL_DATE),
    ("Status", COL_STATUS),
    ("Link", COL_LINK),
)
#: Values of the leading "Due" column. Chosen so that sorting it descending
#: puts every still-open deadline above every overdue one.
DUE_UPCOMING = "Upcoming"
DUE_OVERDUE = "Overdue"
UPCOMING_HEADERS: tuple[str, ...] = ("Due",) + tuple(name for name, _ in UPCOMING_COLUMNS)


WORKTREE_TAB = "Worktree"
#: The Worktree tab's columns, in sheet order (A..H). Read left to right they
#: answer: what is this, what kind, whose move, what do I do, how long has it
#: been sitting, by when, how much mail, and where is the last one. All eight
#: are the agent's: the tab is a view it rewrites, so there is deliberately no
#: column of hers here — a note would be attached to a row whose position
#: changes with the next build. Notes belong on the Inbox row, which the agent
#: never reorders and never rewrites.
WORKTREE_HEADERS: tuple[str, ...] = (
    "Process", "Type", "Status", "Next action", "Waiting since", "Due", "Mails", "Latest",
)
WORKTREE_LAST_COL = column_letter(len(WORKTREE_HEADERS))  # H
#: Written from row 2 down, always the full rectangle — see
#: ``sheet_writer.write_worktree``.
WORKTREE_ROWS_RANGE = f"{WORKTREE_TAB}!A2:{WORKTREE_LAST_COL}{{last}}"
WORKTREE_HEADER_RANGE = f"{WORKTREE_TAB}!A1:Z1"
#: Records that the agent created the Worktree tab, and which tab that was.
#: A tab named Worktree with no marker is hers and is never written to.
WORKTREE_MARKER_KEY = "agentos.a12.worktree"


def _whole(col: int) -> str:
    letter = column_letter(col)
    return f"{INBOX_TAB}!{letter}:{letter}"


#: One formula in Upcoming!A2. Whole-column references only: a row-numbered
#: one (``Inbox!A2:J``) is shifted by Sheets every time the agent's append
#: inserts rows, and drifted to ``A85`` on the first live sheet. The header
#: row is dropped by ``ROW()>1`` instead. FILTER keeps rows with a deadline
#: that are not Done.
#:
#: Overdue deadlines are kept, not hidden — an unfinished obligation that has
#: passed is still hers to close or mark Done — but they no longer sit on top:
#: the leading "Due" column says Overdue or Upcoming (today counts as
#: Upcoming), and SORT puts every Upcoming row first, each block by Deadline
#: ascending. Deadline cells are ISO text written RAW, so comparing them with
#: TODAY() formatted as ISO text is a date comparison.
UPCOMING_FORMULA = (
    f"=ARRAYFORMULA(IFERROR(SORT(FILTER(HSTACK("
    f'IF({_whole(COL_DEADLINE)}<TEXT(TODAY(), "yyyy-mm-dd"), "{DUE_OVERDUE}", "{DUE_UPCOMING}"), '
    f"CHOOSECOLS({INBOX_TAB}!A:{LAST_COL}, {', '.join(str(c) for _, c in UPCOMING_COLUMNS)})), "
    f"ROW({_whole(COL_DATE)})>1, "
    f'{_whole(COL_DEADLINE)}<>"", '
    f'{_whole(COL_STATUS)}<>"{STATUS_DONE}"), 1, FALSE, 2, TRUE), '
    f'"No open deadlines"))'
)


def same_formula(stored: str, wanted: str = UPCOMING_FORMULA) -> bool:
    """Is the formula Sheets hands back the one we wrote? Sheets may re-space
    a formula it stores, so whitespace and letter case do not count."""

    def norm(text: str) -> str:
        return "".join(str(text or "").split()).upper()

    return norm(stored) == norm(wanted)


HEADER_EMPTY = "empty"
HEADER_CURRENT = "current"
HEADER_LEGACY = "legacy"
HEADER_REPAIR = "repair"


def header_state(head: list[str]) -> str:
    """What row 1 of Inbox says about the rows beneath it.

    ``legacy`` is decided on the columns the first release put where Action
    now sits (F..H held Deadline, Link, Message ID). Two of those three still
    in place means the data rows are in the legacy layout, even if she renamed
    a header cell — so the rows get a column inserted, not a header written
    over them. ``repair`` is a current-layout sheet whose agent header cells
    were edited: only the header row is rewritten."""
    cells = [str(c).strip() for c in head]
    if not any(cells):
        return HEADER_EMPTY
    if cells[: len(AGENT_COLUMNS)] == list(AGENT_COLUMNS):
        return HEADER_CURRENT
    first_new = MIGRATION_INSERTS[0][0]
    legacy_tail = LEGACY_AGENT_COLUMNS[first_new:]
    seen_tail = cells[first_new: first_new + len(legacy_tail)]
    matches = sum(1 for a, b in zip(seen_tail, legacy_tail) if a == b)
    if matches >= 2 and (len(cells) <= first_new or cells[first_new] != "Action"):
        return HEADER_LEGACY
    return HEADER_REPAIR


# --------------------------------------------------------------------------- #
# Newest first — where a new row goes, and the one-time sort
# --------------------------------------------------------------------------- #

#: Records that the Inbox tab was brought into newest-first order, once.
#: Spreadsheet-level developer metadata, like the formatting marker: it
#: travels with the sheet, so a sheet connected a second time is not sorted a
#: second time.
ORDER_MARKER_KEY = "agentos.a12.order"
ORDER_VERSION = "newest-first/1"

#: How :func:`agent_values` writes the Date cell. ISO text, so comparing two
#: cells as text is comparing them as dates.
_DATE_CELL = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


def is_date_cell(cell: str) -> bool:
    return bool(_DATE_CELL.match(str(cell or "").strip()))


def is_newest_first(meta: dict) -> bool:
    """Does a ``spreadsheets.get`` result carry the order marker at this
    version?"""
    return any(
        entry.get("metadataKey") == ORDER_MARKER_KEY
        and str(entry.get("metadataValue") or "") == ORDER_VERSION
        for entry in (meta or {}).get("developerMetadata") or []
    )


def order_requests(meta: dict, inbox_sheet_id: int) -> list[dict]:
    """The one-time correction as ONE atomic batch: sort every row below the
    header by Date, newest first, then write the marker.

    The range names no columns, so it is the whole width of the tab: a row
    moves with every cell in it — her Status and Notes, and any column she
    added to the right of them. Nothing is rewritten; cells are only moved.
    Message ID breaks a tie inside one minute (Gmail's ids grow with time and
    are all the same length), which costs nothing when there is no tie.

    The marker is the LAST request of the same batch, so it exists exactly
    when the sort happened: a refusal leaves the sheet as it was and
    unmarked, and the next attempt starts from the beginning — which is also
    where it would have to start, because a sort has no half-way state."""
    requests: list[dict] = [{
        "sortRange": {
            "range": {"sheetId": inbox_sheet_id, "startRowIndex": 1},
            "sortSpecs": [
                {"dimensionIndex": COL_DATE - 1, "sortOrder": "DESCENDING"},
                {"dimensionIndex": COL_MESSAGE_ID - 1, "sortOrder": "DESCENDING"},
            ],
        }
    }]
    requests += [
        {"deleteDeveloperMetadata": {"dataFilter": {
            "developerMetadataLookup": {"metadataId": entry["metadataId"]}}}}
        for entry in (meta or {}).get("developerMetadata") or []
        if entry.get("metadataKey") == ORDER_MARKER_KEY and entry.get("metadataId") is not None
    ]
    requests.append({"createDeveloperMetadata": {"developerMetadata": {
        "metadataKey": ORDER_MARKER_KEY,
        "metadataValue": ORDER_VERSION,
        "location": {"spreadsheet": True},
        "visibility": "DOCUMENT",
    }}})
    return requests


@dataclass(frozen=True)
class Block:
    """New rows that go in at one place. ``at`` is the 0-based row index, in
    the sheet AS READ, that the first of them takes; ``rows`` are positions
    in the list of new rows handed to :func:`place`, newest first."""

    at: int
    rows: tuple[int, ...]


def place(dates: list[str], new_dates: list[str], *, end: int) -> list[Block]:
    """Where each new row goes: directly above the first existing row that is
    as old as it or older, and below the last row when none is.

    ``dates`` is the Date column as read, header included (index 0 is row 1);
    ``end`` is the index just past the last row that holds anything. A cell
    that is not a date the agent wrote — blank, or a row she typed herself —
    is neither older nor newer than anything and is simply passed over.

    On a newest-first sheet that is the top for new mail, the bottom for the
    backfill, and the right place in between for mail that arrived while the
    inbox was disconnected. Blocks come back top to bottom."""
    order = sorted(range(len(new_dates)), key=lambda i: new_dates[i], reverse=True)
    blocks: list[tuple[int, list[int]]] = []
    pointer = 1
    for index in order:
        wanted = new_dates[index]
        # The new rows are taken newest first, so the place for one is never
        # above the place for the one before it: one pass down the column.
        while pointer < end and not (
            pointer < len(dates) and is_date_cell(dates[pointer])
            and str(dates[pointer]).strip() <= wanted
        ):
            pointer += 1
        if blocks and blocks[-1][0] == pointer:
            blocks[-1][1].append(index)
        else:
            blocks.append((pointer, [index]))
    return [Block(at=at, rows=tuple(rows)) for at, rows in blocks]


def landed(blocks: list[Block], count: int) -> list[int]:
    """The 1-based sheet row each new row ends up on, in the order the new
    rows were given — every block above one pushes it down."""
    out = [0] * count
    above = 0
    for block in blocks:  # top to bottom
        for offset, index in enumerate(block.rows):
            out[index] = block.at + above + offset + 1
        above += len(block.rows)
    return out


def pushed(row_index: int, blocks: list[Block]) -> int:
    """Where an EXISTING row (0-based index as read) is after the blocks went
    in: a block inserted at or above it moves it down by the block's size."""
    return row_index + sum(len(block.rows) for block in blocks if block.at <= row_index)


def _cells(values: list[str]) -> dict:
    """One row for ``updateCells``. ``stringValue`` is stored as typed and
    never evaluated — a subject line that starts with ``=`` stays text, which
    is what ``RAW`` means on the values API. An empty cell is sent as no
    value at all, so it is blank rather than an empty string."""
    return {"values": [
        {"userEnteredValue": {"stringValue": str(v)}} if str(v) != "" else {} for v in values
    ]}


def update_requests(inbox_sheet_id: int, row_values: dict[int, list[str]]) -> list[dict]:
    """The agent's cells of rows that already exist, by 0-based row index as
    read. Columns A..I and nothing to their right."""
    return [
        {"updateCells": {
            "start": {"sheetId": inbox_sheet_id, "rowIndex": row_index, "columnIndex": 0},
            "rows": [_cells(values)],
            "fields": "userEnteredValue",
        }}
        for row_index, values in sorted(row_values.items())
    ]


def insert_requests(inbox_sheet_id: int, blocks: list[Block], rows: list[list[str]]) -> list[dict]:
    """The blocks as structural requests, BOTTOM block first, so every index
    is the one that was read: an insert lower down never moves a row above it.

    A block that becomes the first data rows is not inserted AT row 2. The
    tab's banding, like any range that begins on row 2, is pushed down by an
    insert at its own first row and would start below the new rows, leaving
    them unbanded. So the new rows are inserted just BELOW the current first
    row — inside every such range, which therefore grows to take them — and
    that one row is then moved, whole, to beneath them."""
    requests: list[dict] = []
    for block in sorted(blocks, key=lambda b: b.at, reverse=True):
        count = len(block.rows)
        at = max(block.at, 1)
        if at == 1:
            requests.append({"insertDimension": {
                "range": {"sheetId": inbox_sheet_id, "dimension": "ROWS",
                          "startIndex": 2, "endIndex": 2 + count},
                "inheritFromBefore": True,
            }})
            requests.append({"moveDimension": {
                "source": {"sheetId": inbox_sheet_id, "dimension": "ROWS",
                           "startIndex": 1, "endIndex": 2},
                "destinationIndex": 2 + count,
            }})
        else:
            requests.append({"insertDimension": {
                "range": {"sheetId": inbox_sheet_id, "dimension": "ROWS",
                          "startIndex": at, "endIndex": at + count},
                "inheritFromBefore": True,
            }})
        requests.append({"updateCells": {
            "start": {"sheetId": inbox_sheet_id, "rowIndex": at, "columnIndex": 0},
            "rows": [_cells(rows[index]) for index in block.rows],
            "fields": "userEnteredValue",
        }})
    return requests


MESSAGE_URL = "https://mail.google.com/mail/u/0/#inbox/{message_id}"

#: What the reader sees in the Category column. The model answers with the
#: key; the sheet shows the words.
CATEGORY_LABELS: dict[str, str] = {
    "meeting": "Meeting",
    "finance": "Finance / billing",
    "newsletter_promo": "Newsletter / promo",
    "notification": "Automated alert",
    "action_required": "Action required",
    "reply_needed": "Reply needed",
    "fyi": "FYI / update",
    "other": "Other",
    NEEDS_REVIEW: "Needs review",
}
#: Label → key, for reading a Category cell back. Built from the same map, so
#: the two can never drift; an unrecognised label is the caller's to handle.
CATEGORY_KEYS: dict[str, str] = {label: key for key, label in CATEGORY_LABELS.items()}
#: Stamped by code in the Action column when the model said the email asks
#: nothing of the reader — never model text.
NO_ACTION = "No action — FYI"


@dataclass(frozen=True)
class RowFacts:
    """What one message contributes to its row. ``category`` is one of the
    contract's categories or ``needs_review``; ``summary`` and ``action`` are
    empty then. ``action`` is ``None`` when the email asks nothing.

    ``thread_id`` is Gmail's, and is the only field here that no column
    carries: the Inbox layout is unchanged by the Worktree, so the thread id
    lives on the message's tracking document instead."""

    message_id: str
    received_at: datetime
    sender: str
    subject: str
    category: str
    summary: str
    deadline: date | None
    action: str | None = None
    thread_id: str = ""


def message_link(message_id: str) -> str:
    return MESSAGE_URL.format(message_id=message_id)


def agent_values(facts: RowFacts) -> list[str]:
    """The agent's cells, as strings, in :data:`AGENT_COLUMNS` order. Dates
    are ISO so the sheet sorts them as text and as dates alike. A
    ``needs_review`` row carries the facts with Summary and Action blank."""
    action = "" if facts.category == NEEDS_REVIEW else (facts.action or NO_ACTION)
    values = {
        "Date": facts.received_at.strftime("%Y-%m-%d %H:%M"),
        "From": facts.sender,
        "Subject": facts.subject or "(no subject)",
        "Category": CATEGORY_LABELS.get(facts.category, facts.category),
        "Summary": facts.summary,
        "Action": action,
        "Deadline": facts.deadline.isoformat() if facts.deadline else "",
        "Link": message_link(facts.message_id),
        "Message ID": facts.message_id,
    }
    return [values[name] for name in AGENT_COLUMNS]


_SHEET_URL = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
_SHEET_ID = re.compile(r"^[a-zA-Z0-9-_]{20,}$")


def parse_sheet_ref(ref: str) -> str | None:
    """The spreadsheet id from a pasted URL or a bare id; ``None`` if neither.
    A Google Sheet id is a long URL-safe token; anything shorter is a typo
    or a Doc/folder id pasted by mistake."""
    text = (ref or "").strip()
    match = _SHEET_URL.search(text)
    if match:
        return match.group(1)
    if _SHEET_ID.match(text):
        return text
    return None


def sheet_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
