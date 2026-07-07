from datetime import date

from marketing_research_agent import reports
from marketing_research_agent.schemas import CampaignMetric


def _metric(month: int, channel: str = "Google", spend: float = 1200.0) -> CampaignMetric:
    return CampaignMetric(
        channel=channel, campaign="c", utm_source="g", utm_medium="cpc",
        utm_campaign="c", spend=spend, leads=12, qualified_leads=9,
        demos_booked=4, demos_completed=2, date=date(2026, month, 15),
    )


def test_overview_empty_dataset():
    out = reports.overview({"metrics": [], "leads": []})
    assert out["has_data"] is False
    assert out["month"] is None
    assert out["totals"] is None
    assert out["channels"] == {}


def test_overview_uses_latest_month_only():
    ds = {"metrics": [_metric(5, spend=999.0), _metric(6)], "leads": []}
    out = reports.overview(ds)
    assert out["has_data"] is True
    assert out["month"] == "2026-06"
    assert out["totals"]["spend"] == 1200.0  # May's 999 excluded


def test_overview_channels_carry_goal_and_status():
    out = reports.overview({"metrics": [_metric(6)], "leads": []})
    ch = out["channels"]["Google"]
    assert "goal" in ch and "status" in ch
    assert isinstance(out["flag_summary"], list)


def test_overview_passes_sources_through():
    src = [{"platform": "sheets:123", "generated_at": "2026-07-07T00:00:00+00:00"}]
    out = reports.overview({"metrics": [_metric(6)], "leads": [], "sources": src})
    assert out["sources"] == src


def test_overview_persists_no_run(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    reports.overview({"metrics": [_metric(6)], "leads": []})
    assert list(tmp_path.glob("*.json")) == []
