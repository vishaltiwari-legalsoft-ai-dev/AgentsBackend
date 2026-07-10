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


def test_targets_are_editable_and_change_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    assert goals.get_targets()["edited"] is False

    # CPQL 400 is under the default $600 red line…
    m = _m(spend=2000.0, qualified_leads=5, demos_booked=2)
    assert not any(f.metric == "cost_per_qualified_lead" for f in goals.evaluate(m))

    # …but over an edited $300 red line.
    t = goals.set_targets({"thresholds": {"cost_per_qualified_lead_red": 300}})
    assert t["edited"] is True and t["thresholds"]["cost_per_qualified_lead_red"] == 300
    assert any(f.metric == "cost_per_qualified_lead" for f in goals.evaluate(m))

    goals.reset_targets()
    assert goals.get_targets()["edited"] is False


def test_channel_goals_are_editable(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    goals.set_targets({"channel_goals": {"Google": {"cpd_booked_high": 900}}})
    g = goals.channel_goal("google")
    assert g.cpd_booked_high == 900 and g.cpd_booked_low == 550  # untouched default


def test_set_targets_rejects_unknown_keys(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    with pytest.raises(ValueError):
        goals.set_targets({"thresholds": {"nope": 1}})
    with pytest.raises(ValueError):
        goals.set_targets({"thresholds": {"cac_red": "high"}})
