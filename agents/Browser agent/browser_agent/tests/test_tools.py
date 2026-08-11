"""The toolbox: the API half of the agent, and the append-only promise.

Google is stubbed throughout — these tests pin behaviour and the safety
boundary, not connectivity.
"""
from __future__ import annotations

import app  # noqa: F401 - registers agent roots on sys.path
import pytest

from browser_agent import tools

SHEET_URL = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345678/edit#gid=0"
SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345678"


# --------------------------------------------------------------------------- #
# The promise: this module cannot overwrite anything
# --------------------------------------------------------------------------- #

def test_the_write_path_has_no_way_to_overwrite():
    """The safety story is 'the capability was never built', so assert exactly
    that against the source rather than trusting a code review."""
    source = (tools.__file__ and open(tools.__file__, encoding="utf-8").read()) or ""
    body = source.split("TOOLS: dict")[0]  # skip the help strings
    for forbidden in ("values().update", "values().clear", ".update(", ".clear("):
        assert forbidden not in body, f"{forbidden} must never exist in tools.py"
    assert "values().append" in body


def test_only_append_is_exposed_as_a_tool():
    assert set(tools.TOOLS) == {
        "sheet_tabs", "sheet_read", "sheet_append", "web_search", "read_url",
    }


def test_write_scope_is_separate_from_marketing_research():
    """MR keeps its read-only scope; the write scope lives only here."""
    from marketing_research_agent.sources import sheets_source

    assert sheets_source.SHEETS_SCOPE.endswith(".readonly")
    assert not tools.WRITE_SCOPE.endswith(".readonly")


# --------------------------------------------------------------------------- #
# Sheet id parsing + error messages a person can act on
# --------------------------------------------------------------------------- #

def test_resolves_a_pasted_sheet_url():
    assert tools.resolve_sheet(SHEET_URL) == SHEET_ID


def test_a_bad_link_says_so():
    with pytest.raises(tools.ToolError, match="doesn't look like"):
        tools.resolve_sheet("my spreadsheet")


def test_missing_credentials_is_not_reported_as_a_sharing_problem(monkeypatch):
    """Two very different fixes; telling someone to re-share a sheet when the
    server has no credentials at all sends them down a dead end."""
    def boom(*_a, **_kw):
        raise RuntimeError("Your default credentials were not found.")

    monkeypatch.setattr(tools, "_write_service", boom)
    with pytest.raises(tools.ToolError) as err:
        tools.sheet_append(SHEET_URL, [["a"]])
    message = str(err.value)
    assert "no Google credentials" in message
    assert "Share it with" not in message


def test_permission_error_names_the_service_account(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("403 permission denied")

    monkeypatch.setattr(tools, "_write_service", boom)
    with pytest.raises(tools.ToolError) as err:
        tools.sheet_append(SHEET_URL, [["a"]])
    message = str(err.value)
    assert "Editor" in message and "@" in message  # tells them exactly what to do


# --------------------------------------------------------------------------- #
# Append behaviour
# --------------------------------------------------------------------------- #

class _FakeSheets:
    """Records what would have been sent to Google."""

    def __init__(self, existing_tabs=("Sheet1",)):
        self.existing = list(existing_tabs)
        self.appended: list[dict] = []
        self.created: list[str] = []

    def spreadsheets(self):
        return self

    def get(self, spreadsheetId=None):  # noqa: N803 - Google's kwarg name
        self._reply = {"sheets": [{"properties": {"title": t}} for t in self.existing]}
        return self

    def batchUpdate(self, spreadsheetId=None, body=None):  # noqa: N803
        title = body["requests"][0]["addSheet"]["properties"]["title"]
        self.created.append(title)
        self.existing.append(title)
        self._reply = {}
        return self

    def values(self):
        return self

    def append(self, **kwargs):
        self.appended.append(kwargs)
        self._reply = {}
        return self

    def execute(self):
        return getattr(self, "_reply", {})


@pytest.fixture()
def fake(monkeypatch):
    svc = _FakeSheets()
    monkeypatch.setattr(tools, "_write_service", lambda: svc)
    return svc


def test_append_creates_its_own_tab_when_missing(fake):
    out = tools.sheet_append(SHEET_URL, [["Vendor", "Note"]])
    assert out["tab"] == tools.DEFAULT_TAB
    assert out["tab_created"] is True
    assert fake.created == [tools.DEFAULT_TAB]
    assert out["rows_written"] == 1


def test_append_reuses_the_tab_second_time(fake):
    tools.sheet_append(SHEET_URL, [["one"]])
    out = tools.sheet_append(SHEET_URL, [["two"]])
    assert out["tab_created"] is False
    assert fake.created == [tools.DEFAULT_TAB]  # created once, not twice


def test_append_never_targets_an_existing_data_tab_by_default(fake):
    tools.sheet_append(SHEET_URL, [["x"]])
    target = fake.appended[0]["range"]
    assert tools.DEFAULT_TAB in target
    assert "Sheet1" not in target


def test_append_uses_insert_rows_not_overwrite(fake):
    tools.sheet_append(SHEET_URL, [["x"]])
    assert fake.appended[0]["insertDataOption"] == "INSERT_ROWS"


def test_append_stringifies_and_survives_none(fake):
    tools.sheet_append(SHEET_URL, [["a", None, 3]])
    assert fake.appended[0]["body"]["values"] == [["a", "", "3"]]


def test_empty_append_is_refused(fake):
    with pytest.raises(tools.ToolError, match="nothing to write"):
        tools.sheet_append(SHEET_URL, [])


def test_huge_append_is_refused(fake):
    rows = [["x"]] * (tools.MAX_APPEND_ROWS + 1)
    with pytest.raises(tools.ToolError, match="at most"):
        tools.sheet_append(SHEET_URL, rows)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def test_sheet_read_one_tab(monkeypatch):
    from marketing_research_agent.sources import sheets_source

    monkeypatch.setattr(
        sheets_source, "fetch_tab_values", lambda sid, title, **k: [["a", "b"], ["1", "2"]]
    )
    out = tools.sheet_read(SHEET_URL, "Leads")
    assert out["tab"] == "Leads" and out["row_count"] == 2


def test_sheet_read_whole_workbook_is_compacted(monkeypatch):
    from marketing_research_agent import workbook

    grid = workbook.TabGrid(
        title="Leads", gid=0, hidden=False,
        rows=[[f"c{c}" for c in range(30)] for _ in range(50)], n_rows=50, n_cols=30,
    )
    monkeypatch.setattr(workbook, "fetch_workbook", lambda sid, **k: [grid])
    out = tools.sheet_read(SHEET_URL)
    rows = out["tabs"][0]["rows"]
    assert out["tabs"][0]["row_count"] == 50   # honest about the real size
    assert len(rows) <= 12 and len(rows[0]) <= tools.MAX_COLS_READ  # but sends a small view


def test_hidden_tabs_are_skipped(monkeypatch):
    from marketing_research_agent import workbook

    hidden = workbook.TabGrid("Old", 1, True, [["x"]], 1, 1)
    shown = workbook.TabGrid("New", 2, False, [["y"]], 1, 1)
    monkeypatch.setattr(workbook, "fetch_workbook", lambda sid, **k: [hidden, shown])
    assert [t["tab"] for t in tools.sheet_read(SHEET_URL)["tabs"]] == ["New"]


# --------------------------------------------------------------------------- #
# Web tools + degradation
# --------------------------------------------------------------------------- #

def test_web_search_returns_titles_and_urls(monkeypatch):
    from seo_geo_agent import sources

    monkeypatch.setattr(
        sources, "serper_search",
        lambda q, client=None: {"organic": [{"title": "T", "link": "https://x.com"}],
                                "related": [], "paa": []},
    )
    out = tools.web_search("gyms in faridabad")
    assert out["results"] == [{"title": "T", "url": "https://x.com"}]


def test_search_unavailable_degrades_with_a_message(monkeypatch):
    from seo_geo_agent import sources

    def missing(*_a, **_kw):
        raise sources.CredentialMissing("offline mode")

    monkeypatch.setattr(sources, "serper_search", missing)
    with pytest.raises(tools.ToolError, match="offline mode"):
        tools.web_search("anything")


def test_read_url_rejects_non_web_addresses():
    with pytest.raises(tools.ToolError, match="isn't a web address"):
        tools.read_url("file:///etc/passwd")


def test_read_url_reports_http_errors(monkeypatch):
    from seo_geo_agent import sources

    monkeypatch.setattr(sources, "fetch_text", lambda u, client=None: {"status": 404, "text": ""})
    with pytest.raises(tools.ToolError, match="404"):
        tools.read_url("https://example.com/missing")


# --------------------------------------------------------------------------- #
# The registry the brain reads
# --------------------------------------------------------------------------- #

def test_unknown_tool_names_the_alternatives():
    with pytest.raises(tools.ToolError, match="sheet_read"):
        tools.run_tool("teleport", {})


def test_grammar_describes_every_tool():
    grammar = tools.grammar()
    for name in tools.TOOLS:
        assert name in grammar


def test_availability_reports_the_service_account():
    ready = tools.availability()
    assert "@" in ready["service_account"]
    assert set(tools.TOOLS) <= set(ready)
