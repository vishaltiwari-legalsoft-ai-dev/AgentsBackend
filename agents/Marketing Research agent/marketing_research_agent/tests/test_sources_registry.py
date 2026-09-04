"""Multi-sheet source registry: parsing, CRUD, kill-switch, cache scoping.

Every test runs offline (conftest) against a tmp MR_SOURCES_FILE — the cloud
store is exercised in prod via the same _save/_load pair goals.py uses.
"""

from marketing_research_agent import config as mr_config
from marketing_research_agent import profiles, sources_registry as reg
from marketing_research_agent.workbook import TabGrid, grid_signature

SID_A = "1AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt0"
SID_B = "2AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt0"

# Who connected a sheet. The registry is workspace-shared on purpose (see the
# module docstring) — ``added_by`` exists so the DESTRUCTIVE path has an owner
# to check, not so reads can be filtered.
OWNER = "mr-user-owner"
OTHER = "mr-user-other"


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

    src = reg.add_source(SID_A, label="Ops leads sheet", added_by=OWNER)
    assert src["id"] == SID_A and src["label"] == "Ops leads sheet"
    assert src["added_by"] == OWNER

    listed = reg.list_sources(viewer_id=OWNER)
    assert listed[0]["primary"] is True
    assert listed[0]["id"] == mr_config.SHEETS_SPREADSHEET_ID
    assert [s["id"] for s in listed[1:]] == [SID_A]

    assert reg.remove_source(SID_A, requested_by=OWNER) is True
    assert reg.extra_sources() == []
    assert reg.remove_source(SID_A, requested_by=OWNER) is False  # already gone


def test_primary_is_protected(monkeypatch, tmp_path):
    _tmp_store(monkeypatch, tmp_path)
    try:
        reg.add_source(mr_config.SHEETS_SPREADSHEET_ID, label="dup")
        assert False, "adding the primary must raise"
    except ValueError:
        pass
    try:
        reg.remove_source(mr_config.SHEETS_SPREADSHEET_ID, requested_by=OWNER)
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


# --------------------------------------------------------------------------- #
# Ownership of the destructive path (2026-09-05)
# --------------------------------------------------------------------------- #
# Until this date ``remove_source`` took no caller at all: any signed-in user
# could permanently disconnect any connected sheet, and nothing recorded who
# had connected it. Reads stay shared — that is the deliberate design — but the
# delete is now attributable and refusable.


def test_only_the_connector_can_remove_their_sheet(monkeypatch, tmp_path):
    _tmp_store(monkeypatch, tmp_path)
    reg.add_source(SID_A, label="Confidential P&L", added_by=OWNER)

    try:
        reg.remove_source(SID_A, requested_by=OTHER)
        assert False, "another user disconnected a sheet they did not connect"
    except PermissionError:
        pass
    assert [s["id"] for s in reg.extra_sources()] == [SID_A], "the sheet was removed anyway"

    assert reg.remove_source(SID_A, requested_by=OWNER) is True


def test_an_admin_may_remove_a_sheet_somebody_else_connected(monkeypatch, tmp_path):
    """Somebody has to be able to clean up. That somebody is an admin, and it
    is an explicit argument rather than an implicit fallthrough."""
    _tmp_store(monkeypatch, tmp_path)
    reg.add_source(SID_A, label="Ops leads sheet", added_by=OWNER)
    assert reg.remove_source(SID_A, requested_by=OTHER, privileged=True) is True


def test_a_legacy_row_with_no_owner_is_admin_removable_only(monkeypatch, tmp_path):
    """The migration case, pinned.

    Rows written before ``added_by`` existed carry no owner. They stay listed
    for everyone — dropping them from view would be data loss by another name —
    and no ordinary caller can destroy one, because no ordinary caller can
    claim it. Nothing backfills an owner: no store ever recorded one, so any
    value written here would be a guess presented as a fact.
    """
    _tmp_store(monkeypatch, tmp_path)
    reg.add_source(SID_A, label="Connected before attribution")  # no added_by
    legacy = reg.find_source(SID_A)
    assert reg.owner_of(legacy) is None

    assert [s["id"] for s in reg.list_sources(viewer_id=OTHER)[1:]] == [SID_A], (
        "a legacy row disappeared from a caller's list"
    )
    try:
        reg.remove_source(SID_A, requested_by=OTHER)
        assert False, "an ordinary caller destroyed an unowned legacy row"
    except PermissionError as exc:
        assert "admin" in str(exc)

    assert reg.remove_source(SID_A, requested_by=OTHER, privileged=True) is True


def test_remove_source_cannot_be_called_without_a_caller(monkeypatch, tmp_path):
    """``requested_by`` is keyword-only and required on purpose: a call site
    that forgets the caller must fail at the call, not delete quietly."""
    _tmp_store(monkeypatch, tmp_path)
    reg.add_source(SID_A, label="x", added_by=OWNER)
    try:
        reg.remove_source(SID_A)  # type: ignore[call-arg]
        assert False, "remove_source accepted a call with no caller"
    except TypeError:
        pass


def test_list_sources_says_who_connected_each_sheet_and_who_may_remove_it(
        monkeypatch, tmp_path):
    """The console needs both, or the 403 is inexplicable and the button is a
    trap. ``viewer_id=None`` — nobody identified — must not be permissive."""
    _tmp_store(monkeypatch, tmp_path)
    reg.add_source(SID_A, label="Ops leads sheet", added_by=OWNER)

    mine = reg.list_sources(viewer_id=OWNER)[1]
    assert mine["added_by"] == OWNER and mine["can_remove"] is True

    theirs = reg.list_sources(viewer_id=OTHER)[1]
    assert theirs["added_by"] == OWNER and theirs["can_remove"] is False
    assert theirs["label"] == "Ops leads sheet", "the row itself is still shared"

    admin = reg.list_sources(viewer_id=OTHER, privileged=True)[1]
    assert admin["can_remove"] is True

    anon = reg.list_sources()
    assert anon[1]["can_remove"] is False
    assert anon[0]["primary"] is True and anon[0]["can_remove"] is False


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
