"""The sheet as a contract: which columns are the agent's, which are hers,
and the one formula that keeps deadlines visible. Pure; no Sheets I/O.

She reorders and edits the "Inbox" tab freely. So the agent never sorts,
never deletes, never writes outside its own columns, and finds a row again
by the hidden Message ID column — the hub's own records are the checkpoint,
the sheet's id column is the reconciliation key. That is also why a
reconnect after a disconnect updates rows instead of duplicating them.

"Upcoming" is a formula view over "Inbox": nearest deadline first, blanks
excluded, rows she has marked Done hidden. Nothing the agent writes can
disturb her ordering, and nothing she reorders can break the view.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

INBOX_TAB = "Inbox"
UPCOMING_TAB = "Upcoming"

#: Agent-owned columns, in sheet order (A..H). ``Message ID`` is hidden.
AGENT_COLUMNS: tuple[str, ...] = (
    "Date",
    "From",
    "Subject",
    "Category",
    "Summary",
    "Deadline",
    "Link",
    "Message ID",
)
#: Hers, after the agent's (I, J). Written once as headers; never touched again.
USER_COLUMNS: tuple[str, ...] = ("Status", "Notes")
HEADERS: tuple[str, ...] = AGENT_COLUMNS + USER_COLUMNS

STATUS_OPTIONS: tuple[str, ...] = ("New", "In progress", "Done", "Ignore")
STATUS_DONE = "Done"

#: 1-based sheet indexes the writer needs by name.
COL_DEADLINE = AGENT_COLUMNS.index("Deadline") + 1  # F
COL_MESSAGE_ID = AGENT_COLUMNS.index("Message ID") + 1  # H
COL_STATUS = len(AGENT_COLUMNS) + USER_COLUMNS.index("Status") + 1  # I

#: A1 range of the agent's columns for one row, e.g. ``Inbox!A7:H7``.
AGENT_RANGE = f"A{{row}}:{chr(ord('A') + len(AGENT_COLUMNS) - 1)}{{row}}"
#: The Message ID column, whole, for the id → row map.
MESSAGE_ID_RANGE = f"{INBOX_TAB}!{chr(ord('A') + COL_MESSAGE_ID - 1)}:{chr(ord('A') + COL_MESSAGE_ID - 1)}"

#: One formula in Upcoming!A2. FILTER keeps rows with a deadline that are not
#: Done; SORT orders by the Deadline column (6th), ascending.
UPCOMING_FORMULA = (
    f'=IFERROR(SORT(FILTER({INBOX_TAB}!A2:J, {INBOX_TAB}!F2:F<>"", '
    f'{INBOX_TAB}!I2:I<>"{STATUS_DONE}"), {COL_DEADLINE}, TRUE), '
    f'"No open deadlines")'
)

MESSAGE_URL = "https://mail.google.com/mail/u/0/#inbox/{message_id}"


@dataclass(frozen=True)
class RowFacts:
    """What one message contributes to its row. ``category`` is one of the
    contract's categories or ``needs_review``; ``summary`` is empty then."""

    message_id: str
    received_at: datetime
    sender: str
    subject: str
    category: str
    summary: str
    deadline: date | None


def message_link(message_id: str) -> str:
    return MESSAGE_URL.format(message_id=message_id)


def agent_values(facts: RowFacts) -> list[str]:
    """The agent's eight cells, as strings, in :data:`AGENT_COLUMNS` order.
    Dates are ISO so the sheet sorts them as text and as dates alike."""
    return [
        facts.received_at.strftime("%Y-%m-%d %H:%M"),
        facts.sender,
        facts.subject or "(no subject)",
        facts.category,
        facts.summary,
        facts.deadline.isoformat() if facts.deadline else "",
        message_link(facts.message_id),
        facts.message_id,
    ]


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
