from datetime import date

from marketing_research_agent import snapshots


def _grid():
    """Tracker-shaped fixture: title row w/ Jan+Feb pairs and a quarter col to
    skip, a team block, junk rows, then a GOOGLE channel sub-block."""
    H = ["Meta 360 RA", "Jan (Performance)", "Jan (Investment)",
         "Feb (Performance)", "Feb (Investment)", "1st Quarter"]
    return [
        H,
        ["Management Fees", "", "$2,000.00", "", "$2,200.00", "x"],
        ["Budget", "$9,000.00", "$11,000.00", "$10,000.00", "$12,200.00", "x"],
        ["Spend", "$2,000.00", "$4,000.00", "$2,462.09", "$4,662.09", "x"],
        ["Leads", "2", "", "3", "", "x"],
        ["Qualified Leads", "1", "", "1", "", "x"],
        ["Cost per Lead", "$1,000.00", "$2,000.00", "$820.70", "$1,554.03", "x"],
        ["Total Services Sold (Not Actualized)", "1", "", "2", "", "x"],   # occ 1
        ["Total Services Sold (Not Actualized)", "5", "", "6", "", "x"],   # occ 2 (dup label)
        ["ROAS", "0.00%", "", "#DIV/0!", "", "x"],
        ["A1", "", "", "", "", ""],
        ["GOOGLE", "", "", "", "", ""],
        ["Budget", "$17,000.00", "", "$17,500.00", "", "x"],
        ["Spend", "$3,000.00", "", "$3,406.77", "", "x"],
        ["Leads", "", "", "#N/A", "", "x"],
    ]


def test_is_tracker_grid():
    assert snapshots.is_tracker_grid(_grid()) is True
    assert snapshots.is_tracker_grid([["Record ID", "Email"], ["1", "a@b.c"]]) is False
    assert snapshots.is_tracker_grid([]) is False


def test_slugify():
    assert snapshots.slugify("Meta 360 RA") == "meta-360-ra"
    assert snapshots.slugify("Elevate MKT LS Email") == "elevate-mkt-ls-email"


def test_capture_current_month_canonical():
    snap = snapshots.capture_tab(_grid(), title="Meta 360 RA", gid=559258152,
                                 year=2026, today=date(2026, 2, 7))
    assert snap["vendor"] == "Meta 360 RA"
    assert snap["vendor_slug"] == "meta-360-ra"
    assert snap["date"] == "2026-02-07"
    assert snap["month"] == "2026-02"
    c = snap["canonical"]["team_overall"]
    assert c["management_fees_investment"] == 2200.0
    assert c["budget"] == {"performance": 10000.0, "investment": 12200.0}
    assert c["spend"] == {"performance": 2462.09, "investment": 4662.09}
    assert c["leads"]["total"] == 3
    assert c["cost_metrics"]["cost_per_lead_performance"] == 820.70
    assert c["cost_metrics"]["cost_per_lead_investment"] == 1554.03
    assert c["kpis"]["roas_pct"] is None  # #DIV/0! -> null


def test_capture_duplicate_labels_by_occurrence():
    snap = snapshots.capture_tab(_grid(), title="Meta 360 RA", gid=1,
                                 year=2026, today=date(2026, 2, 7))
    raw = snap["raw"]["team_overall"]
    dups = [r for r in raw if r["label"] == "Total Services Sold (Not Actualized)"]
    assert [d["performance"] for d in dups] == [2.0, 6.0]  # both preserved, in order


def test_capture_channel_block_and_prev_month():
    snap = snapshots.capture_tab(_grid(), title="Meta 360 RA", gid=1,
                                 year=2026, today=date(2026, 2, 7))
    g = snap["canonical"]["channels"]["google"]
    assert g["budget"]["performance"] == 17500.0
    assert g["spend"]["performance"] == 3406.77
    assert g["leads"]["total"] is None  # #N/A
    prev = snap["prev_month_raw"]["team_overall"]
    spend_prev = next(r for r in prev if r["label"] == "Spend")
    assert spend_prev["performance"] == 2000.0  # January column


def test_capture_none_when_not_tracker():
    assert snapshots.capture_tab([["Record ID"], ["1"]], title="Contacts", gid=2,
                                 year=2026, today=date(2026, 2, 7)) is None


def test_capture_none_when_month_missing():
    # July requested but grid only has Jan/Feb -> no current-month column
    assert snapshots.capture_tab(_grid(), title="Meta 360 RA", gid=1,
                                 year=2026, today=date(2026, 7, 7)) is None


def test_config_points_at_view_copy():
    import importlib
    from marketing_research_agent import config as mr_config
    importlib.reload(mr_config)
    assert mr_config.SHEETS_SPREADSHEET_ID == "1bYObEifoIh7zbJsLh9sPJDSkLe3oMvKixv-jdA4Tfg0"
