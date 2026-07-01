from datetime import date

from marketing_research_agent.schemas import CampaignMetric, DateRange, Flag


def _metric(**kw):
    base = dict(
        channel="Google", campaign="c1", utm_source="google", utm_medium="cpc",
        utm_campaign="brand", spend=1000.0, leads=10, qualified_leads=8,
        demos_booked=4, demos_completed=2, date=date(2026, 6, 30),
    )
    base.update(kw)
    return CampaignMetric(**base)


def test_cost_metrics_compute():
    m = _metric()
    assert m.cpl == 100.0
    assert m.cost_per_qualified_lead == 125.0
    assert m.cost_per_demo_booked == 250.0
    assert m.cost_per_demo_completed == 500.0
    assert m.cac == 500.0


def test_safe_division_returns_none():
    m = _metric(leads=0, qualified_leads=0, demos_booked=0, demos_completed=0)
    assert m.cpl is None
    assert m.cost_per_demo_completed is None
    assert m.cac is None


def test_daterange_and_flag():
    r = DateRange(start=date(2026, 6, 1), end=date(2026, 6, 30))
    assert r.start < r.end
    f = Flag(level="red", message="x", metric="cac")
    assert f.level == "red" and f.metric == "cac"
