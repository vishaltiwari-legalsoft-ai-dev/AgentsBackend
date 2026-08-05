"""Multi-sheet source registry: parsing, CRUD, kill-switch, cache scoping.

Every test runs offline (conftest) against a tmp MR_SOURCES_FILE — the cloud
store is exercised in prod via the same _save/_load pair goals.py uses.
"""

from marketing_research_agent import config as mr_config
from marketing_research_agent import profiles, sources_registry as reg
from marketing_research_agent.workbook import TabGrid, grid_signature

SID_A = "1AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt0"
SID_B = "2AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt0"


def _tmp_store(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))


def test_parse_spreadsheet_id_variants():
    assert reg.parse_spreadsheet_id(
        f"https://docs.google.com/spreadsheets/d/{SID_A}/edit#gid=123") == SID_A
    assert reg.parse_spreadsheet_id(f"https://docs.google.com/spreadsheets/d/{SID_A}/") == SID_A
    assert reg.parse_spreadsheet_id(SID_A) == SID_A
    assert reg.parse_spreadsheet_id("not a sheet link") is None
    assert reg.parse_spreadsheet_id("") is None
    assert reg.parse_spreadsheet_id("short-id") is None


def test_add_list_remove_roundtrip(monkeypatch, tmp_path):
    _tmp_store(monkeypatch, tmp_path)
    assert reg.extra_sources() == []

    src = reg.add_source(SID_A, label="Ops leads sheet")
    assert src["id"] == SID_A and src["label"] == "Ops leads sheet"

    listed = reg.list_sources()
    assert listed[0]["primary"] is True
    assert listed[0]["id"] == mr_config.SHEETS_SPREADSHEET_ID
    assert [s["id"] for s in listed[1:]] == [SID_A]

    assert reg.remove_source(SID_A) is True
    assert reg.extra_sources() == []
    assert reg.remove_source(SID_A) is False  # already gone


def test_primary_is_protected(monkeypatch, tmp_path):
    _tmp_store(monkeypatch, tmp_path)
    try:
        reg.add_source(mr_config.SHEETS_SPREADSHEET_ID, label="dup")
        assert False, "adding the primary must raise"
    except ValueError:
        pass
    try:
        reg.remove_source(mr_config.SHEETS_SPREADSHEET_ID)
        assert False, "removing the primary must raise"
    except ValueError:
        pass


def test_duplicate_add_rejected(monkeypatch, tmp_path):
    _tmp_store(monkeypatch, tmp_path)
    reg.add_source(SID_A, label="x")
    try:
        reg.add_source(SID_A, label="x again")
        assert False, "duplicate add must raise"
    except ValueError:
        pass


def test_include_in_dashboard_defaults_off(monkeypatch, tmp_path):
    """The double-count guard: a pasted sheet must never feed dashboard
    numbers unless explicitly opted in (dashboard phase)."""
    _tmp_store(monkeypatch, tmp_path)
    src = reg.add_source(SID_A, label="copy of tracker")
    assert src["include_in_dashboard"] is False
    assert reg.list_sources()[1]["include_in_dashboard"] is False


def test_kill_switch_empties_extra_sources(monkeypatch, tmp_path):
    _tmp_store(monkeypatch, tmp_path)
    reg.add_source(SID_A, label="x")
    monkeypatch.setenv("MR_MULTI_SHEET", "0")
    assert reg.multi_sheet_enabled() is False
    assert reg.extra_sources() == []          # read paths revert to single-sheet
    assert len(reg.list_sources()) == 2       # config UI still shows what's stored
    monkeypatch.delenv("MR_MULTI_SHEET")
    assert reg.multi_sheet_enabled() is True
    assert [s["id"] for s in reg.extra_sources()] == [SID_A]


def test_service_account_email_offline_and_env(monkeypatch):
    assert "@" in reg.service_account_email()  # offline fallback is the known SA
    monkeypatch.setenv("MR_SERVICE_ACCOUNT_EMAIL", "bot@example.iam.gserviceaccount.com")
    assert reg.service_account_email() == "bot@example.iam.gserviceaccount.com"


def _grid(title: str, rows: list[list[str]]) -> TabGrid:
    return TabGrid(title=title, gid=1, hidden=False, rows=rows,
                   n_rows=len(rows), n_cols=max((len(r) for r in rows), default=0))


def test_profile_cache_is_scoped_per_workbook(monkeypatch, tmp_path):
    """Each workbook caches under its own file; the primary keeps the original
    filename so existing deployments' caches stay valid."""
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    grids_a = [_grid("Tracker", [["Meta X", "Jan (Performance)"], ["Spend", "$10"]])]
    grids_b = [_grid("Leads", [["Name", "Stage"], ["A", "New"], ["B", "Won"]])]

    prof_a = profiles.profile_workbook(grids_a, year=2026, deep=False)
    prof_b = profiles.profile_workbook(grids_b, year=2026, deep=False, scope=SID_B)
    profiles.save_cache(grid_signature(grids_a), prof_a)
    profiles.save_cache(grid_signature(grids_b), prof_b, scope=SID_B)

    assert (tmp_path / "workbook_profiles.json").exists()
    assert (tmp_path / f"workbook_profiles_{SID_B}.json").exists()

    # Each cache answers only for its own workbook's signature.
    assert profiles.load_cached(grid_signature(grids_a)) is not None
    assert profiles.load_cached(grid_signature(grids_b)) is None
    got_b = profiles.load_cached(grid_signature(grids_b), scope=SID_B)
    assert got_b is not None and got_b[0].title == "Leads"
