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
