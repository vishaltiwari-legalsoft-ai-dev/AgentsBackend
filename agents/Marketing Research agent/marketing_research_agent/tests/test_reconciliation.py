"""Reconciliation guards (2026-07-21): the platform's blended totals must match
the tracker sheet's own convention — media spend only in the blended figures,
and a rollup tab is never counted as a vendor (stale runs included)."""

from datetime import date

from marketing_research_agent import reports, snapshots, trends
from marketing_research_agent.schemas import CampaignMetric
from marketing_research_agent.sources.sheets_source import is_rollup_platform


def _m(channel, spend, leads=10, q=5, b=4, c=2, month=7):
    return CampaignMetric(
        channel=channel, campaign="c", utm_source="s", utm_medium="paid",
        utm_campaign="v", spend=spend, leads=leads, qualified_leads=q,
        demos_booked=b, demos_completed=c, date=date(2026, month, 1),
    )


def test_overview_totals_exclude_website_spend_keep_leads():
    ds = {"metrics": [_m("Google", 1000.0), _m("Websites", 700.0, leads=20, q=8)],
          "today": date(2026, 7, 8), "sources": []}
    out = reports.overview(ds)
    assert out["totals"]["spend"] == 1000.0
    assert out["totals"]["leads"] == 30
    assert out["channels"]["Websites"]["spend"] == 700.0


def test_trends_monthly_spend_excludes_website_channel():
    vd = [{"vendor": "A", "metrics": [_m("Google", 1000.0)]},
          {"vendor": "Website", "metrics": [_m("Websites", 700.0, leads=20)]}]
    out = trends.build(vd, today=date(2026, 7, 8))
    assert out["monthly"][0]["spend"] == 1000.0
    assert out["monthly"][0]["leads"] == 30
    assert [r["spend"] for r in out["channels"]["Websites"]] == [700.0]
    web = next(v for v in out["vendors"] if v["vendor"] == "Website")
    assert web["spend_mtd"] == 700.0


def test_is_rollup_platform():
    assert is_rollup_platform("sheets:Marketing 2026 Overall Report")
    assert not is_rollup_platform("sheets:Meta 360 RA")
    assert not is_rollup_platform("pdf:July report")


def _snap(slug, vendor, d="2026-07-07", spend=100.0):
    return {"vendor": vendor, "vendor_slug": slug, "gid": 1,
            "date": d, "month": d[:7], "captured_at": d + "T18:00:00+00:00",
            "raw": {"team_overall": [], "channels": {}},
            "canonical": {"team_overall": {
                "spend": {"performance": spend, "investment": None},
                "leads": {"total": 5, "qualified": 2},
                "cost_metrics": {"cost_per_lead_performance": None},
            }, "channels": {}},
            "prev_month_raw": {"team_overall": [], "channels": {}}}


def test_deltas_and_listing_skip_rollup_snapshots(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("meta-360-ra", "Meta 360 RA"))
    snapshots.save_snapshot(_snap("marketing-2026-overall-report", "Marketing 2026 Overall Report"))
    assert [d["vendor_slug"] for d in snapshots.deltas_for()] == ["meta-360-ra"]
    assert all("overall" not in s["vendor_slug"] for s in snapshots.list_snapshots())


def test_portfolio_excludes_website_spend_keeps_its_leads(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("meta-360-ra", "Meta 360 RA", spend=1000.0))
    snapshots.save_snapshot(_snap("website", "Website", spend=700.0))
    p = snapshots.portfolio()
    assert p["total_spend"] == 1000.0
    assert p["vendors"] == 2
    assert p["leads"] == 10
