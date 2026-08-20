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
    out = reports.overview(ds, "u1")
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


def test_overview_headline_spend_is_the_sheet_official_total():
    """Decision 2026-07-27: the console headline must equal the Overall tab's
    Spend cell (Performance, all-in) — with the vendor-tab sum kept alongside
    as an auditable reconciliation, never silently different."""
    ds = {"metrics": [_m("Google", 1000.0), _m("Websites", 700.0, leads=20, q=8)],
          "official_spend": {"2026-07": 45461.20},
          "today": date(2026, 7, 8), "sources": []}
    t = reports.overview(ds, "u1")["totals"]
    assert t["spend"] == 45461.20
    assert t["spend_source"] == "sheet_overall"
    assert t["spend_computed"] == 1000.0
    assert t["spend_delta"] == round(45461.20 - 1000.0, 2)
    # derived costs follow the official figure, matching the sheet's own math
    assert t["cost_per_demo_booked"] == round(45461.20 / 8, 2)
    assert t["cost_per_demo_completed"] == round(45461.20 / 4, 2)


def test_overview_falls_back_to_computed_when_official_month_missing():
    ds = {"metrics": [_m("Google", 1000.0)],
          "official_spend": {"2026-06": 99.0},
          "today": date(2026, 7, 8), "sources": []}
    t = reports.overview(ds, "u1")["totals"]
    assert t["spend"] == 1000.0
    assert "spend_source" not in t


def test_overview_official_totals_override_every_team_kpi():
    ds = {"metrics": [_m("Google", 1000.0)],
          "official_totals": {"2026-07": {
              "spend": 47166.58, "leads": 331, "qualified_leads": 137,
              "demos_booked": 137, "demos_completed": 74,
          }},
          "today": date(2026, 7, 27), "sources": []}
    t = reports.overview(ds, "u1")["totals"]
    assert t["spend"] == 47166.58 and t["spend_source"] == "sheet_overall"
    assert t["leads"] == 331 and t["qualified_leads"] == 137
    assert t["demos_booked"] == 137 and t["demos_completed"] == 74
    # derived costs equal the sheet's own math by construction
    assert t["cost_per_demo_booked"] == round(47166.58 / 137, 2)
    assert t["cost_per_demo_completed"] == round(47166.58 / 74, 2)
    # the vendor-tab sums stay alongside as the audit trail
    assert t["demos_booked_computed"] == 4 and t["leads_computed"] == 10


def test_portfolio_prefers_the_overall_rollup_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("meta-360-ra", "Meta 360 RA", spend=1000.0))
    roll = _snap("marketing-2026-overall-report", "Marketing 2026 Overall Report", spend=47166.58)
    roll["canonical"]["team_overall"]["budget"] = {"performance": 78100.2, "investment": None}
    roll["canonical"]["team_overall"]["leads"] = {"total": 331, "qualified": 137}
    roll["canonical"]["team_overall"]["demos"] = {"qualified_booked_all": 115, "completed_all": 74}
    roll["canonical"]["team_overall"]["actualized_revenue"] = {"services_sold": 12}
    snapshots.save_snapshot(roll)
    p = snapshots.portfolio()
    assert p["source"] == "sheet_overall"
    assert p["total_spend"] == 47166.58 and p["total_budget"] == 78100.2
    assert p["leads"] == 331 and p["qualified_leads"] == 137
    assert p["qual_demos_booked"] == 115 and p["demos_completed"] == 74
    assert p["services_sold"] == 12
    assert p["show_rate_pct"] == round(74 * 100 / 115, 2)
    assert p["computed_spend"] == 1000.0   # vendor-sum audit trail
    assert p["vendors"] == 1               # the roll-up is not a vendor


def test_portfolio_falls_back_to_vendor_sum_without_rollup(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("meta-360-ra", "Meta 360 RA", spend=1000.0))
    p = snapshots.portfolio()
    assert p["source"] == "vendor_sum" and p["total_spend"] == 1000.0


def test_trends_monthly_spend_uses_sheet_official_total():
    vd = [{"vendor": "A", "metrics": [_m("Google", 1000.0)]}]
    out = trends.build(vd, today=date(2026, 7, 8),
                       official_spend={"2026-07": 45461.20})
    row = out["monthly"][0]
    assert row["spend"] == 45461.20
    assert row["spend_computed"] == 1000.0
    assert row["cpql"] == round(45461.20 / 5, 2)


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
