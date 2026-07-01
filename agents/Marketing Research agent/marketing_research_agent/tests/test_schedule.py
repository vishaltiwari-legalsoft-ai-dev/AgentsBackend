from datetime import date

from marketing_research_agent import schedule
from marketing_research_agent.schemas import CampaignMetric


def _ds(spend, booked):
    m = CampaignMetric(
        channel="Google", campaign="c", utm_source="g", utm_medium="cpc",
        utm_campaign="c", spend=spend, leads=10, qualified_leads=5,
        demos_booked=booked, demos_completed=0, date=date(2026, 6, 29),
    )
    return {"metrics": [m], "today": date(2026, 6, 30)}


def test_run_daily_returns_report(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    r = schedule.run_daily(_ds(1000, 4), user_id="u1")
    assert r["kind"] == "daily_summary"


def test_check_alerts_fires_on_red_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    assert schedule.check_alerts(_ds(3500, 0), user_id="u1") is not None  # spend, no demo
    assert schedule.check_alerts(_ds(100, 5), user_id="u1") is None
