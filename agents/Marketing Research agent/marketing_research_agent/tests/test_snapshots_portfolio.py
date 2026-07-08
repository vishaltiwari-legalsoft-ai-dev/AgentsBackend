from marketing_research_agent import snapshots


def _snap(slug, d="2026-07-09", budget=10000.0, spend=4000.0, leads=10, q=2,
          qdb=8, comp=3, sold=1, vendor=None):
    return {"vendor": vendor or slug, "vendor_slug": slug, "gid": 1, "date": d,
            "month": d[:7], "captured_at": d + "T18:00:00+00:00",
            "raw": {"team_overall": [], "channels": {}},
            "canonical": {"team_overall": {
                "budget": {"performance": budget, "investment": None},
                "spend": {"performance": spend, "investment": spend + 999},
                "leads": {"total": leads, "qualified": q},
                "demos": {"qualified_booked_all": qdb, "total_booked_all": qdb,
                          "completed_all": comp},
                "actualized_revenue": {"services_sold": sold},
            }, "channels": {}},
            "prev_month_raw": {"team_overall": [], "channels": {}}}


def test_portfolio_sums_vendors_excludes_overall(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("meta-360-ra"))
    snapshots.save_snapshot(_snap("hawksem-ls-google", spend=2000.0, q=0, qdb=2, comp=1, sold=0))
    snapshots.save_snapshot(_snap("marketing-2026-overall-report", spend=99999.0))
    p = snapshots.portfolio()
    assert p["vendors"] == 2
    assert p["total_budget"] == 20000.0
    assert p["total_spend"] == 6000.0            # performance basis, overall excluded
    assert p["budget_utilized_pct"] == 30.0
    assert p["qualified_leads"] == 2
    assert p["cost_per_qualified_lead"] == 3000.0
    assert p["qual_demos_booked"] == 10
    assert p["cost_per_qual_demo_booked"] == 600.0
    assert p["demos_completed"] == 4
    assert p["show_rate_pct"] == 40.0
    assert p["services_sold"] == 1
    assert p["pacing"]["day"] == 9 and p["pacing"]["days_in_month"] == 31
    assert p["benchmarks"]["cpqdb_max"] == 500


def test_portfolio_latest_snapshot_per_vendor(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("meta-360-ra", d="2026-07-08", spend=1000.0))
    snapshots.save_snapshot(_snap("meta-360-ra", d="2026-07-09", spend=4000.0))
    p = snapshots.portfolio()
    assert p["total_spend"] == 4000.0 and p["date"] == "2026-07-09"


def test_portfolio_null_guards(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("ghost", budget=0.0, spend=0.0, leads=0, q=0, qdb=0, comp=0, sold=0))
    p = snapshots.portfolio()
    assert p["budget_utilized_pct"] is None
    assert p["cost_per_qualified_lead"] is None
    assert p["show_rate_pct"] is None


def test_portfolio_none_when_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    assert snapshots.portfolio() is None
