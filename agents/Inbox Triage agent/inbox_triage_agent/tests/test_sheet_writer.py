"""The sheet writes against a fake Sheets service that keeps a real grid —
cells, inserted columns and rows, moved and sorted rows, hidden columns,
dropdowns — and records every request. So "sorts once and never again",
"never deletes, never writes outside A:I", "set-up is idempotent" and "a
legacy sheet migrates in place with nothing lost" are asserted on the cells,
not described.

Where the fake has to behave as Sheets does, it behaves as the throwaway
sheet DID on 2026-09-29/30: ``sortRange`` leaves a hidden row where it is and
puts every text cell above every number; it refuses a range holding a
vertical merge, as ``moveDimension`` refuses a row that is part of one; a
metadata ID that exists cannot be created again, and that refusal takes the
whole batch with it; an insert past the end of the grid is refused."""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

import app  # noqa: F401 — registers the agent roots on sys.path
from inbox_triage_agent import InboxOffline, sheet_layout, sheet_style, sheet_writer
from inbox_triage_agent.sheet_layout import (
    AGENT_COLUMNS, CATEGORY_LABELS, COL_ACTION, COL_MESSAGE_ID, COL_STATUS, HEADERS,
    LEGACY_AGENT_COLUMNS, STATUS_OPTIONS, UPCOMING_FORMULA, UPCOMING_HEADERS, WORKTREE_HEADERS,
)
from inbox_triage_agent.sheet_writer import LayoutMismatch, SheetsUnavailable

SID = "1abcdefghijklmnopqrstuvwxyz0123456789"
CALLER = "her@legalsoft.com"
#: What the first live sheet's Upcoming!A2 had drifted to by 2026-09-19.
DRIFTED_FORMULA = (
    '=IFERROR(SORT(FILTER(Inbox!A85:J, Inbox!F85:F<>"", Inbox!I85:I<>"Done"), 6, TRUE), '
    '"No open deadlines")'
)
#: What the coordinator hand-wrote into both live sheets on 2026-09-19 as a
#: stop-gap (whole columns, legacy letters). Also not ours: rewritten.
HOTFIX_FORMULA = (
    '=IFERROR(SORT(FILTER(Inbox!A:J, ROW(Inbox!A:A)>1, Inbox!F:F<>"", Inbox!I:I<>"Done"), 6, TRUE), '
    '"No open deadlines")'
)
LEGACY_HEADERS = list(LEGACY_AGENT_COLUMNS) + ["Status", "Notes"]


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)


def _http_error(status: int, message: str = "") -> HttpError:
    """``message`` is Google's sentence, in the body Google sends it in."""
    body = json.dumps({"error": {"code": status, "message": message}}).encode() if message else b"body"
    return HttpError(SimpleNamespace(status=status, reason=f"status {status}"), body)


class RealDate(str):
    """A Date cell that holds a REAL date: a number to Sheets, shown however
    she has the column formatted. ``RealDate("2026-09-10 10:00")`` looks
    exactly like the agent's own text; ``shown="9/10/2026 10:00:00"`` is the
    same date displayed the way the throwaway sheet displayed it."""

    serial: float

    def __new__(cls, when: str, shown: str | None = None):
        cell = super().__new__(cls, when if shown is None else shown)
        moment = datetime.strptime(when, "%Y-%m-%d %H:%M")
        cell.serial = (moment - datetime(1899, 12, 30)).total_seconds() / 86400
        return cell

    def __deepcopy__(self, memo):
        return self

    def __copy__(self):
        return self


class _Req:
    def __init__(self, result=None, error: Exception | None = None):
        self._result, self._error = result, error

    def execute(self):
        if self._error:
            raise self._error
        return self._result


_A1 = re.compile(r"^(?:(?P<tab>[^!]+)!)?(?P<c1>[A-Z]+)(?P<r1>\d*)(?::(?P<c2>[A-Z]+)(?P<r2>\d*))?$")


def _col(letters: str) -> int:
    """A → 0."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


class _Refused(Exception):
    """What Sheets would answer 400 to; the fake rolls the batch back. The
    text is Google's own, as the throwaway sheet answered it."""


def _a1_of(merge: dict) -> str:
    return sheet_layout.a1_range(merge)


class FakeSheets:
    """A spreadsheet as a grid per tab (row 0 is sheet row 1), with the
    Inbox tab's hidden columns and dropdown columns tracked by index — an
    inserted column shifts all three, exactly as Sheets does. ``fail`` maps
    a request name to the HttpError it answers.

    Also the look: conditional rules and banding per tab id, spreadsheet
    developer metadata, and every other formatting request recorded. A
    ``batchUpdate`` is atomic, as in Sheets: a request Sheets would refuse
    (a banding over one that exists, a rule index out of range) rolls the
    whole batch back and answers 400."""

    def __init__(self, *, tabs: dict[str, int] | None = None, title: str = "Her inbox"):
        self.title = title
        self.tabs: dict[str, int] = dict(tabs or {})
        self.grid: dict[str, list[list[str]]] = {t: [] for t in self.tabs}
        self.hidden: set[int] = set()
        self.dropdowns: dict[int, list[str]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, Exception] = {}
        self._next_sheet_id = 100
        self.rules: dict[int, list[dict]] = {}
        self.bandings: dict[int, list[int]] = {}
        self.metadata: list[dict] = []
        self.styled: list[dict] = []  # repeatCell / widths / heights / freezes
        self._next_meta_id = 1
        #: tab id -> the 0-based row its banding begins on
        self.band_start: dict[int, int] = {}
        #: a developer-metadata key Sheets will refuse to create (a test's)
        self.refuse_metadata_key: str | None = None
        #: Inbox's merged ranges, as ``GridRange`` (0-based, end exclusive)
        self.merges: list[dict] = []
        #: 0-based Inbox rows she has hidden, and rows her filter is hiding
        self.hidden_rows: set[int] = set()
        self.filtered_rows: set[int] = set()
        #: Inbox's grid height when a test cares; ``None`` is Sheets' 1000
        self.grid_rows: int | None = None
        #: row ranges a row write gave the plain look back to
        self.plain_rows: list[dict] = []

    # -- grid helpers ---------------------------------------------------- #
    def rows(self, tab: str) -> list[list[str]]:
        return self.grid.setdefault(tab, [])

    def cell(self, tab: str, row: int, col: int) -> str:
        grid = self.rows(tab)
        return grid[row][col] if row < len(grid) and col < len(grid[row]) else ""

    def put(self, tab: str, row: int, col: int, value: str) -> None:
        grid = self.rows(tab)
        while len(grid) <= row:
            grid.append([])
        while len(grid[row]) <= col:
            grid[row].append("")
        grid[row][col] = value

    def row(self, tab: str, row: int, width: int) -> list[str]:
        return [self.cell(tab, row, c) for c in range(width)]

    def _read(self, a1: str, render: str = "FORMATTED_VALUE") -> dict:
        m = _A1.match(a1)
        tab = m["tab"]
        c1, c2 = _col(m["c1"]), _col(m["c2"] or m["c1"])
        grid = self.rows(tab)
        r1 = int(m["r1"]) - 1 if m["r1"] else 0
        r2 = int(m["r2"]) - 1 if m["r2"] else (r1 if m["c2"] is None and m["r1"] else len(grid) - 1)

        def stored(cell):
            # What is shown is text; what is STORED is a number for a real date.
            if render == "UNFORMATTED_VALUE" and isinstance(cell, RealDate):
                return cell.serial
            return str(cell)

        values = []
        for r in range(r1, r2 + 1):
            cells = [stored(self.cell(tab, r, c)) for c in range(c1, c2 + 1)]
            while cells and cells[-1] == "":
                cells.pop()
            values.append(cells)
        while values and not values[-1]:
            values.pop()
        return {"range": a1, "values": values} if values else {"range": a1}

    def _write(self, a1: str, values: list[list[str]]) -> None:
        m = _A1.match(a1)
        tab, c1, r1 = m["tab"], _col(m["c1"]), int(m["r1"]) - 1
        for dr, row in enumerate(values):
            for dc, value in enumerate(row):
                self.put(tab, r1 + dr, c1 + dc, value)

    # -- the client surface ---------------------------------------------- #
    def spreadsheets(self):
        return SimpleNamespace(get=self._get, batchUpdate=self._batch_update, values=self._values)

    def _values(self):
        return SimpleNamespace(
            get=self._values_get, batchGet=self._values_batch_get,
            batchUpdate=self._values_batch_update, update=self._values_update,
        )

    def _answer(self, name: str, kw: dict, result_fn):
        self.calls.append((name, kw))
        if name in self.fail:
            return _Req(error=self.fail[name])
        return _Req(result_fn())

    def inbox_grid_rows(self) -> int:
        return self.grid_rows if self.grid_rows is not None else max(1000, len(self.rows("Inbox")))

    def _get(self, **kw):
        def sheet(title: str, sheet_id: int) -> dict:
            out = {
                "properties": {"title": title, "sheetId": sheet_id},
                "conditionalFormats": copy.deepcopy(self.rules.get(sheet_id, [])),
                "bandedRanges": [{"bandedRangeId": b} for b in self.bandings.get(sheet_id, [])],
            }
            if title != "Inbox":
                return out
            height = self.inbox_grid_rows()
            out["properties"]["gridProperties"] = {"rowCount": height}
            # As the throwaway sheet answered: a get that names a range is
            # told only of the merges that touch it.
            asked = [_A1.match(a1) for a1 in kw.get("ranges") or [] if "!" in a1]
            merges = [
                merge for merge in self.merges
                if not asked or any(
                    _col(m["c1"]) < merge["endColumnIndex"]
                    and _col(m["c2"] or m["c1"]) >= merge["startColumnIndex"]
                    for m in asked
                )
            ]
            if merges:
                out["merges"] = copy.deepcopy(merges)
            if kw.get("ranges"):
                out["data"] = [{"rowMetadata": [
                    {**({"hiddenByUser": True} if r in self.hidden_rows else {}),
                     **({"hiddenByFilter": True} if r in self.filtered_rows else {})}
                    for r in range(height)
                ]}]
            return out

        return self._answer("get", kw, lambda: {
            "properties": {"title": self.title},
            "sheets": [sheet(t, i) for t, i in self.tabs.items()],
            "developerMetadata": copy.deepcopy(self.metadata),
        })

    def agent_rules(self, tab: str) -> list[dict]:
        return [r for r in self.rules.get(self.tabs[tab], []) if sheet_style.is_agent_rule(r)]

    def _batch_update(self, **kw):
        requests = kw["body"]["requests"]

        def creates(key: str) -> bool:
            return any(
                ((r.get("createDeveloperMetadata") or {}).get("developerMetadata") or {})
                .get("metadataKey") == key
                for r in requests
            )

        def moves_rows(request) -> bool:
            if "moveDimension" in request:
                return True
            if "insertDimension" in request:
                return request["insertDimension"]["range"]["dimension"] == "ROWS"
            return "updateCells" in request and request["updateCells"]["start"]["rowIndex"] > 0

        if any("addSheet" in r for r in requests):
            name = "batchUpdate:addSheet"
        elif creates(sheet_style.FORMAT_MARKER_KEY):
            # The layout batch writes a marker too — the Worktree tab's — so
            # it is the FORMAT marker that names the formatting batch.
            name = "batchUpdate:format"
        elif any("sortRange" in r for r in requests):
            name = "batchUpdate:order"
        elif any(moves_rows(r) for r in requests):
            name = "batchUpdate:rows"
        elif creates(sheet_layout.ORDER_MARKER_KEY):
            name = "batchUpdate:marker"
        else:
            name = "batchUpdate:layout"

        def refuse(why: str):
            raise _Refused(why)

        def apply_style(request) -> bool:
            if "addConditionalFormatRule" in request:
                add = request["addConditionalFormatRule"]
                sheet_id = add["rule"]["ranges"][0]["sheetId"]
                rules = self.rules.setdefault(sheet_id, [])
                if not 0 <= add["index"] <= len(rules):
                    refuse("rule index out of range")
                rules.insert(add["index"], copy.deepcopy(add["rule"]))
            elif "deleteConditionalFormatRule" in request:
                d = request["deleteConditionalFormatRule"]
                rules = self.rules.setdefault(d["sheetId"], [])
                if not 0 <= d["index"] < len(rules):
                    refuse("no rule at that index")
                rules.pop(d["index"])
            elif "addBanding" in request:
                band = request["addBanding"]["bandedRange"]
                sheet_id = band["range"]["sheetId"]
                if self.bandings.get(sheet_id):
                    refuse("banding overlaps an existing banding")
                if any(band["bandedRangeId"] in b for b in self.bandings.values()):
                    refuse("banding id exists")
                self.bandings[sheet_id] = [band["bandedRangeId"]]
                self.band_start[sheet_id] = band["range"]["startRowIndex"]
            elif "deleteBanding" in request:
                wanted = request["deleteBanding"]["bandedRangeId"]
                if not any(wanted in b for b in self.bandings.values()):
                    refuse("no such banding")
                for sheet_id, ids in self.bandings.items():
                    if wanted in ids:
                        self.band_start.pop(sheet_id, None)
                self.bandings = {k: [x for x in v if x != wanted] for k, v in self.bandings.items()}
            elif "createDeveloperMetadata" in request:
                entry = dict(request["createDeveloperMetadata"]["developerMetadata"])
                if entry["metadataKey"] == self.refuse_metadata_key:
                    refuse("metadata refused")
                if entry.get("metadataId") is None:
                    entry["metadataId"] = self._next_meta_id
                    self._next_meta_id += 1
                elif any(m["metadataId"] == entry["metadataId"] for m in self.metadata):
                    refuse(
                        f"Invalid requests[0].createDeveloperMetadata: Cannot add developer "
                        f"metadata with ID [{entry['metadataId']}] because developer metadata "
                        "with that ID already exists."
                    )
                self.metadata.append(entry)
            elif "deleteDeveloperMetadata" in request:
                wanted = request["deleteDeveloperMetadata"]["dataFilter"]["developerMetadataLookup"]["metadataId"]
                self.metadata = [m for m in self.metadata if m["metadataId"] != wanted]
            elif "repeatCell" in request and name == "batchUpdate:rows":
                self.plain_rows.append(request["repeatCell"]["range"])
            elif "repeatCell" in request or (
                "updateDimensionProperties" in request
                and "hiddenByUser" not in request["updateDimensionProperties"]["fields"]
            ) or ("updateSheetProperties" in request and name == "batchUpdate:format"):
                self.styled.append(request)
            else:
                return False
            return True

        def apply():
            replies = []
            for request in requests:
                if apply_style(request):
                    replies.append({})
                    continue
                if "addSheet" in request:
                    title = request["addSheet"]["properties"]["title"]
                    self._next_sheet_id += 1
                    self.tabs[title] = self._next_sheet_id
                    self.rows(title)
                    replies.append({"addSheet": {"properties": {"title": title, "sheetId": self._next_sheet_id}}})
                    continue
                if "insertDimension" in request and (
                    request["insertDimension"]["range"]["dimension"] == "ROWS"
                ):
                    rng = request["insertDimension"]["range"]
                    at, n = rng["startIndex"], rng["endIndex"] - rng["startIndex"]
                    height = self.inbox_grid_rows()
                    if at > height:
                        refuse(f"Invalid requests[0].insertDimension: range.startIndex is larger "
                               f"than current grid size ({height})")
                    if at == height and not request["insertDimension"].get("inheritFromBefore"):
                        refuse(f"Invalid requests[0].insertDimension: range.startIndex must be "
                               f"less than the grid size ({height}) if inheritFromBefore is false.")
                    grid = self.rows("Inbox")
                    while len(grid) < at:
                        grid.append([])
                    grid[at:at] = [[] for _ in range(n)]
                    if self.grid_rows is not None:
                        self.grid_rows += n
                    # A merge the insert falls INSIDE grows; one at or below
                    # it moves down whole. Hidden rows move with their rows.
                    for merge in self.merges:
                        if merge["startRowIndex"] >= at:
                            merge["startRowIndex"] += n
                            merge["endRowIndex"] += n
                        elif merge["endRowIndex"] > at:
                            merge["endRowIndex"] += n
                    self.hidden_rows = {r + n if r >= at else r for r in self.hidden_rows}
                    self.filtered_rows = {r + n if r >= at else r for r in self.filtered_rows}
                    # As the throwaway sheet did it: a range that BEGINS at
                    # (or below) the insert is pushed down; one the insert
                    # falls inside of simply grows.
                    start = self.band_start.get(rng["sheetId"])
                    if start is not None and at <= start:
                        self.band_start[rng["sheetId"]] = start + n
                elif "insertDimension" in request:
                    rng = request["insertDimension"]["range"]
                    at, n = rng["startIndex"], rng["endIndex"] - rng["startIndex"]
                    for row in self.rows("Inbox"):
                        if len(row) > at:
                            row[at:at] = [""] * n
                    self.hidden = {c + n if c >= at else c for c in self.hidden}
                    self.dropdowns = {c + n if c >= at else c: v for c, v in self.dropdowns.items()}
                elif "moveDimension" in request:
                    source = request["moveDimension"]["source"]
                    a, b = source["startIndex"], source["endIndex"]
                    to = request["moveDimension"]["destinationIndex"]
                    for merge in self.merges:
                        tall = merge["endRowIndex"] - merge["startRowIndex"] > 1
                        if tall and merge["startRowIndex"] < b and merge["endRowIndex"] > a:
                            refuse("Invalid requests[0].moveDimension: Sorry, it is not possible "
                                   "to move a row to a position that crosses a merged cell. "
                                   "Please unmerge and try again.")
                    grid = self.rows("Inbox")
                    while len(grid) < max(b, to):
                        grid.append([])
                    moved = grid[a:b]
                    del grid[a:b]
                    to = to - (b - a) if to > a else to  # named before the rows left
                    grid[to:to] = moved
                elif "sortRange" in request:
                    sort = request["sortRange"]
                    assert "startColumnIndex" not in sort["range"], "whole rows or nothing"
                    first = sort["range"].get("startRowIndex", 0)
                    if first >= self.inbox_grid_rows():
                        refuse(f"Invalid requests[0].sortRange: range.startRowIndex is larger "
                               f"than current grid size ({self.inbox_grid_rows()})")
                    for merge in self.merges:
                        if merge["endRowIndex"] - merge["startRowIndex"] > 1 and (
                            merge["endRowIndex"] > first
                        ):
                            refuse("Invalid requests[0].sortRange: You can't sort a range "
                                   "containing vertical merges. There is a vertical merge at "
                                   + _a1_of(merge))
                    grid = self.rows("Inbox")
                    # A row that is not shown — hidden by her, or by her
                    # filter — stays where it is; the rest are sorted into
                    # the places that are left.
                    unseen = self.hidden_rows | self.filtered_rows
                    places = [i for i in range(first, len(grid)) if i not in unseen]
                    body = [grid[i] for i in places]

                    def at_col(row, col):
                        return row[col] if col < len(row) else ""

                    # Least significant key first; a stable sort keeps the
                    # rest. Descending, every text cell is above every
                    # number (a real date is a number); ascending, below.
                    # Blank cells sort last in either direction.
                    for spec in reversed(sort["sortSpecs"]):
                        col = spec["dimensionIndex"]
                        down = spec["sortOrder"] == "DESCENDING"
                        numbers = [r for r in body if isinstance(at_col(r, col), RealDate)]
                        text = [r for r in body if at_col(r, col) != ""
                                and not isinstance(at_col(r, col), RealDate)]
                        blank = [r for r in body if at_col(r, col) == ""]
                        numbers.sort(key=lambda r: at_col(r, col).serial, reverse=down)
                        text.sort(key=lambda r: at_col(r, col), reverse=down)
                        body = (text + numbers if down else numbers + text) + blank
                    for i, row in zip(places, body):
                        grid[i] = row
                elif "updateCells" in request:
                    start = request["updateCells"]["start"]
                    for dr, row in enumerate(request["updateCells"]["rows"]):
                        for dc, value in enumerate(row["values"]):
                            entered = value.get("userEnteredValue") or {}
                            assert set(entered) <= {"stringValue"}, "text only, never a formula"
                            self.put("Inbox", start["rowIndex"] + dr, start["columnIndex"] + dc,
                                     entered.get("stringValue", ""))
                elif "updateDimensionProperties" in request:
                    rng = request["updateDimensionProperties"]["range"]
                    self.hidden.update(range(rng["startIndex"], rng["endIndex"]))
                elif "setDataValidation" in request:
                    rng = request["setDataValidation"]["range"]
                    values = request["setDataValidation"]["rule"]["condition"]["values"]
                    self.dropdowns[rng["startColumnIndex"]] = [v["userEnteredValue"] for v in values]
                replies.append({})
            return {"replies": replies}

        self.calls.append((name, kw))
        if name in self.fail:
            return _Req(error=self.fail[name])
        snapshot = copy.deepcopy((self.grid, self.tabs, self.hidden, self.dropdowns, self.rules,
                                  self.bandings, self.metadata, self.styled, self.band_start,
                                  self.merges, self.hidden_rows, self.filtered_rows,
                                  self.grid_rows, self.plain_rows))
        try:
            return _Req(apply())
        except _Refused as refused:
            (self.grid, self.tabs, self.hidden, self.dropdowns, self.rules,
             self.bandings, self.metadata, self.styled, self.band_start,
             self.merges, self.hidden_rows, self.filtered_rows,
             self.grid_rows, self.plain_rows) = snapshot
            return _Req(error=_http_error(400, str(refused)))

    def _values_get(self, **kw):
        render = kw.get("valueRenderOption", "FORMATTED_VALUE")
        return self._answer("values.get", kw, lambda: self._read(kw["range"], render))

    def _values_batch_get(self, **kw):
        render = kw.get("valueRenderOption", "FORMATTED_VALUE")
        return self._answer("values.batchGet", kw, lambda: {
            "valueRanges": [self._read(a1, render) for a1 in kw["ranges"]],
        })

    def _values_batch_update(self, **kw):
        def apply():
            for item in kw["body"]["data"]:
                self._write(item["range"], item["values"])
            return {}

        return self._answer("values.batchUpdate", kw, apply)

    def _values_update(self, **kw):
        def apply():
            self._write(kw["range"], kw["body"]["values"])
            return {}

        return self._answer("values.update", kw, apply)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def legacy_sheet(n_rows: int = 83) -> FakeSheets:
    """The first live sheet as it stood on 2026-09-19: the first release's
    header, ``n_rows`` rows (a few with a deadline, some with her Status and
    Notes), Message ID hidden in H, her dropdown on I, and Upcoming's view
    drifted to row 85."""
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
    sheets.grid["Inbox"] = [list(LEGACY_HEADERS)]
    for i in range(n_rows):
        sheets.grid["Inbox"].append([
            f"2026-09-{1 + i % 18:02d} 10:00", f"sender{i}@x.com", f"Subject {i}", "other",
            f"Summary {i}.", "2026-09-23" if i % 20 == 0 else "",
            f"https://mail.google.com/mail/u/0/#inbox/m{i}", f"m{i}",
            "Done" if i % 7 == 0 else "", f"her note {i}" if i % 5 == 0 else "",
        ])
    sheets.hidden = {7}
    sheets.dropdowns = {8: list(STATUS_OPTIONS)}
    sheets.grid["Upcoming"] = [list(LEGACY_HEADERS), [DRIFTED_FORMULA]]
    return sheets


def current_sheet() -> FakeSheets:
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
    sheet_writer.setup(SID, svc=sheets)
    sheets.calls.clear()
    return sheets


def forget_the_look(sheets: FakeSheets, *, version: str | None = None) -> None:
    """Drop the FORMAT marker (or age it), and only it. The Worktree marker
    is a different key recording a different fact — wiping it would say the
    Worktree tab is hers, which is a different test."""
    kept = []
    for entry in sheets.metadata:
        if entry["metadataKey"] != sheet_style.FORMAT_MARKER_KEY:
            kept.append(entry)
        elif version is not None:
            kept.append({**entry, "metadataValue": version})
    sheets.metadata = kept


def format_markers(sheets: FakeSheets) -> list[dict]:
    return [m for m in sheets.metadata if m["metadataKey"] == sheet_style.FORMAT_MARKER_KEY]


class FakeDrive:
    """``files().get`` answering owners and permissions. ``meta`` is what
    Drive returns; ``error`` makes it refuse instead."""

    def __init__(self, meta: dict | None = None, error: Exception | None = None):
        self.meta = meta if meta is not None else {
            "owners": [{"emailAddress": CALLER}],
            "permissions": [{"type": "user", "role": "owner", "emailAddress": CALLER}],
        }
        self.error = error
        self.calls: list[dict] = []

    def files(self):
        return SimpleNamespace(get=self._get)

    def _get(self, **kw):
        self.calls.append(kw)
        return _Req(error=self.error) if self.error else _Req(self.meta)


def _check(sheets, drive=None, email=CALLER):
    return sheet_writer.check(SID, caller_email=email, svc=sheets, drive=drive or FakeDrive())


def _layout_requests(sheets) -> list[dict]:
    return next(c[1] for c in sheets.calls if c[0] == "batchUpdate:layout")["body"]["requests"]


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #

def test_check_maps_404_and_403_on_the_read_to_the_panels_words():
    sheets = FakeSheets()
    sheets.fail["get"] = _http_error(404)
    assert _check(sheets) == sheet_writer.SheetCheck("not_found", "")
    sheets.fail["get"] = _http_error(403)
    assert _check(sheets) == sheet_writer.SheetCheck("not_shared", "")


def test_check_maps_a_refused_write_to_not_editable_and_hides_the_title():
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
    sheets.fail["batchUpdate:layout"] = _http_error(403)
    assert _check(sheets) == sheet_writer.SheetCheck("not_editable", "")


def test_check_ok_also_sets_the_sheet_up():
    sheets = FakeSheets()
    assert _check(sheets) == sheet_writer.SheetCheck("ok", "Her inbox")
    assert set(sheets.tabs) == {"Inbox", "Upcoming", "Worktree"}
    assert sheets.row("Inbox", 0, len(HEADERS)) == list(HEADERS)
    assert sheets.row("Upcoming", 0, len(UPCOMING_HEADERS)) == list(UPCOMING_HEADERS)
    assert sheets.row("Worktree", 0, len(WORKTREE_HEADERS)) == list(WORKTREE_HEADERS)
    assert sheets.cell("Upcoming", 1, 0) == UPCOMING_FORMULA


def test_any_other_refusal_is_unavailable_with_the_status_not_a_raw_error():
    sheets = FakeSheets()
    sheets.fail["get"] = _http_error(400)
    with pytest.raises(SheetsUnavailable, match="HTTP 400"):
        _check(sheets)
    sheets = FakeSheets()
    sheets.fail["get"] = _http_error(503)
    with pytest.raises(SheetsUnavailable, match="after 3 attempts"):
        _check(sheets)


# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #

def test_setup_on_a_fresh_sheet_creates_tabs_headers_layout_and_the_formula():
    sheets = FakeSheets()
    sheet_writer.setup(SID, svc=sheets)
    assert sheets.names() == [
        "get", "batchUpdate:addSheet", "values.batchGet", "batchUpdate:layout",
        "values.batchUpdate", "values.update", "get", "batchUpdate:format",
    ], "the look is its own batch, last, after the headers it styles exist"
    read = next(c[1] for c in sheets.calls if c[0] == "values.batchGet")
    assert read["valueRenderOption"] == "FORMULA", "A2 must be compared as a formula"
    requests = _layout_requests(sheets)
    assert [name for r in requests for name in r] == [
        "updateSheetProperties", "updateDimensionProperties", "setDataValidation",
        "createDeveloperMetadata",
    ], "the new Worktree tab is claimed in the same batch that proves we can write"
    claim = requests[-1]["createDeveloperMetadata"]["developerMetadata"]
    assert claim["metadataKey"] == sheet_layout.WORKTREE_MARKER_KEY
    assert claim["metadataValue"] == str(sheets.tabs["Worktree"])
    assert requests[0]["updateSheetProperties"]["properties"]["gridProperties"] == {"frozenRowCount": 1}
    hidden = requests[1]["updateDimensionProperties"]["range"]
    assert (hidden["startIndex"], hidden["endIndex"]) == (COL_MESSAGE_ID - 1, COL_MESSAGE_ID) == (8, 9)  # I
    validation = requests[2]["setDataValidation"]
    assert (validation["range"]["startColumnIndex"], validation["range"]["endColumnIndex"]) == (9, 10)  # J
    assert [v["userEnteredValue"] for v in validation["rule"]["condition"]["values"]] == list(STATUS_OPTIONS)
    formula = next(c[1] for c in sheets.calls if c[0] == "values.update")
    assert formula["valueInputOption"] == "USER_ENTERED" and formula["range"] == "Upcoming!A2"
    assert sheets.row("Inbox", 0, 11) == list(HEADERS)
    assert sheets.hidden == {8} and set(sheets.dropdowns) == {9}


def test_setup_twice_changes_nothing_the_second_time():
    sheets = FakeSheets()
    sheet_writer.setup(SID, svc=sheets)
    before = {tab: [list(r) for r in rows] for tab, rows in sheets.grid.items()}
    sheets.calls.clear()
    sheet_writer.setup(SID, svc=sheets)
    # No tab creation, no header write, no formula write: the layout write is
    # the one repeated write, and it is the write that proves editability.
    assert sheets.names() == ["get", "values.batchGet", "batchUpdate:layout"]
    assert [name for r in _layout_requests(sheets) for name in r] == [
        "updateSheetProperties", "updateDimensionProperties", "setDataValidation",
    ], "no column is inserted into a current sheet"
    assert sheets.grid == before


def test_setup_repairs_a_current_sheets_edited_header_cells_and_leaves_hers_alone():
    sheets = current_sheet()
    sheets.grid["Inbox"][0] = ["Date", "Sender", "Subject", "Category", "Summary", "Action",
                               "Deadline", "Link", "Msg", "State", "My notes"]
    sheet_writer.setup(SID, svc=sheets)
    write = next(c[1] for c in sheets.calls if c[0] == "values.batchUpdate")
    assert [d["range"] for d in write["body"]["data"]] == ["Inbox!A1:I1"]
    assert write["body"]["valueInputOption"] == "RAW"
    assert sheets.row("Inbox", 0, 11) == list(AGENT_COLUMNS) + ["State", "My notes"]
    assert "insertDimension" not in str(sheets.calls)


@pytest.mark.parametrize("stored", [DRIFTED_FORMULA, HOTFIX_FORMULA, "", "=1+1"])
def test_any_upcoming_formula_but_the_current_one_is_rewritten(stored):
    sheets = current_sheet()
    sheets.put("Upcoming", 1, 0, stored)
    sheet_writer.setup(SID, svc=sheets)
    assert sheets.cell("Upcoming", 1, 0) == UPCOMING_FORMULA
    assert "values.update" in sheets.names()


def test_a_current_upcoming_formula_is_left_alone():
    sheets = current_sheet()

    # Sheets may hand a stored formula back re-spaced; that is not drift.
    sheets.calls.clear()
    sheets.put("Upcoming", 1, 0, UPCOMING_FORMULA.replace(", ", ","))
    sheet_writer.setup(SID, svc=sheets)
    assert "values.update" not in sheets.names()


def test_the_upcoming_view_references_no_numbered_inbox_row():
    """The drift fix, pinned where it matters: nothing in the view names an
    Inbox row, so inserting rows cannot move it."""
    assert not re.search(r"Inbox![A-Z]+\d", UPCOMING_FORMULA)
    assert "ROW(Inbox!A:A)>1" in UPCOMING_FORMULA


# --------------------------------------------------------------------------- #
# Migration of the first release's layout — in place, nothing lost
# --------------------------------------------------------------------------- #

def test_a_legacy_sheet_is_migrated_in_place_with_every_cell_kept():
    sheets = legacy_sheet()
    sheets.put("Upcoming", 1, 0, HOTFIX_FORMULA)  # the live sheets as they stand now
    before = [list(r) for r in sheets.grid["Inbox"]]
    assert _check(sheets).status == "ok"

    requests = _layout_requests(sheets)
    kinds = [name for r in requests for name in r]
    assert kinds[:2] == ["insertDimension", "updateCells"], "migration first, then the layout"
    insert = requests[0]["insertDimension"]["range"]
    assert (insert["dimension"], insert["startIndex"], insert["endIndex"]) == ("COLUMNS", 5, 6)
    batches = [c[0] for c in sheets.calls if c[0].startswith("batchUpdate")]
    assert batches == ["batchUpdate:addSheet", "batchUpdate:layout", "batchUpdate:format"], (
        "the migration is one atomic batch; the look follows it, separately")
    assert sheets.metadata and len(sheets.agent_rules("Inbox")) == len(sheet_style.inbox_rules(1))

    grid = sheets.grid["Inbox"]
    assert len(grid) == len(before) == 84, "no row added or lost"
    assert sheets.row("Inbox", 0, 11) == list(HEADERS)
    for r in range(1, 84):
        old = before[r] + [""] * (10 - len(before[r]))
        new = sheets.row("Inbox", r, 11)
        assert new[:5] == old[:5]          # Date .. Summary
        assert new[5] == ""                 # Action, empty until re-triaged
        assert new[6:9] == old[5:8]         # Deadline, Link, Message ID
        assert new[9:11] == old[8:10]       # her Status and Notes, intact
    assert sheets.hidden == {COL_MESSAGE_ID - 1}, "the hidden column moved with the id; nothing else hidden"
    assert set(sheets.dropdowns) == {COL_STATUS - 1}, "her dropdown moved with Status"

    width = max(len(LEGACY_HEADERS), len(UPCOMING_HEADERS))
    upcoming = sheets.row("Upcoming", 0, width)
    assert upcoming == list(UPCOMING_HEADERS) + [""] * (width - len(UPCOMING_HEADERS)),         "the old header's extra cells are blanked"
    assert sheets.cell("Upcoming", 1, 0) == UPCOMING_FORMULA


def test_after_migration_the_id_map_and_the_action_backlog_read_the_moved_columns():
    sheets = legacy_sheet()
    _check(sheets)
    ids = sheet_writer.id_rows(SID, svc=sheets)
    assert ids == {f"m{i}": i + 2 for i in range(83)}
    assert sheet_writer.blank_action_ids(SID, svc=sheets) == [f"m{i}" for i in range(83)]


def test_a_legacy_header_she_renamed_is_still_migrated_not_written_over():
    sheets = legacy_sheet(3)
    sheets.grid["Inbox"][0] = ["Date", "Sender", "Subject", "Category", "Summary",
                               "Deadline", "Link", "Msg", "State", "My notes"]
    _check(sheets)
    assert sheets.row("Inbox", 0, 11) == list(AGENT_COLUMNS) + ["State", "My notes"]
    assert sheets.row("Inbox", 1, 11)[6:9] == ["2026-09-23", "https://mail.google.com/mail/u/0/#inbox/m0", "m0"]


def test_migration_runs_once():
    sheets = legacy_sheet(5)
    _check(sheets)
    sheets.calls.clear()
    _check(sheets)
    assert "insertDimension" not in str(sheets.calls)
    assert sheets.row("Inbox", 0, 11) == list(HEADERS)


# --------------------------------------------------------------------------- #
# rows
# --------------------------------------------------------------------------- #

def test_id_rows_skips_the_header_and_blanks_and_keeps_the_first_occurrence():
    sheets = current_sheet()
    for r, mid in enumerate(["m1", "", "", "m2", "m1"], start=1):
        if mid:
            sheets.put("Inbox", r, COL_MESSAGE_ID - 1, mid)
    sheets.put("Inbox", 2, 0, "her own row, no id")
    assert sheet_writer.id_rows(SID, svc=sheets) == {"m1": 2, "m2": 5}
    _, kw = sheets.calls[-1]
    assert kw["ranges"] == ["Inbox!A1:Z1", "Inbox!I:I"]


def test_id_rows_refuses_a_sheet_not_in_the_current_layout_and_writes_nothing():
    sheets = legacy_sheet(3)
    with pytest.raises(LayoutMismatch):
        sheet_writer.id_rows(SID, svc=sheets)
    assert sheets.names() == ["values.batchGet"]
    assert isinstance(LayoutMismatch("x"), SheetsUnavailable), "the fire's handler catches it"


def test_blank_action_ids_skips_filled_actions_needs_review_and_rows_without_a_category():
    sheets = current_sheet()
    rows = [
        ("a", "Other", ""),                                     # legacy row → wanted
        ("b", "Action required", "Reply to Priya."),            # already has an action
        ("c", CATEGORY_LABELS["needs_review"], ""),             # the retry path's
        ("d", "needs_review", ""),                              # the old marker, also the retry path's
        ("e", "", ""),                                          # no category: not an agent row
        ("f", "role_to_fill", ""),                              # legacy category → wanted
    ]
    for r, (mid, category, action) in enumerate(rows, start=1):
        sheets.put("Inbox", r, COL_MESSAGE_ID - 1, mid)
        sheets.put("Inbox", r, 3, category)
        sheets.put("Inbox", r, COL_ACTION - 1, action)
    assert sheet_writer.blank_action_ids(SID, svc=sheets) == ["a", "f"]


def test_the_writer_refuses_to_build_offline_and_reports_no_identity():
    with pytest.raises(InboxOffline):
        sheet_writer.service()
    assert sheet_writer.service_account_email() == ""


# --------------------------------------------------------------------------- #
# Pinned 2026-09-18 (tester pass)
# --------------------------------------------------------------------------- #

def test_check_maps_a_refused_tab_creation_to_not_editable():
    sheets = FakeSheets()  # no tabs yet, so the first write is the addSheet
    sheets.fail["batchUpdate:addSheet"] = _http_error(403)
    assert _check(sheets) == sheet_writer.SheetCheck("not_editable", "")
    assert "batchUpdate:layout" not in sheets.names()


def test_a_non_403_refusal_during_set_up_is_unavailable_with_the_status():
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
    sheets.fail["values.batchGet"] = _http_error(404)
    with pytest.raises(SheetsUnavailable, match="HTTP 404"):
        _check(sheets)


def test_check_on_an_already_set_up_sheet_writes_nothing_but_the_layout_and_the_header_repair():
    sheets = current_sheet()
    assert _check(sheets).status == "ok"
    assert sheets.names() == ["get", "values.batchGet", "batchUpdate:layout"]

    sheets.calls.clear()
    sheets.grid["Inbox"][0] = ["Date", "From", "Subject", "Category", "Summary", "Action",
                               "Deadline", "Link", "Msg", "State", "My notes"]
    assert _check(sheets).status == "ok"
    assert sheets.names() == ["get", "values.batchGet", "batchUpdate:layout", "values.batchUpdate"]
    write = sheets.calls[-1][1]["body"]["data"]
    assert [d["range"] for d in write] == ["Inbox!A1:I1"], "her J and K headers are never written"
    assert sheets.row("Inbox", 0, 11)[9:] == ["State", "My notes"]


def test_a_refused_row_call_is_sheets_unavailable_not_a_raw_http_error():
    """The fire catches ``SheetsUnavailable`` and records it on ``last_poll``;
    it does not catch ``HttpError``. She un-shares the sheet, or renames the
    Inbox tab, between two hourly checks: the next fire's id read is refused,
    and that refusal has to reach the panel as a sentence -- not escape the
    fire as a raw client error that leaves ``last_poll`` saying all is well."""
    escaped = []
    for call, run in (
        ("values.batchGet", lambda s: sheet_writer.id_rows(SID, svc=s)),
        ("values.batchGet", lambda s: sheet_writer.blank_action_ids(SID, svc=s)),
        ("get", lambda s: sheet_writer.write_rows(SID, [["x"] * 9], svc=s)),
        ("values.batchGet", lambda s: sheet_writer.write_rows(SID, [["x"] * 9], svc=s)),
        ("batchUpdate:rows", lambda s: sheet_writer.write_rows(SID, [["x"] * 9], svc=s)),
        ("batchUpdate:order", lambda s: sheet_writer.setup(SID, svc=s, reorder=True)),
    ):
        sheets = current_sheet()
        sheets.fail[call] = _http_error(403)
        try:
            run(sheets)
        except SheetsUnavailable:
            continue
        except HttpError:
            escaped.append(call)
    assert escaped == [], f"a 403 escaped as a raw HttpError from: {escaped}"


# --------------------------------------------------------------------------- #
# Pinned 2026-09-18 (review fixes): the sheet must be the caller's, proved by
# Drive before the first write; refusals never carry the sheet's id.
# --------------------------------------------------------------------------- #

def test_a_sheet_drive_does_not_show_as_the_callers_is_not_yours_with_zero_writes_and_no_title():
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
    drive = FakeDrive({
        "owners": [{"emailAddress": "someone.else@legalsoft.com"}],
        "permissions": [
            {"type": "user", "role": "owner", "emailAddress": "someone.else@legalsoft.com"},
            {"type": "user", "role": "reader", "emailAddress": CALLER},
            {"type": "anyone", "role": "writer"},
            {"type": "domain", "role": "writer", "domain": "legalsoft.com"},
        ],
    })
    assert _check(sheets, drive) == sheet_writer.SheetCheck("not_yours", "")
    assert sheets.names() == ["get"], "the metadata read only — nothing written"
    assert drive.calls == [{
        "fileId": SID,
        "fields": "owners(emailAddress),permissions(emailAddress,role,type)",
        "supportsAllDrives": True,
    }]


def test_a_legacy_sheet_that_is_not_the_callers_is_not_migrated():
    sheets = legacy_sheet(3)
    before = [list(r) for r in sheets.grid["Inbox"]]
    drive = FakeDrive({"owners": [{"emailAddress": "someone.else@legalsoft.com"}], "permissions": []})
    assert _check(sheets, drive).status == "not_yours"
    assert sheets.grid["Inbox"] == before and sheets.names() == ["get"]


@pytest.mark.parametrize("meta", [
    {"owners": [{"emailAddress": "Her@LegalSoft.com"}], "permissions": []},
    {"owners": [], "permissions": [{"type": "user", "role": "writer", "emailAddress": "HER@legalsoft.com"}]},
    {"owners": [], "permissions": [{"type": "user", "role": "owner", "emailAddress": CALLER}]},
])
def test_an_owner_or_user_editor_matched_case_insensitively_is_ok(meta):
    sheets = FakeSheets()
    assert _check(sheets, FakeDrive(meta)) == sheet_writer.SheetCheck("ok", "Her inbox")


@pytest.mark.parametrize("meta", [
    {"owners": [{"emailAddress": CALLER}]},                   # no permission list at all
    {"owners": [{"emailAddress": CALLER}], "permissions": None},
    {},
])
def test_an_unreadable_permission_list_fails_closed(meta):
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
    assert _check(sheets, FakeDrive(meta)).status == "not_yours"
    assert sheets.names() == ["get"]


def test_no_caller_address_is_not_yours_without_asking_drive():
    sheets = FakeSheets()
    drive = FakeDrive()
    assert _check(sheets, drive, email="").status == "not_yours"
    assert drive.calls == [] and sheets.names() == ["get"]


def test_drive_404_is_not_yours_and_drive_403_is_a_loud_failure_both_with_zero_writes():
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
    assert _check(sheets, FakeDrive(error=_http_error(404))).status == "not_yours"
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
    with pytest.raises(SheetsUnavailable, match="Drive permission lookup was refused: HTTP 403"):
        _check(sheets, FakeDrive(error=_http_error(403)))
    assert sheets.names() == ["get"]


def test_not_found_and_not_shared_do_not_reach_drive():
    for status, word in ((404, "not_found"), (403, "not_shared")):
        sheets = FakeSheets()
        sheets.fail["get"] = _http_error(status)
        drive = FakeDrive()
        assert _check(sheets, drive).status == word and drive.calls == []


def test_refusals_and_exhausted_retries_never_carry_the_spreadsheet_id():
    uri = f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values:batchGet?ranges=Inbox%21I%3AI"
    for status in (403, 503):
        sheets = FakeSheets()
        sheets.fail["values.batchGet"] = HttpError(SimpleNamespace(status=status, reason="r"), b"body", uri=uri)
        with pytest.raises(SheetsUnavailable) as caught:
            sheet_writer.id_rows(SID, svc=sheets)
        text = str(caught.value)
        assert SID not in text and "googleapis" not in text, text
        assert f"HTTP {status}" in text


def test_the_drive_client_refuses_to_build_offline():
    with pytest.raises(InboxOffline):
        sheet_writer.drive_service()


# --------------------------------------------------------------------------- #
# The look (sheet_style): applied once, never duplicated, never hers
# --------------------------------------------------------------------------- #

def _format_batch(sheets) -> list[dict]:
    return next(c[1] for c in sheets.calls if c[0] == "batchUpdate:format")["body"]["requests"]


def _her_rule(sheet_id: int = 1) -> dict:
    return {"ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 10,
                        "endColumnIndex": 11}],
            "booleanRule": {"condition": {"type": "TEXT_CONTAINS",
                                          "values": [{"userEnteredValue": "urgent"}]},
                            "format": {"textFormat": {"italic": True}}}}


def _luminance(hex_colour: str) -> float:
    def channel(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    c = sheet_style.rgb(hex_colour)
    return 0.2126 * channel(c["red"]) + 0.7152 * channel(c["green"]) + 0.0722 * channel(c["blue"])


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_a_new_sheet_is_formatted_once_with_the_marker_last_in_the_same_batch():
    sheets = FakeSheets()
    result = _check(sheets)
    assert result == sheet_writer.SheetCheck("ok", "Her inbox")
    assert result.formatting == sheet_writer.FORMAT_APPLIED
    requests = _format_batch(sheets)
    assert "createDeveloperMetadata" in requests[-1], "the marker exists exactly when the look does"
    marker = requests[-1]["createDeveloperMetadata"]["developerMetadata"]
    assert (marker["metadataKey"], marker["metadataValue"]) == (
        sheet_style.FORMAT_MARKER_KEY, sheet_style.FORMAT_VERSION)
    assert marker["location"] == {"spreadsheet": True}
    inbox, upcoming = sheets.tabs["Inbox"], sheets.tabs["Upcoming"]
    assert len(sheets.agent_rules("Inbox")) == len(sheet_style.inbox_rules(inbox))
    assert len(sheets.agent_rules("Upcoming")) == len(sheet_style.upcoming_rules(upcoming))
    assert sheets.bandings == {inbox: [sheet_style.INBOX_BANDING_ID]}
    assert sheets.hidden == {COL_MESSAGE_ID - 1}, "widths never un-hide the Message ID column"


def test_every_hourly_recheck_after_that_sends_no_formatting_at_all():
    sheets = current_sheet()
    rules_before = copy.deepcopy(sheets.rules)
    for _ in range(3):
        assert _check(sheets).formatting == sheet_writer.FORMAT_ALREADY
    assert sheets.names() == ["get", "values.batchGet", "batchUpdate:layout"] * 3
    assert sheets.rules == rules_before and len(format_markers(sheets)) == 1


def test_her_later_formatting_survives_the_hourly_recheck():
    sheets = current_sheet()
    inbox = sheets.tabs["Inbox"]
    sheets.rules[inbox] = [_her_rule(inbox)]  # she replaced the colours with her own rule
    sheets.bandings = {}
    _check(sheets)
    assert sheets.rules[inbox] == [_her_rule(inbox)] and sheets.bandings == {}


def test_a_sheet_set_up_before_the_look_existed_is_formatted_once_keeping_her_rules_on_top():
    """The two live sheets: current layout, no marker, her own rule."""
    sheets = current_sheet()
    inbox = sheets.tabs["Inbox"]
    forget_the_look(sheets)
    sheets.rules, sheets.bandings = {inbox: [_her_rule(inbox)]}, {}
    sheets.calls.clear()
    assert _check(sheets).formatting == sheet_writer.FORMAT_APPLIED
    assert sheets.names()[-2:] == ["get", "batchUpdate:format"]
    rules = sheets.rules[inbox]
    assert rules[0] == _her_rule(inbox), "hers keeps precedence"
    assert len(rules) == 1 + len(sheet_style.inbox_rules(inbox))
    sheets.calls.clear()
    assert _check(sheets).formatting == sheet_writer.FORMAT_ALREADY
    assert "batchUpdate:format" not in sheets.names()


@pytest.mark.parametrize("marker", ["missing", "older version"])
def test_re_applying_replaces_only_the_agents_own_rules_banding_and_marker(marker):
    sheets = current_sheet()
    inbox, upcoming = sheets.tabs["Inbox"], sheets.tabs["Upcoming"]
    sheets.rules[inbox].insert(3, _her_rule(inbox))  # hers, between the agent's
    sheets.rules[upcoming].append(_her_rule(upcoming))
    forget_the_look(sheets, version=None if marker == "missing" else "0")
    assert sheet_writer.setup(SID, svc=sheets).formatting == sheet_writer.FORMAT_APPLIED
    assert len(sheets.agent_rules("Inbox")) == len(sheet_style.inbox_rules(inbox)), "not duplicated"
    assert len(sheets.agent_rules("Upcoming")) == len(sheet_style.upcoming_rules(upcoming))
    assert sheets.rules[inbox][0] == _her_rule(inbox) and sheets.rules[upcoming][0] == _her_rule(upcoming)
    assert sum(r == _her_rule(inbox) for r in sheets.rules[inbox]) == 1
    assert sheets.bandings[inbox] == [sheet_style.INBOX_BANDING_ID]
    assert [(m["metadataKey"], m["metadataValue"]) for m in format_markers(sheets)] == [
        (sheet_style.FORMAT_MARKER_KEY, sheet_style.FORMAT_VERSION)]


def test_her_own_banding_on_inbox_is_kept_and_the_agent_adds_none():
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
    sheets.bandings = {1: [555]}
    assert _check(sheets).formatting == sheet_writer.FORMAT_APPLIED
    assert sheets.bandings == {1: [555]}
    assert not any("addBanding" in r for r in _format_batch(sheets))


def test_a_refused_formatting_pass_is_recorded_logged_without_the_id_and_rows_still_flow(caplog):
    sheets = FakeSheets()
    sheets.fail["batchUpdate:format"] = HttpError(
        SimpleNamespace(status=400, reason="bad"), b"body",
        uri=f"https://sheets.googleapis.com/v4/spreadsheets/{SID}:batchUpdate")
    with caplog.at_level(logging.WARNING, logger="agentos.inbox.sheets"):
        result = _check(sheets)
    assert result.status == "ok" and result.formatting == sheet_writer.FORMAT_FAILED
    text = caplog.text
    assert "formatting was not applied" in text and "HTTP 400" in text
    assert SID not in text and "googleapis" not in text
    assert format_markers(sheets) == [] and sheets.rules == {}, "no marker: the next check retries"
    assert sheet_writer.id_rows(SID, svc=sheets) == {}
    assert sheet_writer.write_rows(
        SID, [["2026-09-17 10:05", "f", "s", "c", "sum", "act", "", "l", "m1"]], svc=sheets
    ).new_rows == [2]

    del sheets.fail["batchUpdate:format"]
    assert _check(sheets).formatting == sheet_writer.FORMAT_APPLIED


def test_a_failed_format_read_is_also_not_fatal():
    sheets = current_sheet()
    forget_the_look(sheets)
    real_get = sheets._get
    calls = {"n": 0}

    def get(**kw):
        calls["n"] += 1
        return _Req(error=_http_error(500)) if calls["n"] == 2 else real_get(**kw)

    sheets._get = get
    assert _check(sheets).formatting == sheet_writer.FORMAT_FAILED
    assert "batchUpdate:format" not in sheets.names()


def test_an_atomic_refusal_mid_batch_leaves_no_half_look():
    sheets = current_sheet()
    inbox = sheets.tabs["Inbox"]
    forget_the_look(sheets)
    sheets.bandings = {inbox: [999]}  # hers — but the format read below does not show it
    real_get = sheets._get

    def stale_get(**kw):
        meta = real_get(**kw).execute()
        for sheet in meta["sheets"]:
            sheet["bandedRanges"] = []
        return _Req(meta)

    sheets._get = stale_get
    rules_before = copy.deepcopy(sheets.rules)
    assert sheet_writer.setup(SID, svc=sheets).formatting == sheet_writer.FORMAT_FAILED
    assert sheets.rules == rules_before and format_markers(sheets) == []


def test_a_migrated_sheet_is_formatted_in_the_batch_after_the_migration():
    sheets = legacy_sheet(4)
    assert _check(sheets).formatting == sheet_writer.FORMAT_APPLIED
    names = sheets.names()
    assert names.index("batchUpdate:layout") < names.index("batchUpdate:format")
    assert sheets.hidden == {COL_MESSAGE_ID - 1} and set(sheets.dropdowns) == {COL_STATUS - 1}


def test_the_rules_are_tagged_derived_from_column_names_and_cover_every_future_row():
    from inbox_triage_agent.sheet_layout import COL_CATEGORY, COL_DEADLINE, DUE_OVERDUE, column_letter

    rules = sheet_style.inbox_rules(7)
    for rng, condition, _fmt in rules:
        assert rng["sheetId"] == 7 and rng["startRowIndex"] == 1 and "endRowIndex" not in rng
        assert sheet_style.is_agent_rule({"booleanRule": {"condition": {
            "values": [{"userEnteredValue": sheet_style.tagged(condition)}]}}})
    done_range, done_cond, done_fmt = rules[0]
    assert (done_range["startColumnIndex"], done_range["endColumnIndex"]) == (0, len(HEADERS)), \
        "Done colours the whole row"
    assert done_cond == f'${column_letter(COL_STATUS)}2="Done"'
    assert done_fmt["textFormat"]["strikethrough"] is True
    deadline_rules = [r for r in rules if r[0].get("startColumnIndex") == COL_DEADLINE - 1]
    assert len(deadline_rules) == 2 and all('<>"Done"' in c for _, c, _ in deadline_rules)
    assert any("TODAY()+2" in c for _, c, _ in deadline_rules)
    category_rules = [r for r in rules if r[0].get("startColumnIndex") == COL_CATEGORY - 1]
    styled = {c.split('"')[1] for _, c, _ in category_rules}
    assert styled == set(CATEGORY_LABELS.values()) - {CATEGORY_LABELS["other"]}
    upcoming = sheet_style.upcoming_rules(8)
    assert upcoming[0][1] == f'$A2="{DUE_OVERDUE}"'
    assert all(r[0]["startColumnIndex"] == UPCOMING_HEADERS.index("Due") for r in upcoming)
    assert "$B2<=" in upcoming[1][1] and UPCOMING_HEADERS[1] == "Deadline"
    # Her own custom formula, without the tag, is never taken for the agent's.
    assert not sheet_style.is_agent_rule({"booleanRule": {"condition": {
        "type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": '=$D2="Meeting"'}]}}})
    assert not sheet_style.is_agent_rule(_her_rule())


def test_every_coloured_pair_is_readable():
    pairs = [(sheet_style.BRAND_BLUE, sheet_style.WHITE),
             (sheet_style.IN_PROGRESS_FILL, sheet_style.IN_PROGRESS_TEXT),
             (sheet_style.WHITE, sheet_style.DEADLINE_SOON_TEXT),
             (sheet_style.BAND_TINT, sheet_style.DEADLINE_SOON_TEXT),
             (sheet_style.BAND_TINT, sheet_style.DEADLINE_OVERDUE_TEXT),
             (sheet_style.DUE_OVERDUE_FILL, sheet_style.DUE_OVERDUE_TEXT),
             (sheet_style.DUE_SOON_FILL, sheet_style.DUE_SOON_TEXT),
             (sheet_style.DUE_LATER_FILL, sheet_style.DUE_LATER_TEXT)]
    pairs += [(fill, text) for fill, text in sheet_style.CATEGORY_STYLE.values() if fill]
    weak = [(f, t, round(_contrast(f, t), 2)) for f, t in pairs if _contrast(f, t) < 4.5]
    assert weak == []
    # The deliberately faded ones (newsletter text, a Done row) still clear 3:1.
    faded = sheet_style.CATEGORY_STYLE[CATEGORY_LABELS["newsletter_promo"]][1]
    assert _contrast(sheet_style.WHITE, faded) >= 3 and _contrast(sheet_style.BAND_TINT, faded) >= 3
    assert _contrast(sheet_style.DONE_FILL, sheet_style.DONE_TEXT) >= 3


def test_widths_are_by_column_name_and_the_header_is_brand_blue_on_both_tabs():
    requests = sheet_style.format_requests({}, inbox_sheet_id=1, upcoming_sheet_id=2)
    widths = {
        (r["updateDimensionProperties"]["range"]["sheetId"],
         r["updateDimensionProperties"]["range"]["startIndex"]):
            r["updateDimensionProperties"]["properties"]["pixelSize"]
        for r in requests
        if "updateDimensionProperties" in r
        and r["updateDimensionProperties"]["range"]["dimension"] == "COLUMNS"
    }
    assert widths[(1, HEADERS.index("Summary"))] == 380
    assert widths[(1, HEADERS.index("Notes"))] == 220
    assert (1, COL_MESSAGE_ID - 1) not in widths
    assert widths[(2, UPCOMING_HEADERS.index("Due"))] == 100
    headers = [r["repeatCell"] for r in requests
               if "repeatCell" in r and r["repeatCell"]["range"].get("endRowIndex") == 1]
    assert {h["range"]["sheetId"]: h["range"]["endColumnIndex"] for h in headers} == {
        1: len(HEADERS), 2: len(UPCOMING_HEADERS)}
    for h in headers:
        fmt = h["cell"]["userEnteredFormat"]
        assert fmt["backgroundColor"] == sheet_style.rgb("#1746A2")
        assert fmt["textFormat"] == {"foregroundColor": sheet_style.rgb("#FFFFFF"), "bold": True}
        assert fmt["verticalAlignment"] == "MIDDLE"
    wrapped = {r["repeatCell"]["range"]["startColumnIndex"] for r in requests
               if "repeatCell" in r and r["repeatCell"]["range"]["sheetId"] == 1
               and r["repeatCell"]["cell"]["userEnteredFormat"].get("wrapStrategy") == "WRAP"}
    assert wrapped == {HEADERS.index("Summary"), COL_ACTION - 1}


# --------------------------------------------------------------------------- #
# Worktree — the third tab: whose it is, what is read, what is written
# --------------------------------------------------------------------------- #

def _worktree_markers(sheets: FakeSheets) -> list[dict]:
    return [m for m in sheets.metadata if m["metadataKey"] == sheet_layout.WORKTREE_MARKER_KEY]


def test_a_new_worktree_tab_is_created_claimed_by_its_marker_and_headed():
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
    done = sheet_writer.setup(SID, svc=sheets)
    assert done.worktree == sheet_writer.WORKTREE_OURS
    assert done.worktree_sheet_id == sheets.tabs["Worktree"]
    assert sheets.row("Worktree", 0, len(WORKTREE_HEADERS)) == list(WORKTREE_HEADERS)
    assert [(m["metadataKey"], m["metadataValue"]) for m in _worktree_markers(sheets)] == [
        (sheet_layout.WORKTREE_MARKER_KEY, str(sheets.tabs["Worktree"]))]


def test_an_existing_sheet_gains_the_tab_without_its_inbox_or_upcoming_moving():
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
    sheet_writer.setup(SID, svc=sheets)
    # Wind it back to the sheet as it stands today: two tabs, no Worktree.
    del sheets.tabs["Worktree"], sheets.grid["Worktree"]
    sheets.metadata = [
        m for m in sheets.metadata if m["metadataKey"] != sheet_layout.WORKTREE_MARKER_KEY
    ]
    inbox_before = [list(r) for r in sheets.grid["Inbox"]]
    upcoming_before = [list(r) for r in sheets.grid["Upcoming"]]

    assert sheet_writer.setup(SID, svc=sheets).worktree == sheet_writer.WORKTREE_OURS

    assert sheets.grid["Inbox"] == inbox_before and sheets.grid["Upcoming"] == upcoming_before
    assert sheets.row("Worktree", 0, len(WORKTREE_HEADERS)) == list(WORKTREE_HEADERS)


def test_a_worktree_tab_the_agent_did_not_create_is_hers_and_is_left_exactly_as_it_is():
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2, "Worktree": 9})
    sheets.grid["Worktree"] = [["Her plan"], ["call the landlord"]]
    before = [list(r) for r in sheets.grid["Worktree"]]

    done = sheet_writer.setup(SID, svc=sheets)

    assert done.worktree == sheet_writer.WORKTREE_CLAIMED and done.worktree_sheet_id is None
    assert sheets.grid["Worktree"] == before, "not a cell of hers is written"
    assert _worktree_markers(sheets) == [], "the agent never claims a tab it did not make"
    assert 9 not in sheets.rules, "and never styles one"


def test_a_tab_the_agent_made_is_recognised_again_by_the_id_in_its_marker():
    sheets = current_sheet()
    assert sheet_writer.setup(SID, svc=sheets).worktree == sheet_writer.WORKTREE_OURS
    # She renamed it and made her own in its place: the id no longer matches.
    sheets.tabs["Worktree notes"] = sheets.tabs.pop("Worktree")
    sheets.grid["Worktree notes"] = sheets.grid.pop("Worktree")
    sheets.tabs["Worktree"] = 77
    sheets.grid["Worktree"] = [["hers"]]
    assert sheet_writer.setup(SID, svc=sheets).worktree == sheet_writer.WORKTREE_CLAIMED
    assert sheets.grid["Worktree"] == [["hers"]]


def test_a_renamed_agent_tab_is_made_again_and_the_stale_marker_is_replaced():
    sheets = current_sheet()
    sheets.tabs["Old worktree"] = sheets.tabs.pop("Worktree")
    sheets.grid["Old worktree"] = sheets.grid.pop("Worktree")

    done = sheet_writer.setup(SID, svc=sheets)

    assert done.worktree == sheet_writer.WORKTREE_OURS
    assert [m["metadataValue"] for m in _worktree_markers(sheets)] == [str(sheets.tabs["Worktree"])]
    assert len(_worktree_markers(sheets)) == 1, "exactly one claim, on the tab that exists"


def test_read_inbox_returns_her_columns_too_and_skips_the_header():
    sheets = current_sheet()
    sheets.put("Inbox", 1, 0, "2026-09-17 10:05")
    sheets.put("Inbox", 1, COL_MESSAGE_ID - 1, "m1")
    sheets.put("Inbox", 1, COL_STATUS - 1, "In progress")
    sheets.put("Inbox", 1, COL_STATUS, "her note")

    rows = sheet_writer.read_inbox(SID, svc=sheets)

    assert len(rows) == 1
    assert rows[0][COL_MESSAGE_ID - 1] == "m1"
    assert rows[0][COL_STATUS - 1:COL_STATUS + 1] == ["In progress", "her note"], \
        "the Worktree needs her Status; it only ever reads it"
    read = next(c[1] for c in sheets.calls if c[0] == "values.batchGet")
    assert read["ranges"][1] == "Inbox!A:K"


def test_read_inbox_refuses_a_layout_that_is_not_the_current_one():
    sheets = current_sheet()
    sheets.grid["Inbox"][0] = list(LEGACY_HEADERS)
    with pytest.raises(LayoutMismatch):
        sheet_writer.read_inbox(SID, svc=sheets)


def test_write_worktree_replaces_the_body_and_blanks_what_a_longer_build_left():
    sheets = current_sheet()
    three = [[f"P{i}", "Type", "Waiting on us", "do it", "1 day", "", "1", "link"] for i in range(3)]
    assert sheet_writer.write_worktree(SID, three, previous_rows=0, svc=sheets) == 3
    assert [sheets.cell("Worktree", r, 0) for r in (1, 2, 3)] == ["P0", "P1", "P2"]

    assert sheet_writer.write_worktree(SID, three[:1], previous_rows=3, svc=sheets) == 1
    assert [sheets.cell("Worktree", r, 0) for r in (1, 2, 3)] == ["P0", "", ""], \
        "a shorter build leaves no stale process behind it"
    assert sheets.row("Worktree", 0, len(WORKTREE_HEADERS)) == list(WORKTREE_HEADERS), \
        "the header row is never in the rectangle"


def test_write_worktree_writes_raw_inside_its_own_rectangle_and_nothing_else():
    sheets = current_sheet()
    sheet_writer.write_worktree(SID, [["a"] * len(WORKTREE_HEADERS)] * 2, svc=sheets)
    call = next(c[1] for c in sheets.calls if c[0] == "values.update")
    assert call["range"] == "Worktree!A2:H3" and call["valueInputOption"] == "RAW"
    assert len(sheets.calls) == 1, "one call, not a clear and an update"


def test_write_worktree_with_nothing_to_say_and_nothing_to_clear_does_not_call_sheets():
    sheets = current_sheet()
    assert sheet_writer.write_worktree(SID, [], previous_rows=0, svc=sheets) == 0
    assert sheets.calls == []


def test_the_worktree_rules_colour_a_chase_row_whole_and_the_status_cell_otherwise():
    from inbox_triage_agent.worktree import (
        STATUS_CHASING, STATUS_OVERDUE_REPLY, STATUS_WAITING_ON_THEM, STATUS_WAITING_ON_US,
    )

    rules = sheet_style.worktree_rules(9)
    for rng, condition, _fmt in rules:
        assert rng["sheetId"] == 9 and rng["startRowIndex"] == 1 and "endRowIndex" not in rng
        assert sheet_style.is_agent_rule({"booleanRule": {"condition": {
            "values": [{"userEnteredValue": sheet_style.tagged(condition)}]}}})
    chase_range, chase_condition, chase_format = rules[0]
    assert (chase_range["startColumnIndex"], chase_range["endColumnIndex"]) == (
        0, len(WORKTREE_HEADERS)), "a chase is the one whole-row alarm on this tab"
    assert chase_condition == (
        f'OR($C2="{STATUS_CHASING}", $C2="{STATUS_OVERDUE_REPLY}")'
    ), "both alarms wear the same red, whichever side owes the reply"
    assert chase_format["backgroundColor"] == sheet_style.rgb(sheet_style.CHASE_FILL)
    assert [c for _, c, _ in rules[1:3]] == [
        f'$C2="{STATUS_WAITING_ON_US}"', f'$C2="{STATUS_WAITING_ON_THEM}"']
    due_rules = [r for r in rules if r[0].get("startColumnIndex") == WORKTREE_HEADERS.index("Due")]
    assert len(due_rules) == 2 and any("TODAY()+2" in c for _, c, _ in due_rules)


def test_the_look_covers_the_worktree_tab_only_when_it_is_the_agents():
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
    _check(sheets)
    worktree_id = sheets.tabs["Worktree"]
    assert len(sheets.agent_rules("Worktree")) == len(sheet_style.worktree_rules(worktree_id))
    widths = [
        r["updateDimensionProperties"] for r in sheets.styled
        if "updateDimensionProperties" in r
        and r["updateDimensionProperties"]["range"]["sheetId"] == worktree_id
        and r["updateDimensionProperties"]["range"]["dimension"] == "COLUMNS"
    ]
    assert len(widths) == len(WORKTREE_HEADERS), "every Worktree column gets a width"

    hers = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2, "Worktree": 9})
    _check(hers)
    assert 9 not in hers.rules and not any(
        r.get("updateDimensionProperties", {}).get("range", {}).get("sheetId") == 9
        for r in hers.styled
    ), "hers is not styled at all"


# --------------------------------------------------------------------------- #
# Newest first — where a new row goes (2026-09-29)
#
# Both live sheets held their rows in two runs: the backfill newest-first from
# row 2, and every message since appended oldest-first underneath, so today's
# mail sat thousands of rows down. New rows are now INSERTED where their date
# puts them, and what the fake does to a range that begins on row 2 is what
# the throwaway sheet did when it was tried.
# --------------------------------------------------------------------------- #

def _row(mid: str, when: str, *, status: str = "", notes: str = "", mine: str | None = None) -> list[str]:
    """A whole Inbox row: the agent's nine cells, her Status and Notes, and —
    when ``mine`` is given — a cell in column M, which is no column of the
    layout's at all."""
    cells = [when, f"{mid}@x.com", f"Subject {mid}", "Other", f"Summary {mid}.", "Do it.", "",
             f"https://mail.google.com/mail/u/0/#inbox/{mid}", mid, status, notes]
    return cells + (["", mine] if mine is not None else [])


def _agent(mid: str, when: str) -> list[str]:
    return _row(mid, when)[: len(AGENT_COLUMNS)]


def _fill(sheets: FakeSheets, rows: list[list[str]]) -> FakeSheets:
    for r, cells in enumerate(rows, start=1):
        for c, value in enumerate(cells):
            sheets.put("Inbox", r, c, value)
    return sheets


def _body(sheets: FakeSheets) -> list[list[str]]:
    """Every row under the header that holds anything, as whole rows."""
    rows = [sheets.row("Inbox", r, 13) for r in range(1, len(sheets.rows("Inbox")))]
    return [row for row in rows if any(row)]


def _ids(sheets: FakeSheets) -> list[str]:
    return [row[COL_MESSAGE_ID - 1] for row in _body(sheets)]


def newest_first_sheet() -> FakeSheets:
    return _fill(current_sheet(), [
        _row("c", "2026-09-20 09:00", status="In progress", notes="note c", mine="mine c"),
        _row("b", "2026-09-18 09:00", notes="note b"),
        _row("a", "2026-09-16 09:00", status="Done", mine="mine a"),
    ])


def split_sheet() -> FakeSheets:
    """A live sheet as it was found: eight backfill rows newest-first, then
    five appended since, oldest first — her Status, her Notes and a column of
    her own on some of each."""
    backfill = [
        _row(f"b{i}", f"2026-09-{18 - i:02d} 09:00", status="Done" if i % 3 == 0 else "",
             notes=f"note b{i}" if i % 2 else "", mine=f"mine b{i}" if i % 4 == 0 else None)
        for i in range(8)
    ]
    since = [
        _row(f"n{i}", f"2026-09-{19 + i:02d} 08:00", status="In progress" if i == 1 else "",
             notes=f"note n{i}", mine=f"mine n{i}")
        for i in range(5)
    ]
    return _fill(current_sheet(), backfill + since)


def _row_batches(sheets: FakeSheets) -> list[list[dict]]:
    """What each row write did to the rows. Its stamp — the metadata requests
    at the head of the batch — has tests of its own."""
    return [
        [r for r in kw["body"]["requests"]
         if "createDeveloperMetadata" not in r and "deleteDeveloperMetadata" not in r]
        for name, kw in sheets.calls if name == "batchUpdate:rows"
    ]


def test_a_new_message_lands_in_the_first_data_row_and_every_older_row_moves_down_whole():
    sheets = newest_first_sheet()
    before = _body(sheets)

    written = sheet_writer.write_rows(SID, [_agent("d", "2026-09-29 10:15")], svc=sheets)

    assert written.new_rows == [2] and written.updated == {} and written.missing == []
    assert sheets.names() == ["get", "values.batchGet", "batchUpdate:rows"], \
        "where things are is read once, and everything is written once"
    read = next(kw for name, kw in sheets.calls if name == "values.batchGet")
    assert read["ranges"] == ["Inbox!A1:Z1", "Inbox!A:A", "Inbox!I:I"]
    assert read["valueRenderOption"] == "UNFORMATTED_VALUE", "as stored, not as she has it shown"
    body = _body(sheets)
    assert body[0] == _agent("d", "2026-09-29 10:15") + [""] * 4, \
        "the agent's nine cells, and nothing in any column of hers"
    assert body[1:] == before, "every older row is one row lower, with every cell it had"
    assert sheets.row("Inbox", 0, 11) == list(HEADERS), "the header row never moves"
    assert sheet_writer.id_rows(SID, svc=sheets) == {"d": 2, "c": 3, "b": 4, "a": 5}


def test_two_messages_in_one_write_keep_newest_first_order():
    sheets = newest_first_sheet()
    older, newer = _agent("d", "2026-09-29 10:15"), _agent("e", "2026-09-29 10:40")

    written = sheet_writer.write_rows(SID, [older, newer], svc=sheets)

    assert _ids(sheets) == ["e", "d", "c", "b", "a"]
    assert written.new_rows == [3, 2], "each row's number, in the order the rows were handed over"
    assert len(_row_batches(sheets)) == 1


def test_an_older_message_goes_where_its_date_puts_it_and_the_oldest_to_the_bottom():
    """The backfill walks back in time, and a reconnect finds mail that came
    in while the inbox was disconnected: neither belongs on top."""
    sheets = newest_first_sheet()
    rows = [
        _agent("old", "2026-06-30 12:00"),      # older than everything: the bottom
        _agent("gap", "2026-09-19 12:00"),      # between c and b
        _agent("new", "2026-09-29 12:00"),      # newer than everything: the top
        _agent("gap2", "2026-09-19 18:00"),     # the same gap, and newer than "gap"
    ]

    written = sheet_writer.write_rows(SID, rows, svc=sheets)

    assert _ids(sheets) == ["new", "c", "gap2", "gap", "b", "a", "old"]
    assert written.new_rows == [8, 5, 2, 4]
    assert [row[9:] for row in _body(sheets) if row[8] in ("c", "b", "a")] == [
        ["In progress", "note c", "", "mine c"], ["", "note b", "", ""], ["Done", "", "", "mine a"],
    ], "her cells went with their rows"
    dates = [row[0] for row in _body(sheets)]
    assert dates == sorted(dates, reverse=True)


def test_a_message_from_the_same_minute_as_the_top_row_still_goes_above_it():
    sheets = newest_first_sheet()
    sheet_writer.write_rows(SID, [_agent("d", "2026-09-20 09:00")], svc=sheets)
    assert _ids(sheets)[:2] == ["d", "c"]


def test_the_first_row_of_an_empty_sheet_is_row_two():
    sheets = current_sheet()
    written = sheet_writer.write_rows(
        SID, [_agent("a", "2026-09-16 09:00"), _agent("b", "2026-09-18 09:00")], svc=sheets)
    assert _ids(sheets) == ["b", "a"] and written.new_rows == [3, 2]


def test_rows_she_typed_herself_are_passed_over_never_written_over():
    sheets = newest_first_sheet()
    sheets.grid["Inbox"].insert(1, ["call the landlord"])  # her own line, on top, no date, no id
    sheets.put("Inbox", 7, 10, "a note of hers far below the last message")

    sheet_writer.write_rows(
        SID, [_agent("d", "2026-09-29 10:15"), _agent("old", "2026-06-30 12:00")], svc=sheets)

    grid = sheets.grid["Inbox"]
    assert grid[1] == ["call the landlord"], "a row that is not a message is not a place to insert"
    assert [row[COL_MESSAGE_ID - 1] for row in _body(sheets)[1:]] == ["d", "c", "b", "a", "old", ""]
    assert sheets.cell("Inbox", 9, 10) == "a note of hers far below the last message"


def test_the_banding_still_begins_on_the_first_data_row_after_rows_go_in_on_top():
    """Tried on the throwaway sheet: an insert AT row 2 pushes a range that
    begins on row 2 down with it, so the new rows sat above the banding,
    unbanded. The rows are inserted below the first row instead, inside the
    range, and that row is moved beneath them."""
    sheets = newest_first_sheet()
    inbox = sheets.tabs["Inbox"]
    assert sheets.band_start[inbox] == 1

    sheet_writer.write_rows(
        SID, [_agent("d", "2026-09-29 10:15"), _agent("e", "2026-09-29 10:40")], svc=sheets)

    assert sheets.band_start[inbox] == 1, "the banding did not drift"
    requests = _row_batches(sheets)[0]
    assert [name for r in requests for name in r] == ["insertDimension", "moveDimension", "updateCells"]
    insert, move, cells = requests
    assert insert["insertDimension"]["range"] == {
        "sheetId": inbox, "dimension": "ROWS", "startIndex": 2, "endIndex": 4}
    assert insert["insertDimension"]["inheritFromBefore"] is True, "a data row's look, never the header's"
    assert move["moveDimension"] == {
        "source": {"sheetId": inbox, "dimension": "ROWS", "startIndex": 1, "endIndex": 2},
        "destinationIndex": 4,
    }
    assert cells["updateCells"]["start"] == {"sheetId": inbox, "rowIndex": 1, "columnIndex": 0}
    assert _ids(sheets) == ["e", "d", "c", "b", "a"]


def test_cells_are_written_as_text_never_evaluated_and_an_empty_cell_is_left_blank():
    sheets = newest_first_sheet()
    row = _agent("d", "2026-09-29 10:15")
    row[2] = '=HYPERLINK("https://evil.example","Invoice")'

    sheet_writer.write_rows(SID, [row], svc=sheets)

    cells = _row_batches(sheets)[0][-1]["updateCells"]
    assert cells["fields"] == "userEnteredValue"
    values = cells["rows"][0]["values"]
    assert len(values) == len(AGENT_COLUMNS), "A..I and nothing to the right of it"
    assert values[2] == {"userEnteredValue": {"stringValue": row[2]}}, \
        "a string value is stored as typed; only a formulaValue is ever evaluated"
    assert values[6] == {}, "no Deadline is no value, not an empty string"
    assert all(set(v) <= {"userEnteredValue"} and "formulaValue" not in str(v) for v in values)


def test_a_rewrite_finds_its_row_by_id_at_the_moment_it_writes_not_by_an_earlier_number():
    sheets = newest_first_sheet()
    assert sheet_writer.id_rows(SID, svc=sheets)["a"] == 4
    # Between the fire's first read and its write she sorted the tab.
    grid = sheets.grid["Inbox"]
    grid[1], grid[3] = grid[3], grid[1]
    before = _body(sheets)
    again = _agent("a", "2026-09-16 09:00")
    again[4] = "Now readable."

    written = sheet_writer.write_rows(
        SID, [], {"a": again, "gone": _agent("gone", "2026-09-01 09:00")}, svc=sheets)

    assert written.updated == {"a": 2} and written.missing == ["gone"]
    body = _body(sheets)
    assert body[0][:9] == again and body[0][9:] == ["Done", "", "", "mine a"], \
        "the agent's cells rewritten, hers on that row untouched"
    assert body[1:] == before[1:], "no other row was written to"


def test_a_rewrite_and_new_rows_in_one_fire_are_one_batch_and_each_reaches_its_own_row():
    """The reason the rewrite is addressed by id: a row inserted on top moves
    every row beneath it, so a number read before the insert names the row
    above the one that was meant."""
    sheets = newest_first_sheet()
    again = _agent("b", "2026-09-18 09:00")
    again[4] = "Now readable."

    written = sheet_writer.write_rows(
        SID, [_agent("d", "2026-09-29 10:15")], {"b": again}, svc=sheets)

    assert len(_row_batches(sheets)) == 1
    assert written.new_rows == [2] and written.updated == {"b": 4}
    body = {row[8]: row for row in _body(sheets)}
    assert body["b"][4] == "Now readable." and body["b"][10] == "note b"
    assert body["c"][4] == "Summary c." and body["a"][4] == "Summary a."


def test_a_refused_write_changes_nothing_at_all():
    sheets = newest_first_sheet()
    before = copy.deepcopy(sheets.grid["Inbox"])
    sheets.fail["batchUpdate:rows"] = _http_error(400)
    again = _agent("b", "2026-09-18 09:00")
    with pytest.raises(SheetsUnavailable, match="row write was refused: HTTP 400"):
        sheet_writer.write_rows(SID, [_agent("d", "2026-09-29 10:15")], {"b": again}, svc=sheets)
    assert sheets.grid["Inbox"] == before, "one batch: all of it or none of it"
    assert sheets.names() == [
        "get", "values.batchGet", "batchUpdate:rows",  # planned and sent, once
        "get", "values.batchGet",                       # the sheet asked what happened
    ], "a batch Sheets refused is not sent a second time"


def test_a_message_already_on_the_sheet_is_not_written_a_second_time():
    sheets = newest_first_sheet()
    written = sheet_writer.write_rows(
        SID, [_agent("b", "2026-09-18 09:00"), _agent("d", "2026-09-29 10:15")], svc=sheets)
    assert _ids(sheets) == ["d", "c", "b", "a"]
    assert written.new_rows == [4, 2], "the row it already has, and the new one"


def test_nothing_to_write_asks_sheets_nothing_and_a_foreign_layout_is_refused_unwritten():
    sheets = newest_first_sheet()
    assert sheet_writer.write_rows(SID, [], {}, svc=sheets) == sheet_writer.Written()
    assert sheets.calls == []
    legacy = legacy_sheet(3)
    before = copy.deepcopy(legacy.grid["Inbox"])
    with pytest.raises(LayoutMismatch):
        sheet_writer.write_rows(SID, [_agent("d", "2026-09-29 10:15")], svc=legacy)
    assert legacy.grid["Inbox"] == before and "batchUpdate:rows" not in legacy.names()


def test_inserting_rows_cannot_move_the_upcoming_view():
    """Upcoming reads whole columns of Inbox, so no insert and no sort can
    shift what it points at; set-up finds nothing to repair afterwards."""
    sheets = newest_first_sheet()
    sheet_writer.write_rows(SID, [_agent("d", "2026-09-29 10:15")], svc=sheets)
    sheets.calls.clear()
    sheet_writer.setup(SID, svc=sheets, reorder=True)
    assert sheets.cell("Upcoming", 1, 0) == UPCOMING_FORMULA and "values.update" not in sheets.names()


# --------------------------------------------------------------------------- #
# The one-time reorder — existing sheets brought into the same order, once
# --------------------------------------------------------------------------- #

def _order_markers(sheets: FakeSheets) -> list[dict]:
    return [m for m in sheets.metadata if m["metadataKey"] == sheet_layout.ORDER_MARKER_KEY]


def _check_as_a_poll(sheets: FakeSheets):
    return sheet_writer.check(
        SID, caller_email=CALLER, svc=sheets, drive=FakeDrive(), reorder=True)


def _sorts(sheets: FakeSheets) -> list[list[dict]]:
    return [kw["body"]["requests"] for name, kw in sheets.calls if name == "batchUpdate:order"]


def test_the_reorder_is_a_stamped_sort_then_a_read_back_and_only_then_the_marker():
    sheets = split_sheet()

    _check_as_a_poll(sheets)

    shape_read = [kw for name, kw in sheets.calls if name == "get" and kw.get("ranges")]
    assert [kw["ranges"] for kw in shape_read] == [["Inbox"]],         "the whole tab: asked about one column, Sheets leaves out the merges beside it"
    assert "rowData" not in shape_read[0]["fields"], "and not a cell of it"
    order = [name for name in sheets.names() if name not in ("values.batchGet", "batchUpdate:layout")]
    assert order == [
        "get",                 # is it the caller's sheet, is it marked already
        "get", "values.get",   # what the tab is made of, then its dates
        "batchUpdate:order",   # the sort
        "values.get",          # the dates again: IS it newest first now?
        "get", "batchUpdate:marker",
    ]
    (sort,) = _sorts(sheets)
    assert [name for r in sort for name in r] == ["createDeveloperMetadata", "sortRange"]
    stamp = sort[0]["createDeveloperMetadata"]["developerMetadata"]
    assert stamp["metadataKey"] == sheet_layout.ROWS_STAMP_KEY, "stamped like every batch that moves rows"
    assert not any(
        r.get("createDeveloperMetadata", {}).get("developerMetadata", {}).get("metadataKey")
        == sheet_layout.ORDER_MARKER_KEY for r in sort
    ), "the sort does not carry the claim that it worked"
    reads = [kw for name, kw in sheets.calls if name == "values.get"]
    assert all(kw["range"] == "Inbox!A:A" and kw["valueRenderOption"] == "UNFORMATTED_VALUE"
               for kw in reads), "the Date column and nothing else, as stored"


def test_the_reorder_makes_a_split_sheet_newest_first_and_keeps_every_cell_of_a_row_together():
    sheets = split_sheet()
    before = {row[8]: row for row in _body(sheets)}
    assert _ids(sheets)[:2] == ["b0", "b1"] and _ids(sheets)[-1] == "n4", "today's mail is last"

    result = _check_as_a_poll(sheets)

    assert result.status == "ok" and result.ordering == sheet_writer.ORDER_APPLIED
    body = _body(sheets)
    assert [row[8] for row in body] == [
        "n4", "n3", "n2", "n1", "n0", "b0", "b1", "b2", "b3", "b4", "b5", "b6", "b7"]
    dates = [row[0] for row in body]
    assert dates == sorted(dates, reverse=True) and len(set(dates)) == len(dates)
    assert {row[8]: row for row in body} == before, \
        "every row is the row it was: her Status, her Notes and her own column M included"
    assert body[3][9:] == ["In progress", "note n1", "", "mine n1"]
    assert sheets.row("Inbox", 0, 11) == list(HEADERS), "the header is not in the sort"
    assert sheets.names().count("batchUpdate:order") == 1
    assert [m["metadataValue"] for m in _order_markers(sheets)] == [sheet_layout.ORDER_VERSION]


def test_the_reorder_runs_once_and_a_sort_of_her_own_afterwards_is_hers():
    sheets = split_sheet()
    _check_as_a_poll(sheets)
    # She sorts the tab her own way (oldest first).
    grid = sheets.grid["Inbox"]
    grid[1:] = sorted((row for row in grid[1:] if any(row)), key=lambda row: row[0])
    hers = copy.deepcopy(grid)
    sheets.calls.clear()

    for _ in range(3):
        assert _check_as_a_poll(sheets).ordering == sheet_writer.ORDER_ALREADY

    assert "batchUpdate:order" not in sheets.names() and sheets.grid["Inbox"] == hers
    assert len(_order_markers(sheets)) == 1


def test_a_check_that_is_not_a_poll_never_moves_a_row():
    """Setting or re-checking the sheet from the panel can land in the middle
    of a fire. Only the fire, which holds the lease, may move rows."""
    sheets = split_sheet()
    before = copy.deepcopy(sheets.grid["Inbox"])
    result = _check(sheets)
    assert result.status == "ok" and result.ordering == sheet_writer.ORDER_PENDING
    assert sheets.grid["Inbox"] == before and _order_markers(sheets) == []
    assert "batchUpdate:order" not in sheets.names()


def test_a_poll_for_someone_who_does_not_own_the_sheet_never_sorts_it():
    """Tried on the throwaway sheet with the real Drive lookup: ownership is
    settled before set-up, and the sort is the last step of set-up — so a
    poll allowed to reorder still moves nothing on a sheet that is not the
    caller's, and leaves no marker claiming it did."""
    sheets = split_sheet()
    before = copy.deepcopy(sheets.grid["Inbox"])
    drive = FakeDrive({"owners": [{"emailAddress": "someone.else@legalsoft.com"}], "permissions": []})

    result = sheet_writer.check(SID, caller_email=CALLER, svc=sheets, drive=drive, reorder=True)

    assert (result.status, result.title, result.ordering) == (sheet_writer.CHECK_NOT_YOURS, "", "")
    assert sheets.names() == ["get"], "the metadata read, and not one write"
    assert sheets.grid["Inbox"] == before and _order_markers(sheets) == []
    assert len(drive.calls) == 1


def test_a_new_sheet_is_marked_newest_first_with_nothing_to_move():
    sheets = FakeSheets()
    assert _check_as_a_poll(sheets).ordering == sheet_writer.ORDER_APPLIED
    assert len(_order_markers(sheets)) == 1 and _body(sheets) == []


def test_a_sort_google_refuses_is_recorded_in_its_own_words_and_the_next_attempt_does_it():
    """Whatever Sheets refuses the sort for, the check is still ``ok``: the
    sheet is writable and mail goes on landing. What she is told keeps
    Google's own sentence, cell reference and all, and never the sheet id."""
    sheets = split_sheet()
    before = copy.deepcopy(sheets.grid["Inbox"])
    sheets.fail["batchUpdate:order"] = HttpError(
        SimpleNamespace(status=400, reason="bad"),
        json.dumps({"error": {"code": 400, "message": (
            "Invalid requests[1].sortRange: You can't sort a range containing something new. "
            f"There is one at K3:K4 of {SID}")}}).encode(),
        uri=f"https://sheets.googleapis.com/v4/spreadsheets/{SID}:batchUpdate")

    result = _check_as_a_poll(sheets)

    assert result.status == "ok" and result.ordering == sheet_writer.ORDER_BLOCKED
    note = result.ordering_note
    assert "Google Sheets refused the sort: You can't sort a range containing something new. " \
           "There is one at K3:K4 of (id)." in note
    assert "New mail is still added at the top" in note and "every hour" in note
    assert SID not in note and "googleapis" not in note and "requests[" not in note
    assert sheets.grid["Inbox"] == before, "the sheet is exactly as it was"
    assert _order_markers(sheets) == [], "and it is NOT marked done"
    assert len(_sorts(sheets)) == 1, "a sort Sheets refused is not sent again"

    # Whatever was in the way gone, the next attempt starts again and finishes.
    del sheets.fail["batchUpdate:order"]
    again = _check_as_a_poll(sheets)
    assert (again.ordering, again.ordering_note) == (sheet_writer.ORDER_APPLIED, "")
    dates = [row[0] for row in _body(sheets)]
    assert dates == sorted(dates, reverse=True) and len(_order_markers(sheets)) == 1


def test_a_sort_refused_part_way_through_its_batch_leaves_no_half_sorted_sheet():
    """The batch is atomic in Sheets: a request it refuses takes the rest
    back with it. Here the stamp is what it refuses."""
    sheets = split_sheet()
    before = copy.deepcopy(sheets.grid["Inbox"])
    sheets.refuse_metadata_key = sheet_layout.ROWS_STAMP_KEY

    result = _check_as_a_poll(sheets)

    assert result.status == "ok" and result.ordering == sheet_writer.ORDER_BLOCKED
    assert sheets.grid["Inbox"] == before and _order_markers(sheets) == []
    sheets.refuse_metadata_key = None
    assert _check_as_a_poll(sheets).ordering == sheet_writer.ORDER_APPLIED


def test_a_tab_in_order_whose_marker_could_not_be_written_says_so_and_is_marked_next_time():
    sheets = split_sheet()
    sheets.refuse_metadata_key = sheet_layout.ORDER_MARKER_KEY

    result = _check_as_a_poll(sheets)

    dates = [row[0] for row in _body(sheets)]
    assert dates == sorted(dates, reverse=True), "the sort itself landed"
    assert result.ordering == sheet_writer.ORDER_BLOCKED and _order_markers(sheets) == []
    assert result.ordering_note.startswith("The Inbox tab is in newest-first order, but")

    sheets.refuse_metadata_key = None
    sheets.calls.clear()
    assert _check_as_a_poll(sheets).ordering == sheet_writer.ORDER_APPLIED
    assert _sorts(sheets) == [], "found in order: marked, and nothing moved a second time"
    assert len(_order_markers(sheets)) == 1


def test_a_reorder_whose_reply_was_lost_is_found_on_the_sheet_and_not_reported_as_failed():
    """The batch applied and the connection died before the answer arrived.
    What is true is on the sheet, so the sheet is asked — and the sort is
    not sent a second time."""
    sheets = split_sheet()
    real_batch = sheets._batch_update

    def applied_but_unanswered(**kw):
        request = real_batch(**kw)
        if any("sortRange" in r for r in kw["body"]["requests"]):
            request.execute()
            return _Req(error=TimeoutError("The write operation timed out"))
        return request

    sheets._batch_update = applied_but_unanswered

    assert _check_as_a_poll(sheets).ordering == sheet_writer.ORDER_APPLIED

    dates = [row[0] for row in _body(sheets)]
    assert dates == sorted(dates, reverse=True) and len(dates) == 13, "no row lost or doubled"
    assert _order_markers(sheets), "marked, so no later poll sorts again"
    assert len(_sorts(sheets)) == 1, "sent once"
    sheets._batch_update = real_batch
    sheets.calls.clear()
    assert _check_as_a_poll(sheets).ordering == sheet_writer.ORDER_ALREADY
    assert "batchUpdate:order" not in sheets.names()


def test_after_the_reorder_new_mail_lands_on_top_of_a_sheet_that_is_one_order():
    sheets = split_sheet()
    _check_as_a_poll(sheets)
    sheet_writer.write_rows(SID, [_agent("today", "2026-09-29 10:15")], svc=sheets)
    ids = _ids(sheets)
    assert ids[0] == "today" and ids[1:6] == ["n4", "n3", "n2", "n1", "n0"] and ids[-1] == "b7"


# --------------------------------------------------------------------------- #
# Pinned 2026-09-30: what the independent verification of the newest-first
# change found, each seen on the throwaway sheet. A row batch whose reply is
# lost was sent again, put the new message on the sheet twice and wrote one
# message over another's row; the one-time sort was marked done on sheets it
# had left out of order; a sheet Sheets would not sort stopped all mail; and a
# Date column not shown as the agent's text sent new mail to the bottom.
# --------------------------------------------------------------------------- #

class _SentAgainOnRetry:
    """What a googleapiclient request is: every ``execute()`` sends it. The
    first answer is lost on the way back (the batch HAS been applied)."""

    def __init__(self, send):
        self._send, self.sent = send, 0

    def execute(self):
        self.sent += 1
        result = self._send()
        if self.sent == 1:
            raise TimeoutError("The read operation timed out")
        return result


def _lose_the_reply(sheets: FakeSheets, *, of: str = "insertDimension") -> list[_SentAgainOnRetry]:
    real_batch = sheets._batch_update
    requests_sent: list[_SentAgainOnRetry] = []

    def batch(**kw):
        if any(of in r for r in kw["body"]["requests"]):
            request = _SentAgainOnRetry(lambda: real_batch(**kw).execute())
            requests_sent.append(request)
            return request
        return real_batch(**kw)

    sheets._batch_update = batch
    return requests_sent


def _her_cells_are_on_their_own_messages(sheets: FakeSheets, before: dict) -> None:
    for row in _body(sheets):
        if row[8] in before:
            assert row[9:] == before[row[8]][9:], (
                f"her cells {row[9:]} now sit on message {row[8]!r}, "
                f"whose own were {before[row[8]][9:]}"
            )


def test_a_row_write_whose_reply_was_lost_does_not_double_rows_or_write_over_another_message():
    sheets = newest_first_sheet()  # c, b, a - her Status/Notes/own column on them
    before = {row[8]: row for row in _body(sheets)}
    requests_sent = _lose_the_reply(sheets)
    again = _agent("b", "2026-09-18 09:00")
    again[4] = "Now readable."

    written = sheet_writer.write_rows(
        SID, [_agent("d", "2026-09-29 10:15")], {"b": again}, svc=sheets)

    assert len(requests_sent) == 1 and requests_sent[0].sent == 1, "one batch, sent once"
    body = _body(sheets)
    ids = [row[8] for row in body]
    assert ids.count("d") == 1, f"the new message is on the sheet {ids.count('d')} times: {ids}"
    assert ids == ["d", "c", "b", "a"], f"a message was written over: {ids}"
    _her_cells_are_on_their_own_messages(sheets, before)
    assert {row[8]: row for row in body}["b"][4] == "Now readable."
    assert written == sheet_writer.Written(new_rows=[2], updated={"b": 4}, missing=[]), \
        "and the fire is told where its rows are, read off the sheet"
    assert sheets.names() == [
        "get", "values.batchGet", "batchUpdate:rows",  # applied; the answer never came
        "get", "values.batchGet",                       # so the sheet was asked
    ]


def test_a_row_write_that_got_no_answer_and_did_not_land_is_planned_again_from_the_sheet():
    """No answer and nothing on the sheet: the write is not re-sent as it
    stood, it is planned again - here she has added a line of her own on top
    in between, and every row number in the first plan is off by one."""
    sheets = newest_first_sheet()
    before = {row[8]: row for row in _body(sheets)}
    real_batch = sheets._batch_update
    sent: list[list[dict]] = []

    def batch(**kw):
        if not any("insertDimension" in r for r in kw["body"]["requests"]):
            return real_batch(**kw)
        sent.append(copy.deepcopy(kw["body"]["requests"]))
        if len(sent) == 1:
            sheets.grid["Inbox"].insert(1, ["her own line, typed meanwhile"])
            return _Req(error=TimeoutError("The read operation timed out"))
        return real_batch(**kw)

    sheets._batch_update = batch
    again = _agent("b", "2026-09-18 09:00")
    again[4] = "Now readable."

    written = sheet_writer.write_rows(
        SID, [_agent("d", "2026-09-29 10:15")], {"b": again}, svc=sheets)

    assert len(sent) == 2 and sent[0] != sent[1], "planned again, not repeated"
    rows = sheets.grid["Inbox"]
    assert rows[1] == ["her own line, typed meanwhile"]
    assert _ids(sheets) == ["", "d", "c", "b", "a"]
    _her_cells_are_on_their_own_messages(sheets, before)
    assert written.new_rows == [3] and written.updated == {"b": 5}
    stamps = [r[0]["createDeveloperMetadata"]["developerMetadata"] for r in sent]
    assert stamps[0]["metadataId"] == stamps[1]["metadataId"], \
        "the same stamp both times: if the first had landed after all, Sheets refuses the second"
    assert stamps[0]["metadataValue"].split("@")[2] == stamps[1]["metadataValue"].split("@")[2]


def test_a_row_write_that_lands_late_makes_sheets_refuse_the_one_sent_after_it():
    """The worst order: no answer, the sheet does not show the batch, it is
    planned and sent again - and only then does the first one land. Both ask
    for the same stamp, so exactly one is applied."""
    sheets = newest_first_sheet()
    before = {row[8]: row for row in _body(sheets)}
    real_batch = sheets._batch_update
    sent: list[dict] = []

    def batch(**kw):
        if not any("insertDimension" in r for r in kw["body"]["requests"]):
            return real_batch(**kw)
        sent.append(copy.deepcopy(kw))
        if len(sent) == 1:
            return _Req(error=TimeoutError("The read operation timed out"))
        if len(sent) == 2:
            real_batch(**sent[0]).execute()  # the first arrives at last, ahead of the second
        return real_batch(**kw)

    sheets._batch_update = batch
    again = _agent("b", "2026-09-18 09:00")
    again[4] = "Now readable."

    written = sheet_writer.write_rows(
        SID, [_agent("d", "2026-09-29 10:15")], {"b": again}, svc=sheets)

    assert len(sent) == 2, "the refused second send is not followed by a third"
    assert _ids(sheets) == ["d", "c", "b", "a"]
    _her_cells_are_on_their_own_messages(sheets, before)
    assert written == sheet_writer.Written(new_rows=[2], updated={"b": 4}, missing=[])


def test_two_writes_planned_from_one_reading_of_the_tab_cannot_both_land():
    """Seen live (R2): another fire's write landed between this one's
    position read and its batch, and this one's rewrite went to the row
    above the one it meant. The stamp makes Sheets refuse the batch planned
    on the old reading; it is planned again on the new one."""
    sheets = newest_first_sheet()
    before = {row[8]: row for row in _body(sheets)}
    real_batch = sheets._batch_update
    overlapped: list[bool] = []

    def batch(**kw):
        if any("insertDimension" in r for r in kw["body"]["requests"]) and not overlapped:
            overlapped.append(True)
            sheet_writer.write_rows(SID, [_agent("theirs", "2026-09-29 09:00")], svc=sheets)
        return real_batch(**kw)

    sheets._batch_update = batch
    again = _agent("b", "2026-09-18 09:00")
    again[4] = "Now readable."

    written = sheet_writer.write_rows(
        SID, [_agent("mine", "2026-09-29 10:15")], {"b": again}, svc=sheets)

    assert _ids(sheets) == ["mine", "theirs", "c", "b", "a"]
    _her_cells_are_on_their_own_messages(sheets, before)
    rows = {row[8]: row for row in _body(sheets)}
    assert rows["b"][4] == "Now readable." and rows["c"][4] == "Summary c."
    assert written.new_rows == [2] and written.updated == {"b": 5}
    assert sheets.names().count("batchUpdate:rows") == 3, "theirs, mine refused, mine again"


def test_a_write_of_rewrites_alone_is_as_safe_to_repeat_as_one_with_new_rows():
    sheets = newest_first_sheet()
    before = {row[8]: row for row in _body(sheets)}
    requests_sent = _lose_the_reply(sheets, of="updateCells")
    again = _agent("b", "2026-09-18 09:00")
    again[4] = "Now readable."

    written = sheet_writer.write_rows(SID, [], {"b": again}, svc=sheets)

    assert requests_sent[0].sent == 1 and written.updated == {"b": 3}
    assert _ids(sheets) == ["c", "b", "a"]
    _her_cells_are_on_their_own_messages(sheets, before)


def test_a_row_write_that_never_gets_an_answer_and_never_lands_fails_loudly_in_the_end():
    sheets = newest_first_sheet()
    before = copy.deepcopy(sheets.grid["Inbox"])
    sheets.fail["batchUpdate:rows"] = TimeoutError("The read operation timed out")

    with pytest.raises(SheetsUnavailable, match="got no answer in 3 attempts") as caught:
        sheet_writer.write_rows(SID, [_agent("d", "2026-09-29 10:15")], svc=sheets)

    assert SID not in str(caught.value)
    assert sheets.grid["Inbox"] == before
    assert sheets.names().count("batchUpdate:rows") == sheet_writer.WRITE_ATTEMPTS
    assert sheets.names()[-2:] == ["get", "values.batchGet"], "the sheet has the last word"


class _Lease:
    """The fire's lease as the writer sees it, recording when it was asked."""

    def __init__(self, sheets: FakeSheets, *, lost_at: int | None = None):
        self.sheets, self.lost_at, self.asked = sheets, lost_at, []

    def _ask(self, what: str) -> None:
        self.asked.append((what, len(self.sheets.calls)))
        if self.lost_at is not None and len(self.asked) >= self.lost_at:
            raise RuntimeError("the lease is another fire's now")

    def prove(self) -> None:
        self._ask("prove")

    def cover(self, seconds: float) -> None:
        self._ask(f"cover {seconds:g}")


def test_the_lease_is_proved_before_the_positions_are_read_and_must_outlast_the_send():
    sheets = newest_first_sheet()
    lease = _Lease(sheets)

    sheet_writer.write_rows(SID, [_agent("d", "2026-09-29 10:15")], svc=sheets, hold=lease)

    assert lease.asked == [("prove", 0), ("cover 45", 2)], \
        "proved with the store before anything is read; nothing but the plan between read and send"
    assert sheets.names() == ["get", "values.batchGet", "batchUpdate:rows"]
    assert sheet_writer.SEND_COVER_SECONDS > sheet_writer.SHEETS_TIMEOUT_SECONDS


@pytest.mark.parametrize("lost_at", [1, 2])
def test_a_write_whose_lease_is_gone_sends_nothing(lost_at):
    sheets = newest_first_sheet()
    before = copy.deepcopy(sheets.grid["Inbox"])
    lease = _Lease(sheets, lost_at=lost_at)

    with pytest.raises(RuntimeError, match="another fire's"):
        sheet_writer.write_rows(SID, [_agent("d", "2026-09-29 10:15")], svc=sheets, hold=lease)

    assert sheets.grid["Inbox"] == before
    assert not any(name.startswith("batchUpdate") for name in sheets.names())


def test_the_lease_is_proved_again_before_a_write_is_planned_a_second_time():
    sheets = newest_first_sheet()
    _lose_the_reply(sheets)
    lease = _Lease(sheets)

    sheet_writer.write_rows(SID, [_agent("d", "2026-09-29 10:15")], svc=sheets, hold=lease)

    assert [what for what, _ in lease.asked] == ["prove", "cover 45", "prove"]


def test_the_sort_is_under_the_lease_too():
    sheets = split_sheet()
    lease = _Lease(sheets)
    sheet_writer.check(SID, caller_email=CALLER, svc=sheets, drive=FakeDrive(), reorder=True,
                       hold=lease)
    assert [what for what, _ in lease.asked] == ["prove", "cover 45"]
    proved_at, covered_at = (at for _what, at in lease.asked)
    names = sheets.names()
    assert names[proved_at:covered_at] == ["get", "values.get"], "proved, then the tab is read"
    assert names[covered_at] == "batchUpdate:order"

    lost = _Lease(split := split_sheet(), lost_at=1)
    before = copy.deepcopy(split.grid["Inbox"])
    with pytest.raises(RuntimeError, match="another fire's"):
        sheet_writer.check(SID, caller_email=CALLER, svc=split, drive=FakeDrive(), reorder=True,
                           hold=lost)
    assert split.grid["Inbox"] == before and _sorts(split) == [] and _order_markers(split) == []


# -- the one-time sort, against Sheets as it really sorts ---------------------- #

def _split_of_ten() -> FakeSheets:
    rows = [_row(f"b{i}", f"2026-09-{18 - i:02d} 09:00", notes=f"note b{i}") for i in range(6)]
    rows += [_row(f"n{i}", f"2026-09-{19 + i:02d} 08:00", notes=f"note n{i}") for i in range(4)]
    return _fill(current_sheet(), rows)


def _marked(sheets: FakeSheets) -> bool:
    return sheet_layout.is_newest_first({"developerMetadata": sheets.metadata})


def _dates_top_to_bottom(sheets: FakeSheets) -> list[str]:
    return [str(row[0]) for row in _body(sheets)]


def test_a_sheet_with_rows_she_has_hidden_is_never_marked_newest_first_while_it_is_not():
    """Seen live (S1a, S6b): hidden rows stay where they are, the rest are
    sorted around them, and the marker was written all the same - so the
    wrong order was permanent."""
    sheets = _split_of_ten()
    sheets.hidden_rows = {3, 4}  # rows 4-5: Hide rows
    before = copy.deepcopy(sheets.grid["Inbox"])

    result = _check_as_a_poll(sheets)

    dates = _dates_top_to_bottom(sheets)
    assert not _marked(sheets) or dates == sorted(dates, reverse=True), (
        f"marked newest-first, but top to bottom the sheet reads {dates}"
    )
    assert not _marked(sheets) and result.ordering == sheet_writer.ORDER_BLOCKED
    assert sheets.grid["Inbox"] == before and _sorts(sheets) == [], \
        "a sort that cannot come out right is not sent: nothing moved"
    assert "rows 4-5 are hidden" in result.ordering_note and "Unhide" in result.ordering_note

    sheets.hidden_rows = set()  # she unhides them
    assert _check_as_a_poll(sheets).ordering == sheet_writer.ORDER_APPLIED
    dates = _dates_top_to_bottom(sheets)
    assert _marked(sheets) and dates == sorted(dates, reverse=True)


def test_a_sheet_whose_filter_is_hiding_rows_is_not_sorted_around_them():
    """The first live sheet, as the census found it on 2026-09-30: a basic
    filter hiding 343 of its 642 rows."""
    sheets = _split_of_ten()
    sheets.filtered_rows = {2, 5, 7}
    before = copy.deepcopy(sheets.grid["Inbox"])

    result = _check_as_a_poll(sheets)

    assert result.status == "ok" and result.ordering == sheet_writer.ORDER_BLOCKED
    assert not _marked(sheets) and sheets.grid["Inbox"] == before and _sorts(sheets) == []
    assert "a filter is hiding 3 rows (rows 3, 6, 8)" in result.ordering_note
    assert "Remove filter" in result.ordering_note


def test_a_date_column_of_text_and_real_dates_is_never_marked_newest_first_while_it_is_not():
    """Seen live (S5c, S5d): every text date first, then every real date -
    so the newest message can end up under the oldest."""
    sheets = _split_of_ten()
    grid = sheets.rows("Inbox")
    for r in (2, 5, 8, 10):  # she re-typed these Date cells; n3, the newest, is one of them
        grid[r][0] = RealDate(grid[r][0])
    before = copy.deepcopy(sheets.grid["Inbox"])

    result = _check_as_a_poll(sheets)

    dates = _dates_top_to_bottom(sheets)
    assert not _marked(sheets) or dates == sorted(dates, reverse=True), (
        f"marked newest-first, but top to bottom the sheet reads {dates}"
    )
    assert not _marked(sheets) and result.ordering == sheet_writer.ORDER_BLOCKED
    assert sheets.grid["Inbox"] == before and _sorts(sheets) == []
    assert "4 cells are real dates: A3, A6, A9 and more" in result.ordering_note


def test_a_date_column_of_real_dates_throughout_is_sorted_and_marked():
    """Seen live (S5a, S5b): numbers sort as numbers."""
    sheets = _split_of_ten()
    for row in sheets.rows("Inbox")[1:]:
        row[0] = RealDate(row[0], shown=f"9/{int(row[0][8:10])}/2026 {row[0][11:]}:00")
    before = {row[8]: row for row in _body(sheets)}

    assert _check_as_a_poll(sheets).ordering == sheet_writer.ORDER_APPLIED

    assert _ids(sheets) == ["n3", "n2", "n1", "n0", "b0", "b1", "b2", "b3", "b4", "b5"]
    assert {row[8]: row for row in _body(sheets)} == before and _marked(sheets)


def test_a_sort_that_leaves_the_tab_out_of_order_is_never_marked_whatever_the_reason():
    """The reasons above are the ones that were found. The rule does not
    depend on the list being complete: the marker follows the read-back."""
    sheets = _split_of_ten()
    real_batch = sheets._batch_update

    def a_sort_that_goes_wrong(**kw):
        request = real_batch(**kw)
        if any("sortRange" in r for r in kw["body"]["requests"]):
            grid = sheets.grid["Inbox"]
            grid[1], grid[2] = grid[2], grid[1]
        return request

    sheets._batch_update = a_sort_that_goes_wrong

    result = _check_as_a_poll(sheets)

    assert result.status == "ok" and result.ordering == sheet_writer.ORDER_BLOCKED
    assert not _marked(sheets)
    assert "row 3 is still newer than the row above it" in result.ordering_note
    assert "could not be confirmed" in result.ordering_note


def test_a_tab_already_newest_first_is_marked_and_nothing_is_moved():
    sheets = newest_first_sheet()
    sheets.merges = [dict(MERGE_K3_K4)]   # which would stop a sort, were one needed
    sheets.hidden_rows = {2}
    before = copy.deepcopy(sheets.grid["Inbox"])

    result = _check_as_a_poll(sheets)

    assert (result.ordering, result.ordering_note) == (sheet_writer.ORDER_APPLIED, "")
    assert sheets.grid["Inbox"] == before and _sorts(sheets) == [] and _marked(sheets)


# -- a sheet that cannot be sorted still gets its mail --------------------------- #

MERGE_K3_K4 = {"sheetId": 1, "startRowIndex": 2, "endRowIndex": 4,
               "startColumnIndex": 10, "endColumnIndex": 11}


def test_a_sheet_with_merged_rows_is_not_sorted_says_where_and_still_takes_its_mail():
    """Seen live (S3b, B4): Sheets answers "You can't sort a range containing
    vertical merges. There is a vertical merge at K3:K4", and every fire
    failed on it. The merge is read off the sheet first, so the sort is not
    even asked for."""
    sheets = split_sheet()
    sheets.merges = [dict(MERGE_K3_K4)]
    before = {row[8]: row for row in _body(sheets)}
    order = _ids(sheets)

    result = _check_as_a_poll(sheets)

    assert result.status == "ok" and result.title == "Her inbox"
    assert result.ordering == sheet_writer.ORDER_BLOCKED and not _marked(sheets)
    assert result.ordering_note == (
        "The Inbox tab has not been put in newest-first order yet: it has merged cells at "
        "K3:K4, and Google Sheets cannot sort rows that are merged together. Unmerge them "
        "(Format > Merge cells > Unmerge). New mail is still added at the top. The agent "
        "tries the sort again every hour."
    )
    assert _sorts(sheets) == [] and _ids(sheets) == order, "left in the order it was in"

    written = sheet_writer.write_rows(SID, [_agent("today", "2026-09-29 10:15")], svc=sheets)

    assert written.new_rows == [2] and _ids(sheets) == ["today", *order]
    assert {row[8]: row for row in _body(sheets) if row[8] != "today"} == before
    assert sheets.merges == [{**MERGE_K3_K4, "startRowIndex": 3, "endRowIndex": 5}], \
        "her merge moved down with its rows"

    sheets.merges = []  # she unmerges
    assert _check_as_a_poll(sheets).ordering == sheet_writer.ORDER_APPLIED
    assert _ids(sheets)[0] == "today" and _marked(sheets)


def test_a_vertical_merge_the_agent_did_not_see_coming_is_still_not_the_end_of_the_mail():
    """The merge appears between the reading of the tab and the sort: Sheets
    refuses, in the words it used on the throwaway sheet."""
    sheets = split_sheet()
    order = _ids(sheets)
    real_batch = sheets._batch_update

    def merged_meanwhile(**kw):
        if any("sortRange" in r for r in kw["body"]["requests"]):
            sheets.merges = [dict(MERGE_K3_K4)]
        return real_batch(**kw)

    sheets._batch_update = merged_meanwhile

    result = _check_as_a_poll(sheets)

    assert result.status == "ok" and result.ordering == sheet_writer.ORDER_BLOCKED
    assert "Google Sheets refused the sort: You can't sort a range containing vertical merges. " \
           "There is a vertical merge at K3:K4." in result.ordering_note
    assert _ids(sheets) == order and not _marked(sheets)
    sheet_writer.write_rows(SID, [_agent("today", "2026-09-29 10:15")], svc=sheets)
    assert _ids(sheets) == ["today", *order]


def test_a_sort_that_gets_no_answer_and_did_not_land_is_not_the_end_of_the_mail_either():
    sheets = split_sheet()
    order = _ids(sheets)
    sheets.fail["batchUpdate:order"] = TimeoutError("The read operation timed out")

    result = _check_as_a_poll(sheets)

    assert result.status == "ok" and result.ordering == sheet_writer.ORDER_BLOCKED
    assert "did not answer" in result.ordering_note and not _marked(sheets)
    assert len(_sorts(sheets)) == 1, "sent once; an unanswered sort is never sent again blind"
    sheet_writer.write_rows(SID, [_agent("today", "2026-09-29 10:15")], svc=sheets)
    assert _ids(sheets) == ["today", *order]


def test_new_mail_goes_on_top_of_a_first_row_that_is_merged_with_the_row_below_it():
    """Seen live (W3a): K2:K3 merged, and the write was refused whole -
    "not possible to move a row to a position that crosses a merged cell"."""
    sheets = newest_first_sheet()
    sheets.merges = [{"sheetId": 1, "startRowIndex": 1, "endRowIndex": 3,
                      "startColumnIndex": 10, "endColumnIndex": 11}]
    before = {row[8]: row for row in _body(sheets)}
    again = _agent("a", "2026-09-16 09:00")
    again[4] = "Now readable."

    written = sheet_writer.write_rows(
        SID, [_agent("d", "2026-09-29 10:15"), _agent("gap", "2026-09-19 10:00"),
              _agent("e", "2026-09-29 10:40")], {"a": again}, svc=sheets)

    assert _ids(sheets) == ["e", "d", "c", "gap", "b", "a"]
    assert written.new_rows == [3, 5, 2] and written.updated == {"a": 7}
    _her_cells_are_on_their_own_messages(sheets, before)
    assert sheets.merges[0]["startRowIndex"] == 3 and sheets.merges[0]["endRowIndex"] == 6, \
        "her merge is two rows lower, and took in the row that went between its two"
    assert not any("moveDimension" in r for r in _row_batches(sheets)[0])


def test_a_sheet_whose_grid_ends_at_the_header_is_marked_in_order_and_takes_its_first_mail():
    """Seen live (W8c): an empty sheet whose empty rows she deleted. The
    sort was refused - "range.startIndex is larger than current grid size
    (1)" - and so was the write."""
    sheets = current_sheet()
    sheets.grid_rows = 1

    result = _check_as_a_poll(sheets)

    assert (result.ordering, result.ordering_note) == (sheet_writer.ORDER_APPLIED, "")
    assert _sorts(sheets) == [] and _marked(sheets), "nothing to sort is in order"

    written = sheet_writer.write_rows(
        SID, [_agent("a", "2026-09-16 09:00"), _agent("b", "2026-09-18 09:00")], svc=sheets)

    assert _ids(sheets) == ["b", "a"] and written.new_rows == [3, 2]
    assert sheets.grid_rows == 3
    assert sheets.plain_rows == [{"sheetId": 1, "startRowIndex": 1, "endRowIndex": 3}], \
        "the header's look, which an insert after row 1 takes, is given back"

    sheet_writer.write_rows(SID, [_agent("c", "2026-09-20 09:00")], svc=sheets)
    assert _ids(sheets) == ["c", "b", "a"] and len(sheets.plain_rows) == 1, "and then as usual"


# -- a Date column that is not shown as the agent wrote it ----------------------- #

def _shown_as_us_dates(sheets: FakeSheets) -> FakeSheets:
    for row in sheets.rows("Inbox")[1:]:
        day, clock = int(row[0][8:10]), row[0][11:]
        row[0] = RealDate(row[0], shown=f"9/{day}/2026 {clock}:00")
    return sheets


def test_new_mail_is_the_first_data_row_when_the_date_column_is_shown_in_another_format():
    """Seen live (W4b): the cells are real dates shown as m/d/yyyy h:mm:ss.
    Read as shown, every one was 'passed over', so new mail went under the
    last row - the very thing the change exists to stop."""
    sheets = _shown_as_us_dates(newest_first_sheet())
    assert [row[0] for row in _body(sheets)] == [
        "9/20/2026 09:00:00", "9/18/2026 09:00:00", "9/16/2026 09:00:00"]
    before = {row[8]: row for row in _body(sheets)}

    written = sheet_writer.write_rows(
        SID, [_agent("old", "2026-06-30 12:00"), _agent("gap", "2026-09-19 12:00"),
              _agent("new", "2026-09-29 12:00")], svc=sheets)

    assert written.new_rows[2] == 2, f"today's mail was put on row {written.new_rows[2]}"
    assert _ids(sheets) == ["new", "c", "gap", "b", "a", "old"]
    _her_cells_are_on_their_own_messages(sheets, before)
    assert {row[8]: row[0] for row in _body(sheets)}["c"] == "9/20/2026 09:00:00", \
        "her cells are read, never rewritten"


def test_the_check_the_sort_and_the_placement_agree_on_a_column_of_real_dates():
    sheets = _shown_as_us_dates(split_sheet())

    assert _check_as_a_poll(sheets).ordering == sheet_writer.ORDER_APPLIED
    order = _ids(sheets)
    assert order[:5] == ["n4", "n3", "n2", "n1", "n0"] and order[-1] == "b7"

    sheet_writer.write_rows(
        SID, [_agent("today", "2026-09-29 10:15"), _agent("gap", "2026-09-18 12:00")], svc=sheets)
    assert _ids(sheets) == ["today", "n4", "n3", "n2", "n1", "n0", "gap", *order[5:]]
    # The agent's own text among her real dates: placed by the same rule, and
    # by that rule the tab is in order.
    column = sheet_writer._date_column(SID, sheets)
    assert sheet_layout.first_out_of_order(column) is None
    assert {type(cell).__name__ for cell in column[1:]} == {"str", "float"}


def test_a_message_id_she_turned_into_a_number_is_still_found_and_not_written_twice():
    sheets = newest_first_sheet()
    real_read = sheets._read

    def read(a1, render="FORMATTED_VALUE"):
        out = real_read(a1, render)
        if a1 == "Inbox!I:I" and render == "UNFORMATTED_VALUE":
            out["values"][2] = [1992837465001234.0]
        return out

    sheets._read = read
    sheets.put("Inbox", 2, 8, "1992837465001234")

    written = sheet_writer.write_rows(
        SID, [_agent("1992837465001234", "2026-09-18 09:00")], svc=sheets)

    assert written.new_rows == [3] and len(_body(sheets)) == 3
