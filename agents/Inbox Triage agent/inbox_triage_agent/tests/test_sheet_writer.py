"""The sheet writes against a fake Sheets service that keeps a grid and
records every request — so "never sorts, never deletes, never writes outside
A:H" and "set-up is idempotent" are asserted, not described."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

import app  # noqa: F401 — registers the agent roots on sys.path
from inbox_triage_agent import InboxOffline, sheet_writer
from inbox_triage_agent.sheet_layout import AGENT_COLUMNS, HEADERS, STATUS_OPTIONS, UPCOMING_FORMULA
from inbox_triage_agent.sheet_writer import SheetsUnavailable

SID = "1abcdefghijklmnopqrstuvwxyz0123456789"
CALLER = "her@legalsoft.com"


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


class FakeSheets:
    """A spreadsheet with tabs, a header row per tab, an id column and A2 of
    Upcoming. ``fail`` maps a request name to the HttpError it answers."""

    def __init__(self, *, tabs: dict[str, int] | None = None, title: str = "Her inbox"):
        self.title = title
        self.tabs: dict[str, int] = dict(tabs or {})
        self.headers: dict[str, list[str]] = {}
        self.upcoming_a2: str = ""
        self.id_column: list[list[str]] = []
        self.appended: list[list[list[str]]] = []
        self.updated: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, Exception] = {}
        self._next_sheet_id = 100

    # -- the client surface ---------------------------------------------- #
    def spreadsheets(self):
        return SimpleNamespace(
            get=self._get, batchUpdate=self._batch_update, values=self._values,
        )

    def _values(self):
        return SimpleNamespace(
            get=self._values_get, batchGet=self._values_batch_get,
            append=self._values_append, batchUpdate=self._values_batch_update,
            update=self._values_update,
        )

    def _answer(self, name: str, kw: dict, result):
        self.calls.append((name, kw))
        if name in self.fail:
            return _Req(error=self.fail[name])
        return _Req(result)

    def _get(self, **kw):
        return self._answer("get", kw, {
            "properties": {"title": self.title},
            "sheets": [{"properties": {"title": t, "sheetId": i}} for t, i in self.tabs.items()],
        })

    def _batch_update(self, **kw):
        replies = []
        for request in kw["body"]["requests"]:
            if "addSheet" in request:
                title = request["addSheet"]["properties"]["title"]
                self._next_sheet_id += 1
                self.tabs[title] = self._next_sheet_id
                replies.append({"addSheet": {"properties": {"title": title, "sheetId": self._next_sheet_id}}})
            else:
                replies.append({})
        name = "batchUpdate:addSheet" if any("addSheet" in r for r in kw["body"]["requests"]) else "batchUpdate:layout"
        return self._answer(name, kw, {"replies": replies})

    def _values_get(self, **kw):
        return self._answer("values.get", kw, {"values": self.id_column})

    def _values_batch_get(self, **kw):
        ranges = []
        for a1 in kw["ranges"]:
            tab, _, cells = a1.partition("!")
            if cells == "A2":
                ranges.append({"values": [[self.upcoming_a2]] if self.upcoming_a2 else []})
            else:
                head = self.headers.get(tab)
                ranges.append({"values": [head]} if head else {})
        return self._answer("values.batchGet", kw, {"valueRanges": ranges})

    def _values_append(self, **kw):
        rows = kw["body"]["values"]
        first = 1 + len(self.id_column)  # id_column[0] is the header, row 1
        self.appended.append(rows)
        self.id_column.extend([[row[7]] for row in rows])
        return self._answer("values.append", kw, {
            "updates": {"updatedRange": f"Inbox!A{first}:H{first + len(rows) - 1}"},
        })

    def _values_batch_update(self, **kw):
        for item in kw["body"]["data"]:
            tab, _, cells = item["range"].partition("!")
            if cells in ("A1:J1", "A1:H1"):
                current = self.headers.get(tab, [])
                new = list(item["values"][0])
                self.headers[tab] = new + current[len(new):] if cells == "A1:H1" else new
            else:
                self.updated.append(item)
        return self._answer("values.batchUpdate", kw, {})

    def _values_update(self, **kw):
        if kw["range"].endswith("!A2"):
            self.upcoming_a2 = kw["body"]["values"][0][0]
        return self._answer("values.update", kw, {})

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


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
    assert sheets.headers["Inbox"] == list(HEADERS)
    assert sheets.upcoming_a2 == UPCOMING_FORMULA


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
        "get", "batchUpdate:addSheet", "batchUpdate:layout", "values.batchGet",
        "values.batchUpdate", "values.update",
    ]
    layout = [name for call in sheets.calls if call[0] == "batchUpdate:layout"
              for r in call[1]["body"]["requests"] for name in r]
    assert layout == ["updateSheetProperties", "updateDimensionProperties", "setDataValidation"]
    requests = next(c[1] for c in sheets.calls if c[0] == "batchUpdate:layout")["body"]["requests"]
    assert requests[0]["updateSheetProperties"]["properties"]["gridProperties"] == {"frozenRowCount": 1}
    hidden = requests[1]["updateDimensionProperties"]["range"]
    assert (hidden["startIndex"], hidden["endIndex"]) == (7, 8)  # column H
    validation = requests[2]["setDataValidation"]
    assert (validation["range"]["startColumnIndex"], validation["range"]["endColumnIndex"]) == (8, 9)  # I
    assert [v["userEnteredValue"] for v in validation["rule"]["condition"]["values"]] == list(STATUS_OPTIONS)
    formula = next(c[1] for c in sheets.calls if c[0] == "values.update")
    assert formula["valueInputOption"] == "USER_ENTERED" and formula["range"] == "Upcoming!A2"
    assert sheets.headers == {"Inbox": list(HEADERS), "Upcoming": list(HEADERS)}


def test_setup_twice_changes_nothing_the_second_time():
    sheets = FakeSheets()
    sheet_writer.setup(SID, svc=sheets)
    sheets.calls.clear()
    sheet_writer.setup(SID, svc=sheets)
    # No tab creation, no header write, no formula write: the layout write is
    # the one repeated call, and it is the write that proves editability.
    assert sheets.names() == ["get", "batchUpdate:layout", "values.batchGet"]
    assert sheets.headers == {"Inbox": list(HEADERS), "Upcoming": list(HEADERS)}
    assert sheets.upcoming_a2 == UPCOMING_FORMULA


def test_setup_repairs_the_agents_header_cells_and_leaves_hers_alone():
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
    sheets.headers = {"Inbox": ["Date", "Sender", "Subject", "Category", "Summary",
                                "Deadline", "Link", "Message ID", "State", "My notes"],
                      "Upcoming": list(HEADERS)}
    sheets.upcoming_a2 = UPCOMING_FORMULA
    sheet_writer.setup(SID, svc=sheets)
    write = next(c[1] for c in sheets.calls if c[0] == "values.batchUpdate")
    assert [d["range"] for d in write["body"]["data"]] == ["Inbox!A1:H1"]
    assert write["body"]["valueInputOption"] == "RAW"
    assert sheets.headers["Inbox"] == list(AGENT_COLUMNS) + ["State", "My notes"]


# --------------------------------------------------------------------------- #
# rows
# --------------------------------------------------------------------------- #

def test_id_rows_skips_the_header_and_blanks_and_keeps_the_first_occurrence():
    sheets = FakeSheets()
    sheets.id_column = [["Message ID"], ["m1"], [], [""], ["m2"], ["m1"]]
    assert sheet_writer.id_rows(SID, svc=sheets) == {"m1": 2, "m2": 5}
    _, kw = sheets.calls[0]
    assert kw["range"] == "Inbox!H:H"


def test_append_is_one_raw_insert_in_a_to_h_and_returns_the_row_numbers():
    sheets = FakeSheets()
    sheets.id_column = [["Message ID"], ["m1"]]
    rows = [["d", "f", "s", "c", "sum", "", "link", "m2"], ["d", "f", "s", "c", "sum", "", "link", "m3"]]
    assert sheet_writer.append(SID, rows, svc=sheets) == [3, 4]
    name, kw = sheets.calls[0]
    assert name == "values.append" and kw["range"] == "Inbox!A:H"
    assert kw["valueInputOption"] == "RAW" and kw["insertDataOption"] == "INSERT_ROWS"
    assert sheet_writer.append(SID, [], svc=sheets) == []
    assert len(sheets.calls) == 1, "an empty append must not call the API"


def test_update_is_one_raw_batch_over_the_agents_cells_only():
    sheets = FakeSheets()
    count = sheet_writer.update(SID, {7: ["a"] * 8, 3: ["b"] * 8}, svc=sheets)
    assert count == 2
    name, kw = sheets.calls[0]
    assert name == "values.batchUpdate" and kw["body"]["valueInputOption"] == "RAW"
    assert [d["range"] for d in kw["body"]["data"]] == ["Inbox!A3:H3", "Inbox!A7:H7"]
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
    sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
    sheets.headers = {"Inbox": list(HEADERS), "Upcoming": list(HEADERS)}
    sheets.upcoming_a2 = UPCOMING_FORMULA
    assert _check(sheets).status == "ok"
    assert sheets.names() == ["get", "batchUpdate:layout", "values.batchGet"]

    sheets.calls.clear()
    sheets.headers["Inbox"] = ["Date", "From", "Subject", "Category", "Summary",
                               "Deadline", "Link", "Msg", "State", "My notes"]
    assert _check(sheets).status == "ok"
    assert sheets.names() == ["get", "batchUpdate:layout", "values.batchGet", "values.batchUpdate"]
    write = sheets.calls[-1][1]["body"]["data"]
    assert [d["range"] for d in write] == ["Inbox!A1:H1"], "her I and J headers are never written"
    assert sheets.headers["Inbox"][8:] == ["State", "My notes"]


def test_a_refused_row_call_is_sheets_unavailable_not_a_raw_http_error():
    """The fire catches ``SheetsUnavailable`` and records it on ``last_poll``;
    it does not catch ``HttpError``. She un-shares the sheet, or renames the
    Inbox tab, between two hourly checks: the next fire's id read is refused,
    and that refusal has to reach the panel as a sentence -- not escape the
    fire as a raw client error that leaves ``last_poll`` saying all is well."""
    escaped = []
    for call, run in (
        ("values.get", lambda s: sheet_writer.id_rows(SID, svc=s)),
        ("values.append", lambda s: sheet_writer.append(SID, [["x"] * 8], svc=s)),
        ("values.batchUpdate", lambda s: sheet_writer.update(SID, {5: ["x"] * 8}, svc=s)),
    ):
        sheets = FakeSheets(tabs={"Inbox": 1, "Upcoming": 2})
        sheets.id_column = [["Message ID"]]
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
    uri = f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/Inbox%21H%3AH"
    for status in (403, 503):
        sheets = FakeSheets()
        sheets.fail["values.get"] = HttpError(SimpleNamespace(status=status, reason="r"), b"body", uri=uri)
        with pytest.raises(SheetsUnavailable) as caught:
            sheet_writer.id_rows(SID, svc=sheets)
        text = str(caught.value)
        assert SID not in text and "googleapis" not in text, text
        assert f"HTTP {status}" in text


def test_the_drive_client_refuses_to_build_offline():
    with pytest.raises(InboxOffline):
        sheet_writer.drive_service()
