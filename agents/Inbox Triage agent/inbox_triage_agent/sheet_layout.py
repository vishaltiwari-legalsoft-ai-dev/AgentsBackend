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
(:func:`sort_requests`) — whole rows, every column — to bring a sheet
written before this rule into the same order, and records that it did
(:func:`order_marker_requests`) only after reading the tab back and finding
it newest first (:func:`first_out_of_order`). A tab the sort cannot put right
(:func:`sort_blocker`) is left exactly as it is, and says why.

**One reading of a date.** Placement, the newest-first check and the decision
to sort all read a Date cell through :func:`date_key`, on the value Sheets
STORES — the agent's text, or the number behind a real date — never on how
the column happens to be displayed.

**One writer at a time, proved on the sheet.** Every batch that moves rows
carries a stamp (:func:`rows_stamp`): a developer-metadata entry whose id is
the next in a sequence. Sheets refuses to create an id that exists, and a
batch is atomic, so of two batches planned from the same reading of the tab
exactly one is applied — whether the second is another fire's or this one's
own, sent again after a reply was lost.

"Upcoming" is a formula view over "Inbox": nearest deadline first, blanks
excluded, rows she has marked Done hidden, overdue ones marked and listed
after the open ones, only the columns needed to act. Nothing the agent writes can
disturb her ordering, and nothing she reorders can break the view.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

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
_DATE_FORMAT = "%Y-%m-%d %H:%M"
#: Day zero of the number Sheets stores for a real date.
_SERIAL_EPOCH = datetime(1899, 12, 30)
#: Serial numbers read as dates: 1954-10-03 to 2173-10-13. A number outside
#: that is a number she typed, not a date.
_SERIAL_RANGE = (20000.0, 100000.0)


def date_key(cell) -> datetime | None:
    """When a Date cell says its message arrived, or ``None`` when it does
    not say.

    Read from the value Sheets STORES (``UNFORMATTED_VALUE``), so the answer
    does not depend on how she has the column displayed: the agent's own
    text (``2026-09-12 09:00``) is parsed, and a real date — a cell she
    re-typed, or a column she converted — arrives as the number of days
    since 1899-12-30 and is read as that. Anything else (blank, a line she
    typed, a date typed as other text) has no key: it is neither older nor
    newer than anything."""
    if isinstance(cell, bool):
        return None
    if isinstance(cell, (int, float)):
        if not _SERIAL_RANGE[0] <= cell <= _SERIAL_RANGE[1]:
            return None
        return _SERIAL_EPOCH + timedelta(seconds=round(float(cell) * 86400))
    text = str(cell or "").strip()
    if not _DATE_CELL.match(text):
        return None
    try:
        return datetime.strptime(text, _DATE_FORMAT)
    except ValueError:  # 2026-02-31 09:00 has the shape and is no date
        return None


def is_date_cell(cell) -> bool:
    return date_key(cell) is not None


def is_real_date(cell) -> bool:
    """A date held as a number, not as the agent's text."""
    return not isinstance(cell, str) and date_key(cell) is not None


def first_out_of_order(dates: list) -> int | None:
    """The 1-based sheet row of the first dated row that is NEWER than the
    dated row above it; ``None`` when the column is newest first. ``dates``
    is the Date column as read, header included. Rows with no date are not
    part of the order and are passed over."""
    above: datetime | None = None
    for index in range(1, len(dates)):
        key = date_key(dates[index])
        if key is None:
            continue
        if above is not None and key > above:
            return index + 1
        above = key
    return None


def is_newest_first(meta: dict) -> bool:
    """Does a ``spreadsheets.get`` result carry the order marker at this
    version?"""
    return any(
        entry.get("metadataKey") == ORDER_MARKER_KEY
        and str(entry.get("metadataValue") or "") == ORDER_VERSION
        for entry in (meta or {}).get("developerMetadata") or []
    )


def sort_requests(inbox_sheet_id: int) -> list[dict]:
    """The one-time correction: sort every row below the header by Date,
    newest first.

    The range names no columns, so it is the whole width of the tab: a row
    moves with every cell in it — her Status and Notes, and any column she
    added to the right of them. Nothing is rewritten; cells are only moved.
    Message ID breaks a tie inside one minute (Gmail's ids grow with time and
    are all the same length), which costs nothing when there is no tie.

    No marker here. Sheets sorts around rows it is not showing and puts every
    text cell above every number, so that the sort was APPLIED does not mean
    the tab is in order: the marker is written only once the tab has been
    read back and found newest first."""
    return [{
        "sortRange": {
            "range": {"sheetId": inbox_sheet_id, "startRowIndex": 1},
            "sortSpecs": [
                {"dimensionIndex": COL_DATE - 1, "sortOrder": "DESCENDING"},
                {"dimensionIndex": COL_MESSAGE_ID - 1, "sortOrder": "DESCENDING"},
            ],
        }
    }]


def order_marker_requests(meta: dict) -> list[dict]:
    """Record that the tab is newest first: any marker of another version
    dropped, this version's written. Sent only after the order was read off
    the sheet."""
    requests: list[dict] = [
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


# --------------------------------------------------------------------------- #
# What the one-time sort cannot put right, in words she can act on
# --------------------------------------------------------------------------- #

_NOTE_HEAD = "The Inbox tab has not been put in newest-first order yet: "
#: How every note ends: what is still happening, and when the sort is tried
#: again. ``{retry}`` is filled by the writer, which owns the interval.
_NOTE_TAIL = (
    " New mail is still added at the top. The agent tries the sort again {retry}."
)


def a1_range(grid_range: dict) -> str:
    """A ``GridRange`` as she would read it in the sheet (``K3:K4``)."""
    first_col = int(grid_range.get("startColumnIndex") or 0)
    last_col = grid_range.get("endColumnIndex")
    first_row = int(grid_range.get("startRowIndex") or 0)
    last_row = grid_range.get("endRowIndex")
    left = f"{column_letter(first_col + 1)}{first_row + 1}"
    right = (
        f"{column_letter(int(last_col)) if last_col else ''}{int(last_row) if last_row else ''}"
    )
    return left if left == right or not right else f"{left}:{right}"


def _row_spans(rows: list[int]) -> str:
    """1-based rows as she would say them: ``4-5, 9``. At most four spans."""
    spans: list[list[int]] = []
    for row in sorted(rows):
        if spans and row == spans[-1][1] + 1:
            spans[-1][1] = row
        else:
            spans.append([row, row])
    words = [str(a) if a == b else f"{a}-{b}" for a, b in spans[:4]]
    return ", ".join(words) + (" and more" if len(spans) > 4 else "")


def vertical_merges(merges: list[dict] | None, *, from_row: int = 1) -> list[dict]:
    """The merges that span more than one row and reach below ``from_row``
    (0-based) — the ones Sheets refuses to sort or to move a row through."""
    return [
        merge for merge in merges or []
        if int(merge.get("endRowIndex") or 0) - int(merge.get("startRowIndex") or 0) > 1
        and int(merge.get("endRowIndex") or 0) > from_row
    ]


def sort_blocker(
    dates: list, *, merges: list[dict] | None, hidden_by_filter: list[int],
    hidden_by_user: list[int],
) -> str | None:
    """Why ``sortRange`` would not leave this tab newest first — the first
    reason that applies, as the middle of a sentence — or ``None`` when it
    would. Each was seen on a real sheet: Sheets refuses a range holding a
    vertical merge; it sorts AROUND rows a filter or she has hidden, leaving
    them where they were; and sorted descending it puts every text cell above
    every number, so a column of both comes out as two runs.

    ``hidden_*`` are 1-based sheet rows. The words name what to change and
    where."""
    vertical = vertical_merges(merges)
    if vertical:
        where = ", ".join(a1_range(merge) for merge in vertical[:4])
        more = " and more" if len(vertical) > 4 else ""
        return (
            f"it has merged cells at {where}{more}, and Google Sheets cannot sort rows that "
            "are merged together. Unmerge them (Format > Merge cells > Unmerge)."
        )
    if hidden_by_filter:
        count = len(hidden_by_filter)
        return (
            f"a filter is hiding {count} row{'s' if count != 1 else ''} "
            f"(rows {_row_spans(hidden_by_filter)}), and Google Sheets sorts around hidden "
            "rows. Show every row (Data > Remove filter, or clear the filter's conditions)."
        )
    if hidden_by_user:
        return (
            f"rows {_row_spans(hidden_by_user)} are hidden, and Google Sheets sorts around "
            "hidden rows. Unhide them (select the rows on either side, right-click > Unhide "
            "rows)."
        )
    text = [i + 1 for i in range(1, len(dates)) if isinstance(dates[i], str) and is_date_cell(dates[i])]
    real = [i + 1 for i in range(1, len(dates)) if is_real_date(dates[i])]
    if text and real:
        fewer = real if len(real) <= len(text) else text
        kind = "real dates" if fewer is real else "text"
        cells = ", ".join(f"A{row}" for row in fewer[:3]) + (" and more" if len(fewer) > 3 else "")
        return (
            "the Date column holds both dates written as text and real dates "
            f"({len(fewer)} cell{'s are' if len(fewer) != 1 else ' is'} {kind}: {cells}), and "
            "Google Sheets sorts the two apart. Make column A all one kind (select the "
            "column, Format > Number > Plain text, then re-type those cells)."
        )
    return None


def order_note(reason: str, *, retry: str) -> str:
    """What the panel shows while the tab is not in order: what is in the
    way, what to do about it, and what is still happening meanwhile."""
    return _NOTE_HEAD + reason.rstrip() + _NOTE_TAIL.format(retry=retry)


def unrecorded_note(why: str, *, retry: str) -> str:
    """The tab IS newest first, and the marker that says so could not be
    written — so the agent has to look again."""
    return (
        "The Inbox tab is in newest-first order, but Google Sheets did not let the agent "
        f"record that ({why}), so it will look again. New mail is still added at the top. "
        f"The agent tries again {retry}."
    )


# --------------------------------------------------------------------------- #
# The stamp every row-moving batch carries
# --------------------------------------------------------------------------- #

#: One entry per batch that moved rows. Its metadata ID is
#: :data:`ROWS_STAMP_BASE` plus a sequence number; its value is
#: ``sequence@epoch seconds@token``.
ROWS_STAMP_KEY = "agentos.a12.rows"
ROWS_STAMP_BASE = 1_620_000_000
#: Stamps older than this are dropped by the next batch. It bounds how late a
#: batch sent earlier could still arrive and be refused by its own stamp; a
#: request Google has held for an hour does not exist.
ROWS_STAMP_KEEP_SECONDS = 3600


def _stamps(meta: dict) -> list[tuple[int, int, str, int]]:
    """``(sequence, epoch, token, metadata id)`` of every stamp, oldest first."""
    out: list[tuple[int, int, str, int]] = []
    for entry in (meta or {}).get("developerMetadata") or []:
        if entry.get("metadataKey") != ROWS_STAMP_KEY or entry.get("metadataId") is None:
            continue
        parts = str(entry.get("metadataValue") or "").split("@")
        try:
            sequence, epoch = int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            sequence, epoch = int(entry["metadataId"]) - ROWS_STAMP_BASE, 0
        out.append((sequence, epoch, parts[2] if len(parts) > 2 else "", int(entry["metadataId"])))
    return sorted(out)


def rows_sequence(meta: dict) -> int:
    """How many row-moving batches the sheet says it has taken; 0 for none."""
    stamps = _stamps(meta)
    return stamps[-1][0] if stamps else 0


def rows_stamped_by(meta: dict, token: str) -> bool:
    """Is a batch carrying ``token`` on the sheet?"""
    return bool(token) and any(stamp[2] == token for stamp in _stamps(meta))


def rows_stamp(meta: dict, *, token: str, now_epoch: int) -> list[dict]:
    """The requests that stamp a batch planned from ``meta``: FIRST in the
    batch, create the next stamp in the sequence; then drop the stamps that
    have aged out.

    The ID is a function of what was read, so two batches planned from the
    same reading ask for the same ID and Sheets applies one of them. An ID
    some other metadata already holds is stepped over — by both, equally."""
    taken = {
        int(entry["metadataId"]) for entry in (meta or {}).get("developerMetadata") or []
        if entry.get("metadataId") is not None
    }
    sequence = rows_sequence(meta) + 1
    while ROWS_STAMP_BASE + sequence in taken:
        sequence += 1
    requests: list[dict] = [{"createDeveloperMetadata": {"developerMetadata": {
        "metadataId": ROWS_STAMP_BASE + sequence,
        "metadataKey": ROWS_STAMP_KEY,
        "metadataValue": f"{sequence}@{int(now_epoch)}@{token}",
        "location": {"spreadsheet": True},
        "visibility": "DOCUMENT",
    }}}]
    stamps = _stamps(meta)
    requests += [
        {"deleteDeveloperMetadata": {"dataFilter": {
            "developerMetadataLookup": {"metadataId": metadata_id}}}}
        for _sequence, epoch, _token, metadata_id in stamps[:-1]  # the latest always stays
        if now_epoch - epoch > ROWS_STAMP_KEEP_SECONDS
    ]
    return requests


@dataclass(frozen=True)
class Block:
    """New rows that go in at one place. ``at`` is the 0-based row index, in
    the sheet AS READ, that the first of them takes; ``rows`` are positions
    in the list of new rows handed to :func:`place`, newest first."""

    at: int
    rows: tuple[int, ...]


def place(dates: list, new_dates: list[str], *, end: int) -> list[Block]:
    """Where each new row goes: directly above the first existing row that is
    as old as it or older, and below the last row when none is.

    ``dates`` is the Date column as STORED, header included (index 0 is row
    1) — text for the agent's own cells, a number for a real date, both read
    by :func:`date_key`. ``end`` is the index just past the last row that
    holds anything. A cell that says no date — blank, or a row she typed
    herself — is neither older nor newer than anything and is simply passed
    over.

    On a newest-first sheet that is the top for new mail, the bottom for the
    backfill, and the right place in between for mail that arrived while the
    inbox was disconnected. Blocks come back top to bottom."""
    wanted_keys = [date_key(value) or datetime.max for value in new_dates]
    order = sorted(range(len(new_dates)), key=lambda i: wanted_keys[i], reverse=True)
    keys = [date_key(cell) for cell in dates]
    blocks: list[tuple[int, list[int]]] = []
    pointer = 1
    for index in order:
        wanted = wanted_keys[index]
        # The new rows are taken newest first, so the place for one is never
        # above the place for the one before it: one pass down the column.
        while pointer < end and not (
            pointer < len(keys) and keys[pointer] is not None and keys[pointer] <= wanted
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


def insert_requests(
    inbox_sheet_id: int, blocks: list[Block], rows: list[list[str]], *,
    grid_rows: int | None = None, merges: list[dict] | None = None,
) -> list[dict]:
    """The blocks as structural requests, BOTTOM block first, so every index
    is the one that was read: an insert lower down never moves a row above it.

    A block that becomes the first data rows is not inserted AT row 2. The
    tab's banding, like any range that begins on row 2, is pushed down by an
    insert at its own first row and would start below the new rows, leaving
    them unbanded. So the new rows are inserted just BELOW the current first
    row — inside every such range, which therefore grows to take them — and
    that one row is then moved, whole, to beneath them.

    Two sheets cannot take that, and mail must reach them all the same
    (``grid_rows`` and ``merges`` are the tab's, read with the positions):

    - **Row 2 is merged with the row below it.** Sheets will not move a row
      out of a merge. The block is inserted AT row 2, above the merge, which
      moves down whole with every row under it.
    - **The grid ends at the header** — she deleted every empty row. There is
      no row 2 to insert below; the block is added after row 1, which is the
      one place Sheets allows, and its cells are given back the plain look
      that an insert after the header would otherwise take from it."""
    requests: list[dict] = []
    top_is_merged = any(
        int(merge.get("startRowIndex") or 0) <= 1 for merge in vertical_merges(merges)
    )
    for block in sorted(blocks, key=lambda b: b.at, reverse=True):
        count = len(block.rows)
        at = max(block.at, 1)
        if at == 1 and grid_rows is not None and grid_rows < 2:
            requests.append({"insertDimension": {
                "range": {"sheetId": inbox_sheet_id, "dimension": "ROWS",
                          "startIndex": 1, "endIndex": 1 + count},
                "inheritFromBefore": True,  # the only kind Sheets takes at the grid's end
            }})
            requests.append({"repeatCell": {
                "range": {"sheetId": inbox_sheet_id, "startRowIndex": 1, "endRowIndex": 1 + count},
                "cell": {"userEnteredFormat": {}},
                "fields": "userEnteredFormat",
            }})
        elif at == 1 and top_is_merged:
            requests.append({"insertDimension": {
                "range": {"sheetId": inbox_sheet_id, "dimension": "ROWS",
                          "startIndex": 1, "endIndex": 1 + count},
                "inheritFromBefore": False,  # a data row's look, never the header's
            }})
        elif at == 1:
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
