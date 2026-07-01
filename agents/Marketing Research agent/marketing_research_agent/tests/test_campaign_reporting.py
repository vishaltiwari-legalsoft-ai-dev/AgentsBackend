from datetime import date

from marketing_research_agent.modules import campaign_reporting as cr
from marketing_research_agent.schemas import CampaignMetric


def _m(channel, spend, booked, src="google", completed=0, leads=10, qualified=8, campaign="c"):
    return CampaignMetric(
        channel=channel, campaign=campaign, utm_source=src, utm_medium="cpc",
        utm_campaign=campaign, spend=spend, leads=leads, qualified_leads=qualified,
        demos_booked=booked, demos_completed=completed, date=date(2026, 6, 29),
    )


def test_aggregate_by_channel_sums_and_costs():
    agg = cr.aggregate_by_channel([_m("Google", 1000, 4), _m("Google", 500, 1)])
    g = agg["Google"]
    assert g["spend"] == 1500.0 and g["demos_booked"] == 5
    assert g["cost_per_demo_booked"] == 300.0


def test_top_utm_sources_sorted():
    top = cr.top_utm_sources([_m("Google", 100, 1, src="a"), _m("Google", 100, 5, src="b")])
    assert top[0]["utm_source"] == "b"


def test_week_over_week_delta():
    cur = cr.aggregate_by_channel([_m("Google", 1000, 6)])
    prev = cr.aggregate_by_channel([_m("Google", 1000, 4)])
    wow = cr.week_over_week(cur, prev)
    assert wow["Google"]["demos_booked_delta"] == 2


def test_flag_all_collects_flags():
    flags = cr.flag_all([_m("Google", 3500, 0)])  # spend, no demo -> red
    assert any(f.metric == "spend_no_demo" for f in flags)
