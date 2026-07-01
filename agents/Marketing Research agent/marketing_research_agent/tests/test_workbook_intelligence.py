"""Offline tests for the workbook intelligence layer (profiling + insight).
Run with MR_OFFLINE=1 so the LLM paths fall back to deterministic logic."""

from marketing_research_agent import insight
from marketing_research_agent.profiles import _heuristic_profile, profile_workbook
from marketing_research_agent.workbook import TabGrid, compact_grid, grid_signature

YEAR = 2026

TRACKER = TabGrid(
    title="Marketing 2026 Overall Report", gid=1, hidden=False,
    rows=[
        ["All", "Jan (Performance)", "Jan (Investment)", "Feb (Performance)", "Feb (Investment)"],
        ["Spend ", "$100", "$110", "$120", "$120"],
        ["Leads", "5", "", "8", ""],
        ["Total Demos Booked (SDR+VAPI+Direct)", "2", "", "4", ""],
        ["Demos Completed (SDR+VAPI+Direct)", "1", "", "3", ""],
    ],
    n_rows=5, n_cols=5,
)
LOOKER = TabGrid("Looker Studio per Brand (May)", 2, True, [["x"]], 1, 1)
CONTROL = TabGrid("DropdownControls", 3, False, [["ctrl"]], 1, 1)
LEADS = TabGrid(
    "Leads Tracker Month to Month", 4, False,
    rows=[["Month", "Google", "Meta"], ["January", "10", "20"], ["February", "12", "18"]],
    n_rows=3, n_cols=3,
)
ALL = [TRACKER, LOOKER, CONTROL, LEADS]


def test_heuristic_classifies_tracker():
    p = _heuristic_profile(TRACKER, YEAR)
    assert p.kind == "performance_tracker"
    assert p.granularity == "monthly"
    assert p.useful is True


def test_heuristic_marks_looker_and_control_not_useful():
    assert _heuristic_profile(LOOKER, YEAR).useful is False
    assert _heuristic_profile(CONTROL, YEAR).kind == "control"


def test_leads_tab_detects_platforms():
    p = _heuristic_profile(LEADS, YEAR)
    assert p.kind == "leads_by_period"
    assert "Google" in p.platforms and "Meta" in p.platforms


def test_profile_workbook_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    first = profile_workbook(ALL, year=YEAR)
    assert len(first) == 4
    # second call hits the cache for the same signature
    again = profile_workbook(ALL, year=YEAR)
    assert [p.title for p in again] == [p.title for p in first]


def test_grid_signature_changes_with_shape():
    sig = grid_signature(ALL)
    changed = grid_signature(ALL[:3])
    assert sig != changed


def test_select_tabs_skips_non_useful():
    profiles = [_heuristic_profile(g, YEAR) for g in ALL]
    picked = insight.select_tabs("how did we do this month", "monthly", profiles)
    assert picked
    assert "Looker Studio per Brand (May)" not in picked
    assert "DropdownControls" not in picked


def test_answer_returns_grounded_text():
    profiles = [_heuristic_profile(g, YEAR) for g in ALL]
    grids = {g.title: g.rows for g in ALL}
    out = insight.answer("How much did we spend this month?", profiles, grids, year=YEAR)
    assert out["answer"] and out["used_tabs"]
    assert isinstance(out["answer"], str) and len(out["answer"]) > 10


def test_slice_filters_long_tab_by_month():
    rows = [
        ["Vendor", "Month", "Budget", "Spend"],
        ["A", "January", "$100", "$90"],
        ["B", "June", "$200", "$250"],
        ["C", "June", "$300", "$280"],
        ["D", "July", "$100", "$100"],
    ]
    out = insight.slice_for_timeframe(rows, ("June", 6))
    assert out[0][0] == "Vendor"  # header kept
    names = [r[0] for r in out[1:]]
    assert names == ["B", "C"]  # only June rows


def test_slice_selects_month_columns_in_wide_tab():
    rows = [
        ["Metric", "May (Performance)", "May (Investment)", "June (Performance)", "June (Investment)"],
        ["Spend", "$10", "$11", "$20", "$22"],
        ["Leads", "5", "", "8", ""],
    ]
    out = insight.slice_for_timeframe(rows, ("June", 6))
    assert out[0] == ["Metric", "June (Performance)", "June (Investment)"]
    assert out[1] == ["Spend", "$20", "$22"]


def test_target_month_parsing():
    assert insight.target_month("vendor report for the month of june") == ("June", 6)
    assert insight.target_month("how did we do") is None


def test_compact_grid_bounds():
    big = [[str(i)] * 40 for i in range(50)]
    c = compact_grid(big, max_rows=5, max_cols=6)
    assert len(c) == 5 and len(c[0]) == 6
