from datetime import date

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
