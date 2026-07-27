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


def test_portfolio_overall_rollup_is_the_official_bar(monkeypatch, tmp_path):
    """Decision 2026-07-27: the Overall tab aggregates sources with no vendor
    tab, so when its snapshot exists ITS figures ARE the summary bar; the
    vendor sum stays as the audit trail and the roll-up is never a vendor."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("meta-360-ra"))
    snapshots.save_snapshot(_snap("hawksem-ls-google", spend=2000.0, q=0, qdb=2, comp=1, sold=0))
    snapshots.save_snapshot(_snap("marketing-2026-overall-report", spend=99999.0,
                                  budget=120000.0, leads=50, q=20, qdb=30, comp=15, sold=5))
    p = snapshots.portfolio()
    assert p["vendors"] == 2                       # the roll-up is not a vendor
    assert p["source"] == "sheet_overall"
    assert p["total_spend"] == 99999.0 and p["total_budget"] == 120000.0
    assert p["qualified_leads"] == 20
    assert p["qual_demos_booked"] == 30 and p["demos_completed"] == 15
    assert p["cost_per_qual_demo_booked"] == round(99999.0 / 30, 2)
    assert p["show_rate_pct"] == 50.0
    assert p["services_sold"] == 5
    assert p["computed_spend"] == 6000.0 and p["computed_budget"] == 20000.0
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
