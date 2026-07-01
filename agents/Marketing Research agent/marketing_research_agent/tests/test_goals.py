from datetime import date

from marketing_research_agent import goals
from marketing_research_agent.schemas import CampaignMetric


def _m(**kw):
    base = dict(
        channel="Google", campaign="c", utm_source="g", utm_medium="cpc",
        utm_campaign="b", spend=4000.0, leads=10, qualified_leads=5,
        demos_booked=0, demos_completed=0, date=date(2026, 6, 30),
    )
    base.update(kw)
    return CampaignMetric(**base)


def test_channel_goal_lookup_case_insensitive():
    g = goals.channel_goal("google")
    assert g is not None and g.cpd_booked_low == 550 and g.cpd_booked_high == 750


def test_spend_with_no_demo_is_red():
    flags = goals.evaluate(_m(spend=3500.0, demos_booked=0))
    assert any(f.level == "red" and f.metric == "spend_no_demo" for f in flags)


def test_cost_per_booking_over_threshold_flags():
    flags = goals.evaluate(_m(spend=900.0, demos_booked=3))  # cost/booking = 300 > 150
    assert any(f.metric == "cost_per_booking" for f in flags)


def test_cpql_red_flag():
    flags = goals.evaluate(_m(spend=4000.0, qualified_leads=5))  # cpql = 800 >= 600
    assert any(f.level == "red" and f.metric == "cost_per_qualified_lead" for f in flags)


def test_conversion_drop_flag():
    m = _m(spend=100.0, leads=10, demos_booked=1)  # current conv = 0.1
    flags = goals.evaluate(m, prior_conversion=0.20)  # 50% drop
    assert any(f.metric == "conversion_drop" for f in flags)
