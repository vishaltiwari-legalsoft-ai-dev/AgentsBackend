"""The sheet writes against a fake Sheets service that keeps a real grid —
cells, inserted columns, hidden columns, dropdowns — and records every
request. So "never sorts, never deletes, never writes outside A:I", "set-up
is idempotent" and "a legacy sheet migrates in place with nothing lost" are
asserted on the cells, not described."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

import app  # noqa: F401 — registers the agent roots on sys.path
from inbox_triage_agent import InboxOffline, sheet_writer
from inbox_triage_agent.sheet_layout import (
    AGENT_COLUMNS, CATEGORY_LABELS, COL_ACTION, COL_MESSAGE_ID, COL_STATUS, HEADERS,
    LEGACY_AGENT_COLUMNS, STATUS_OPTIONS, UPCOMING_FORMULA, UPCOMING_HEADERS,
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


def _http_error(status: int) -> HttpError:
    return HttpError(SimpleNamespace(status=status, reason=f"status {status}"), b"body")


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


class FakeSheets:
    """A spreadsheet as a grid per tab (row 0 is sheet row 1), with the
    Inbox tab's hidden columns and dropdown columns tracked by index — an
    inserted column shifts all three, exactly as Sheets does. ``fail`` maps
    a request name to the HttpError it answers."""

    def __init__(self, *, tabs: dict[str, int] | None = None, title: str = "Her inbox"):
        self.title = title
        self.tabs: dict[str, int] = dict(tabs or {})
        self.grid: dict[str, list[list[str]]] = {t: [] for t in self.tabs}
        self.hidden: set[int] = set()
        self.dropdowns: dict[int, list[str]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, Exception] = {}
        self._next_sheet_id = 100

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

    def _read(self, a1: str) -> dict:
        m = _A1.match(a1)
        tab = m["tab"]
        c1, c2 = _col(m["c1"]), _col(m["c2"] or m["c1"])
        grid = self.rows(tab)
        r1 = int(m["r1"]) - 1 if m["r1"] else 0
        r2 = int(m["r2"]) - 1 if m["r2"] else (r1 if m["c2"] is None and m["r1"] else len(grid) - 1)
        values = []
        for r in range(r1, r2 + 1):
            cells = [self.cell(tab, r, c) for c in range(c1, c2 + 1)]
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
            append=self._values_append, batchUpdate=self._values_batch_update,
            update=self._values_update,
        )

    def _answer(self, name: str, kw: dict, result_fn):
        self.calls.append((name, kw))
        if name in self.fail:
            return _Req(error=self.fail[name])
        return _Req(result_fn())

    def _get(self, **kw):
        return self._answer("get", kw, lambda: {
            "properties": {"title": self.title},
            "sheets": [{"properties": {"title": t, "sheetId": i}} for t, i in self.tabs.items()],
        })

    def _batch_update(self, **kw):
        requests = kw["body"]["requests"]
        name = "batchUpdate:addSheet" if any("addSheet" in r for r in requests) else "batchUpdate:layout"

        def apply():
            replies = []
            for request in requests:
                if "addSheet" in request:
                    title = request["addSheet"]["properties"]["title"]
                    self._next_sheet_id += 1
                    self.tabs[title] = self._next_sheet_id
                    self.rows(title)
                    replies.append({"addSheet": {"properties": {"title": title, "sheetId": self._next_sheet_id}}})
                    continue
                if "insertDimension" in request:
                    rng = request["insertDimension"]["range"]
                    at, n = rng["startIndex"], rng["endIndex"] - rng["startIndex"]
                    for row in self.rows("Inbox"):
                        if len(row) > at:
                            row[at:at] = [""] * n
                    self.hidden = {c + n if c >= at else c for c in self.hidden}
                    self.dropdowns = {c + n if c >= at else c: v for c, v in self.dropdowns.items()}
                elif "updateCells" in request:
                    start = request["updateCells"]["start"]
                    for dr, row in enumerate(request["updateCells"]["rows"]):
                        for dc, value in enumerate(row["values"]):
                            self.put("Inbox", start["rowIndex"] + dr, start["columnIndex"] + dc,
                                     value["userEnteredValue"]["stringValue"])
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
        return _Req(apply())

    def _values_get(self, **kw):
        return self._answer("values.get", kw, lambda: self._read(kw["range"]))

    def _values_batch_get(self, **kw):
        return self._answer("values.batchGet", kw, lambda: {
            "valueRanges": [self._read(a1) for a1 in kw["ranges"]],
        })

    def _values_append(self, **kw):
        def apply():
            rows = kw["body"]["values"]
            grid = self.rows("Inbox")
            first = len(grid) + 1
            for offset, row in enumerate(rows):
                for c, value in enumerate(row):
                    self.put("Inbox", first - 1 + offset, c, value)
            return {"updates": {"updatedRange": f"Inbox!A{first}:I{first + len(rows) - 1}"}}

        return self._answer("values.append", kw, apply)

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
    assert set(sheets.tabs) == {"Inbox", "Upcoming"}
    assert sheets.row("Inbox", 0, len(HEADERS)) == list(HEADERS)
    assert sheets.row("Upcoming", 0, len(UPCOMING_HEADERS)) == list(UPCOMING_HEADERS)
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
        "values.batchUpdate", "values.update",
    ]
    read = next(c[1] for c in sheets.calls if c[0] == "values.batchGet")
    assert read["valueRenderOption"] == "FORMULA", "A2 must be compared as a formula"
    requests = _layout_requests(sheets)
    assert [name for r in requests for name in r] == [
        "updateSheetProperties", "updateDimensionProperties", "setDataValidation",
    ]
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
    assert len([c for c in sheets.calls if c[0].startswith("batchUpdate")]) == 1, "one atomic batch"

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


def test_append_is_one_raw_insert_in_a_to_i_and_returns_the_row_numbers():
    sheets = current_sheet()
    sheets.put("Inbox", 1, COL_MESSAGE_ID - 1, "m1")
    rows = [["d", "f", "s", "c", "sum", "act", "", "link", "m2"], ["d", "f", "s", "c", "sum", "act", "", "link", "m3"]]
    assert sheet_writer.append(SID, rows, svc=sheets) == [3, 4]
    name, kw = sheets.calls[0]
    assert name == "values.append" and kw["range"] == "Inbox!A:I"
    assert kw["valueInputOption"] == "RAW" and kw["insertDataOption"] == "INSERT_ROWS"
    assert sheet_writer.append(SID, [], svc=sheets) == []
    assert len(sheets.calls) == 1, "an empty append must not call the API"


def test_update_is_one_raw_batch_over_the_agents_cells_only():
    sheets = current_sheet()
    count = sheet_writer.update(SID, {7: ["a"] * 9, 3: ["b"] * 9}, svc=sheets)
    assert count == 2
    name, kw = sheets.calls[0]
    assert name == "values.batchUpdate" and kw["body"]["valueInputOption"] == "RAW"
    assert [d["range"] for d in kw["body"]["data"]] == ["Inbox!A3:I3", "Inbox!A7:I7"]
    assert sheet_writer.update(SID, {}, svc=sheets) == 0 and len(sheets.calls) == 1


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
        ("values.append", lambda s: sheet_writer.append(SID, [["x"] * 9], svc=s)),
        ("values.batchUpdate", lambda s: sheet_writer.update(SID, {5: ["x"] * 9}, svc=s)),
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
