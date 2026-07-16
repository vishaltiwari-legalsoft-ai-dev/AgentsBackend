from datetime import date

from marketing_research_agent import trends
from marketing_research_agent.schemas import CampaignMetric


def _m(month, channel="Google", spend=1000.0, leads=10, q=5, b=4, c=2, day=1):
    return CampaignMetric(
        channel=channel, campaign="c", utm_source="g", utm_medium="paid",
        utm_campaign="v", spend=spend, leads=leads, qualified_leads=q,
        demos_booked=b, demos_completed=c, date=date(2026, month, day),
    )


TODAY = date(2026, 7, 8)


def test_empty():
    out = trends.build([], today=TODAY)
    assert out["has_data"] is False and out["insights"] == []


def test_monthly_rollup_excludes_future_months():
    vd = [{"vendor": "A", "metrics": [_m(6), _m(7), _m(9, spend=2200.0, leads=0, q=0, b=0, c=0)]}]
    out = trends.build(vd, today=TODAY)
    assert [r["month"] for r in out["monthly"]] == ["2026-06", "2026-07"]
    assert out["month"] == "2026-07"
    assert out["monthly"][0]["spend"] == 1000.0
    assert out["monthly"][0]["cpql"] == 200.0  # 1000 / 5


def test_channels_and_vendor_series():
    vd = [
        {"vendor": "A", "metrics": [_m(6, channel="Google"), _m(7, channel="Google", spend=500.0)]},
        {"vendor": "B", "metrics": [_m(7, channel="META", spend=300.0, q=3)]},
    ]
    out = trends.build(vd, today=TODAY)
    assert [r["spend"] for r in out["channels"]["Google"]] == [1000.0, 500.0]
    a = next(v for v in out["vendors"] if v["vendor"] == "A")
    assert a["spend_mtd"] == 500.0 and a["cpql"] == 100.0
    assert [s["spend"] for s in a["spend_series"]] == [1000.0, 500.0]


def test_insight_pace_vs_last_month():
    # June actual 3100; July MTD 400 by day 8 -> proj 1550 -> -50%
    vd = [{"vendor": "A", "metrics": [_m(6, spend=3100.0), _m(7, spend=400.0)]}]
    out = trends.build(vd, today=TODAY)
    pace = next(i for i in out["insights"] if "pace" in i["text"].lower())
    assert pace["level"] == "warn" and "-50%" in pace["text"].replace("−", "-")


def test_insight_best_and_worst_cpql():
    vd = [
        {"vendor": "Good Co", "metrics": [_m(7, spend=500.0, q=5)]},   # cpql 100
        {"vendor": "Pricey Co", "metrics": [_m(7, spend=900.0, q=3)]}, # cpql 300 (3x)
    ]
    out = trends.build(vd, today=TODAY)
    texts = [i["text"] for i in out["insights"]]
    assert any("Good Co" in t and "$100" in t for t in texts)
    assert any("Pricey Co" in t for t in texts)


def test_no_zero_lead_spender_banner():
    """The desk asked for this banner off the Overview (7/9 review). Vendors that
    spend with no leads are still named as a red flag inside the reports."""
    vd = [{"vendor": "Ghost", "metrics": [_m(7, spend=4600.0, leads=0, q=0)]}]
    out = trends.build(vd, today=TODAY)
    assert not any("zero leads" in i["text"].lower() for i in out["insights"])


def test_every_insight_declares_its_kind():
    """The board routes insights by kind — pace to the hero, efficiency to the
    vendor chart. It used to substring-match, and the pace sentence contains the
    words "qualified leads", so it matched BOTH and rendered twice."""
    vd = [
        {"vendor": "Good Co", "metrics": [_m(6, spend=1000.0, q=5), _m(7, spend=500.0, q=5)]},
        {"vendor": "Pricey Co", "metrics": [_m(6, spend=900.0, q=3), _m(7, spend=900.0, q=3)]},
    ]
    out = trends.build(vd, today=TODAY)
    assert out["insights"], "expected insights for this fixture"
    assert all(i.get("kind") in {"pace", "efficiency", "mover"} for i in out["insights"])
    # The pace sentence must be pace and nothing else.
    paces = [i for i in out["insights"] if i["kind"] == "pace"]
    assert all("pace" in i["text"].lower() for i in paces)


def test_insight_kinds_are_unique_per_role():
    """No insight may carry two roles — that is what produced the duplicate."""
    vd = [{"vendor": "A", "metrics": [_m(6, spend=1000.0), _m(7, spend=1500.0)]}]
    out = trends.build(vd, today=TODAY)
    kinds = [i["kind"] for i in out["insights"]]
    assert len(kinds) == len(out["insights"])


def test_insight_mom_mover():
    # May->June spend +50%
    vd = [{"vendor": "A", "metrics": [_m(5, spend=1000.0), _m(6, spend=1500.0), _m(7, spend=100.0)]}]
    out = trends.build(vd, today=TODAY)
    assert any("+50%" in i["text"] and "June" in i["text"] for i in out["insights"])
