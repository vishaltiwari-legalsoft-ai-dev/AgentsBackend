from datetime import date

import pytest

from marketing_research_agent import reports
from marketing_research_agent.schemas import CampaignMetric, Lead, MediaOpportunity


def _dataset():
    m = CampaignMetric(
        channel="Google", campaign="c", utm_source="g", utm_medium="cpc",
        utm_campaign="c", spend=1200.0, leads=12, qualified_leads=9,
        demos_booked=4, demos_completed=2, date=date(2026, 6, 29),
    )
    l = Lead(
        id="1", channel="Google", utm_source="g", utm_medium="cpc", utm_campaign="c",
        practice_area="PI", stage="qualified", created_at=date(2026, 6, 29),
    )
    o = MediaOpportunity(
        name="Pod", type="podcast", audience_size=50000, engagement_rate=0.8,
        host_authority=0.9, practice_area_fit=1.0,
    )
    return {"metrics": [m], "leads": [l], "opportunities": [o], "today": date(2026, 6, 30)}


def test_daily_summary_builds(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    r = reports.build("daily_summary", _dataset(), user_id="u1")
    assert r["kind"] == "daily_summary"
    assert r["markdown"] and r["html"] and r["structured"]["channels"]["Google"]


def test_all_kinds_build_without_error(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    for kind in reports.KINDS:
        r = reports.build(kind, _dataset(), user_id="u1")
        assert r["markdown"]


def test_daily_movement_report_builds(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    deltas = [{"vendor": "Meta 360 RA", "vendor_slug": "meta-360-ra", "date": "2026-02-07",
               "since": "2026-02-06", "days": 1, "month_start": False, "corrected": False,
               "blocks": {"team_overall": {"additive": {"spend.performance": {"delta": 100.0, "mtd": 300.0, "corrected": False},
                                                        "leads.total": {"delta": 2, "mtd": 5, "corrected": False}},
                                           "rates": {}},
                          "channels": {}}}]
    r = reports.build("daily_movement", {"snapshot_deltas": deltas}, user_id="u1")
    assert r["kind"] == "daily_movement"
    assert r["structured"]["vendors"][0]["vendor"] == "Meta 360 RA"
    assert r["markdown"]


def test_report_stamps_sources(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    ds = _dataset()
    ds["sources"] = [{"platform": "sheets:123", "generated_at": "2026-07-07T00:00:00+00:00",
                      "metrics": 1, "leads": 1}]
    r = reports.build("daily_summary", ds, user_id="u1")
    assert r["sources"] == ds["sources"]


def test_daily_report_covers_month_to_date_through_yesterday(monkeypatch, tmp_path):
    """On July 9 the daily report must read July 1–8: July data only, no other
    months, and the period stamped on the report."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))

    def m(month, day, spend):
        return CampaignMetric(
            channel="Google", campaign="c", utm_source="g", utm_medium="cpc",
            utm_campaign="c", spend=spend, leads=10, qualified_leads=5,
            demos_booked=2, demos_completed=1, date=date(2026, month, day),
        )

    ds = {"metrics": [m(6, 15, 999.0), m(7, 1, 500.0), m(9, 1, 2200.0)],
          "leads": [], "today": date(2026, 7, 9)}
    r = reports.build("daily_summary", ds, user_id="u1")
    p = r["structured"]["period"]
    assert p["start"] == "2026-07-01" and p["end"] == "2026-07-08"
    assert r["structured"]["totals"]["spend"] == 500.0  # June + future September excluded


def test_quarterly_report_spans_the_quarter(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))

    def m(month, spend):
        return CampaignMetric(
            channel="Google", campaign="c", utm_source="g", utm_medium="cpc",
            utm_campaign="c", spend=spend, leads=10, qualified_leads=5,
            demos_booked=2, demos_completed=1, date=date(2026, month, 1),
        )

    ds = {"metrics": [m(6, 100.0), m(7, 200.0)], "leads": [], "today": date(2026, 7, 9)}
    r = reports.build("quarterly_summary", ds, user_id="u1")
    assert r["structured"]["period"]["start"] == "2026-07-01"
    assert r["structured"]["totals"]["spend"] == 200.0  # Q3 only; June is Q2


def test_report_names_red_flag_vendors_and_insights(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))

    def m(spend, ql):
        return CampaignMetric(
            channel="Google", campaign="c", utm_source="g", utm_medium="cpc",
            utm_campaign="c", spend=spend, leads=10, qualified_leads=ql,
            demos_booked=2, demos_completed=1, date=date(2026, 7, 1),
        )

    good, bad = m(600.0, 3), m(4000.0, 5)  # bad: CPQL $800 >= $600 red line
    ds = {"metrics": [good, bad], "leads": [], "today": date(2026, 7, 9),
          "vendor_metrics": {"Good Vendor": [good], "Bad Vendor": [bad]}}
    r = reports.build("daily_summary", ds, user_id="u1")
    s = r["structured"]
    assert [v["vendor"] for v in s["red_flag_vendors"]] == ["Bad Vendor"]
    assert "qualified lead" in s["red_flag_vendors"][0]["reasons"][0]
    assert len(s["vendors"]) == 2
    rows = {i["vendor"]: i for i in s["vendor_insights"]}
    assert set(rows) == {"Good Vendor", "Bad Vendor"}
    assert all(len(i["insights"]) == 3 and len(i["actions"]) == 3 for i in rows.values())
    assert "Bad Vendor" in r["markdown"]  # exec summary names the flagged vendor


def test_daily_movement_has_a_real_prompt():
    """Without a prompt file the LLM free-styles a 3000-word report; the daily
    brief must ship with explicit brevity instructions."""
    from marketing_research_agent import analysis

    prompt = analysis.load_prompt("daily_movement")
    assert prompt != "{data}"
    assert "Recommend:" in prompt and "{data}" in prompt


# Every kind whose narrative is rendered by the report doc's <Prose>, which turns
# a lead line + "- " bullets into readable blocks. The desk called these reports
# unreadable while the prompts were ordering "NO bullet points" — guard the
# instruction against drifting away from the renderer again.
NARRATIVE_KINDS = [
    "daily_summary", "weekly_summary", "monthly_summary",
    "quarterly_summary", "threshold_alert", "daily_movement",
]


@pytest.mark.parametrize("kind", NARRATIVE_KINDS)
def test_narrative_prompt_asks_for_the_shape_the_doc_renders(kind):
    from marketing_research_agent import analysis

    prompt = analysis.load_prompt(kind)
    low = prompt.lower()
    assert "{data}" in prompt, f"{kind}: prompt lost its data slot"
    assert "recommend:" in low, f"{kind}: no Recommend line"
    assert "bullet" in low, f"{kind}: does not ask for bullets"
    assert "\n- " in prompt, f"{kind}: does not show the '- ' marker"
    assert "no markdown" not in low, f"{kind}: blanket markdown ban contradicts bullets"
    assert "no bullet" not in low, f"{kind}: still forbids the bullets the doc renders"
