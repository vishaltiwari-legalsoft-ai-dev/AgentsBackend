"""Lead-analysis sheet: detection, parsing, aggregation, and the five flag
rules from the 2026-08-10 requirements doc."""

from datetime import date

import pytest

from marketing_research_agent import goals
from marketing_research_agent import lead_analysis as la
from marketing_research_agent import reports
from marketing_research_agent.schemas import CampaignMetric

HEADER = [
    "Demo Month", "Demo Date", "First Name", "Last Name", "Law Firm", "Email",
    "Hubspot Link", "Phone Number", "SPC Assigned", "Campaign", "Brand", "Source",
    "Original Lead Source", "Meeting Outcome", "Deal Stage", "VA Deal",
    "$ Amount", "MRR", "Price", "No. of Services Sold",
]


def _row(month="August", campaign="Meta 360 RA", brand="RA", source="Meta",
         outcome="Completed", stage="Contract Sent", amount="", mrr="", services=""):
    return [month, f"{month} 3, 2026", "A", "B", "Firm", "a@b.c", "url", "123",
            "SPC", campaign, brand, source, "Flytech Meta LS", outcome, stage, "",
            amount, mrr, "", services]


def _grid(rows):
    # Junk banner + blank row above the real header, like a live sheet.
    return [["Lead Analysis"], [], HEADER, *rows]


@pytest.fixture(autouse=True)
def _isolated_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))


# --- detection / parsing ----------------------------------------------------

def test_find_lead_tab_resolves_columns():
    found = la.find_lead_tab(_grid([]))
    assert found and found["header_row"] == 2
    cols = found["cols"]
    # "Source" must win over "Original Lead Source" (exact > contains).
    assert cols["source"] == HEADER.index("Source")
    assert cols["campaign"] == HEADER.index("Campaign")
    assert cols["services_sold"] == HEADER.index("No. of Services Sold")


def test_find_lead_tab_rejects_non_lead_grids():
    assert la.find_lead_tab([["Spend", "August", "September"], ["$100", "1", "2"]]) is None
    assert la.find_lead_tab([]) is None


def test_parse_normalizes_outcomes_stages_and_money():
    rows = [
        _row(outcome="Completed", stage="Contract Sent", amount="$2,000.00", mrr="$2,000.00", services="1"),
        _row(outcome="No Show", stage="Demo No Show"),
        _row(outcome="Canceled", stage="Demo No Show"),
        _row(outcome="Bad Lead", stage="Lost DNC"),
        _row(outcome="", stage=""),  # upcoming demo
    ]
    records, gaps = la.parse_lead_rows(_grid(rows), year=2026)
    assert not gaps
    assert [r["outcome"] for r in records] == ["completed", "no_show", "canceled", "bad_lead", "pending"]
    assert [r["stage"] for r in records] == ["contract_sent", "demo_no_show", "demo_no_show", "lost_dnc", "none"]
    assert records[0]["month"] == "2026-08"
    assert records[0]["amount"] == 2000.0 and records[0]["services_sold"] == 1


def test_parse_skips_unreadable_months_with_a_gap_note():
    records, gaps = la.parse_lead_rows(_grid([_row(month="???")]), year=2026)
    assert records == []
    assert gaps and "Demo Month" in gaps[0]


# --- aggregation ------------------------------------------------------------

def test_summarize_counts_and_rates_over_resolved_only():
    rows = [
        _row(outcome="Completed"), _row(outcome="Completed"),
        _row(outcome="No Show", stage="Demo No Show"),
        _row(outcome="Bad Lead", stage="Lost DNC"),
        _row(outcome="", stage=""), _row(outcome="", stage=""),  # pending — never in a denominator
    ]
    records, _ = la.parse_lead_rows(_grid(rows), year=2026)
    s = la.summarize(records)
    v = s["months"]["2026-08"]["vendors"][0]
    assert v["booked"] == 6 and v["resolved"] == 4 and v["pending"] == 2
    assert v["completed_rate_pct"] == 50.0
    assert v["bad_lead_rate_pct"] == 25.0
    assert v["deal_stages"] == {"contract_sent": 2, "demo_no_show": 1, "lost_dnc": 1, "none": 2}
    assert s["latest_month"] == "2026-08"


# --- the five flag rules ----------------------------------------------------

def _metrics(records, **kw):
    s = la.summarize(records, **kw)
    v = s["months"]["2026-08"]["vendors"][0]
    return {f["metric"] for f in v["flags"]}, v


def test_bad_lead_rate_flags_over_30_pct():
    bad = [_row(outcome="Bad Lead")] * 4 + [_row(outcome="Completed")] * 6
    records, _ = la.parse_lead_rows(_grid(bad), year=2026)
    metrics, _ = _metrics(records)
    assert "bad_lead_rate" in metrics

    at_line = [_row(outcome="Bad Lead")] * 3 + [_row(outcome="Completed")] * 7
    records, _ = la.parse_lead_rows(_grid(at_line), year=2026)
    metrics, _ = _metrics(records)
    assert "bad_lead_rate" not in metrics  # exactly 30% is the line, not over it


def test_no_show_and_canceled_have_separate_rules():
    rows = [_row(outcome="No Show")] * 4 + [_row(outcome="Canceled")] * 3 + [_row(outcome="Completed")] * 3
    records, _ = la.parse_lead_rows(_grid(rows), year=2026)
    metrics, v = _metrics(records)
    assert "no_show_rate" in metrics       # 40% > 30%
    assert "canceled_rate" in metrics      # 30% > 20%
    messages = " ".join(f["message"] for f in v["flags"])
    assert "booking/confirmation" in messages and "calendar invite" in messages


def test_zero_completed_flags_only_once_demos_actually_resolve():
    resolved = [_row(outcome="No Show")] * 3
    records, _ = la.parse_lead_rows(_grid(resolved), year=2026)
    metrics, _ = _metrics(records)
    assert "zero_completed" in metrics

    mostly_pending = [_row(outcome="No Show")] * 2 + [_row(outcome="")] * 5
    records, _ = la.parse_lead_rows(_grid(mostly_pending), year=2026)
    metrics, _ = _metrics(records)
    assert "zero_completed" not in metrics  # upcoming demos can't trip it


def test_ql_high_booking_low_needs_the_tracker_join():
    records, _ = la.parse_lead_rows(_grid([_row(outcome="Completed")]), year=2026)
    rollup = {"2026-08": {"meta-360-ra": {
        "vendor": "Meta 360 RA", "leads": 100, "qualified_leads": 80, "demos_booked": 10}}}
    metrics, v = _metrics(records, tracker_rollups=rollup)
    assert "ql_high_booking_low" in metrics  # QL 80% & booking 10%
    assert v["matched_vendor"] == "Meta 360 RA"
    assert v["tracker"]["ql_ratio_pct"] == 80.0

    rollup["2026-08"]["meta-360-ra"]["demos_booked"] = 20  # booking 20% — process fine
    metrics, _ = _metrics(records, tracker_rollups=rollup)
    assert "ql_high_booking_low" not in metrics

    # No tracker match at all → no rule, campaign surfaces as unmatched.
    s = la.summarize(records, tracker_rollups={})
    assert s["unmatched_campaigns"] == ["Meta 360 RA"]


# --- report surfaces --------------------------------------------------------

def _flagged_summary():
    rows = [_row(outcome="Bad Lead")] * 4 + [_row(outcome="Completed")] * 6
    records, _ = la.parse_lead_rows(_grid(rows), year=2026)
    rollup = {"2026-08": {"meta-360-ra": {
        "vendor": "Meta 360 RA", "leads": 50, "qualified_leads": 20, "demos_booked": 15}}}
    return la.summarize(records, tracker_rollups=rollup)


def test_red_flag_entries_use_matched_vendor_name():
    entries = la.red_flag_entries(_flagged_summary())
    assert entries[0]["vendor"] == "Meta 360 RA"
    assert any("Bad-lead rate" in r for r in entries[0]["reasons"])


def test_flag_summary_groups_by_rule():
    rows = la.flag_summary(_flagged_summary()["months"]["2026-08"])
    assert rows == [{"metric": "bad_lead_rate", "level": "red", "count": 1,
                     "text": "1 vendor over the bad-lead line (lead-quality fix)"}]


def _campaign_ds(lead_summary):
    m = CampaignMetric(
        channel="META", campaign="c", utm_source="m", utm_medium="cpc",
        utm_campaign="c", spend=1000.0, leads=50, qualified_leads=20,
        demos_booked=15, demos_completed=9, date=date(2026, 8, 3),
    )
    return {"metrics": [m], "vendor_metrics": {"Meta 360 RA": [m]},
            "lead_summary": lead_summary, "lead_month_keys": ["2026-08"],
            "today": date(2026, 8, 10)}


def test_campaign_structured_merges_lead_flags_into_red_flag_vendors(monkeypatch):
    monkeypatch.setenv("MR_OFFLINE", "1")
    s = reports._campaign_structured(_campaign_ds(_flagged_summary()),
                                     goals.get_targets("u1"))
    assert "2026-08" in s["lead_quality"]["months"]
    meta = next(r for r in s["red_flag_vendors"] if r["vendor"] == "Meta 360 RA")
    assert any("Bad-lead rate" in reason for reason in meta["reasons"])


def test_overview_carries_lead_quality_and_flags(monkeypatch):
    monkeypatch.setenv("MR_OFFLINE", "1")
    out = reports.overview(_campaign_ds(_flagged_summary()), "u1")
    assert out["lead_quality"]["vendors"][0]["campaign"] == "Meta 360 RA"
    assert any(f["metric"] == "bad_lead_rate" for f in out["flag_summary"])


def test_overview_without_lead_summary_is_unchanged(monkeypatch):
    monkeypatch.setenv("MR_OFFLINE", "1")
    ds = _campaign_ds(None)
    out = reports.overview(ds, "u1")
    assert out["lead_quality"] is None
    assert all(f["metric"] != "bad_lead_rate" for f in out["flag_summary"])
