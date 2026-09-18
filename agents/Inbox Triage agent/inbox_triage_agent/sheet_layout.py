"""The sheet as a contract: which columns are the agent's, which are hers,
and the one formula that keeps deadlines visible. Pure; no Sheets I/O.

She reorders and edits the "Inbox" tab freely. So the agent never sorts,
never deletes, never writes outside its own columns, and finds a row again
by the hidden Message ID column — the hub's own records are the checkpoint,
the sheet's id column is the reconciliation key. That is also why a
reconnect after a disconnect updates rows instead of duplicating them.

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

#: A1 range of the agent's columns for one row, e.g. ``A7:I7``.
AGENT_RANGE = f"A{{row}}:{LAST_AGENT_COL}{{row}}"
#: The Message ID column, whole, for the id → row map.
MESSAGE_ID_RANGE = f"{INBOX_TAB}!{column_letter(COL_MESSAGE_ID)}:{column_letter(COL_MESSAGE_ID)}"
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
#: Stamped by code in the Action column when the model said the email asks
#: nothing of the reader — never model text.
NO_ACTION = "No action — FYI"


@dataclass(frozen=True)
class RowFacts:
    """What one message contributes to its row. ``category`` is one of the
    contract's categories or ``needs_review``; ``summary`` and ``action`` are
    empty then. ``action`` is ``None`` when the email asks nothing."""

    message_id: str
    received_at: datetime
    sender: str
    subject: str
    category: str
    summary: str
    deadline: date | None
    action: str | None = None


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
