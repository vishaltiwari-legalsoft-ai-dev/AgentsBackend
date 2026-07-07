from marketing_research_agent import snapshots


def _snap(d, spend, leads, qualified=1, cpl=None):
    return {"vendor": "Meta 360 RA", "vendor_slug": "meta-360-ra", "gid": 1,
            "date": d, "month": d[:7], "captured_at": d + "T18:00:00+00:00",
            "raw": {"team_overall": [], "channels": {}},
            "canonical": {"team_overall": {
                "spend": {"performance": spend, "investment": None},
                "leads": {"total": leads, "qualified": qualified},
                "cost_metrics": {"cost_per_lead_performance": cpl},
            }, "channels": {}},
            "prev_month_raw": {"team_overall": [], "channels": {}}}


def test_simple_day_delta():
    d = snapshots.compute_delta(_snap("2026-02-07", 300.0, 5), _snap("2026-02-06", 200.0, 3))
    t = d["blocks"]["team_overall"]
    assert t["additive"]["spend.performance"]["delta"] == 100.0
    assert t["additive"]["leads.total"]["delta"] == 2
    assert d["days"] == 1 and d["month_start"] is False and d["corrected"] is False


def test_month_start_delta_is_mtd():
    d = snapshots.compute_delta(_snap("2026-02-01", 50.0, 1), None)
    assert d["month_start"] is True
    assert d["blocks"]["team_overall"]["additive"]["spend.performance"]["delta"] == 50.0


def test_gap_days_span():
    d = snapshots.compute_delta(_snap("2026-02-07", 300.0, 5), _snap("2026-02-04", 200.0, 3))
    assert d["days"] == 3 and d["since"] == "2026-02-04"


def test_correction_flagged_not_clamped():
    d = snapshots.compute_delta(_snap("2026-02-07", 300.0, 4), _snap("2026-02-06", 200.0, 5))
    lt = d["blocks"]["team_overall"]["additive"]["leads.total"]
    assert lt["delta"] == -1 and lt["corrected"] is True
    assert d["corrected"] is True


def test_rates_recomputed_from_day_components():
    d = snapshots.compute_delta(_snap("2026-02-07", 300.0, 5, cpl=60.0),
                                _snap("2026-02-06", 200.0, 3, cpl=66.7))
    r = d["blocks"]["team_overall"]["rates"]["cost_metrics.cost_per_lead_performance"]
    assert r["mode"] == "recomputed"
    assert r["value"] == 50.0  # day spend 100 / day leads 2


def test_rates_div_zero_null():
    d = snapshots.compute_delta(_snap("2026-02-07", 300.0, 3, cpl=100.0),
                                _snap("2026-02-06", 200.0, 3, cpl=66.7))
    r = d["blocks"]["team_overall"]["rates"]["cost_metrics.cost_per_lead_performance"]
    assert r["value"] is None  # 0 day-leads


def test_deltas_for_latest_date(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("2026-02-06", 200.0, 3))
    snapshots.save_snapshot(_snap("2026-02-07", 300.0, 5))
    out = snapshots.deltas_for()
    assert len(out) == 1
    assert out[0]["date"] == "2026-02-07"
    assert out[0]["blocks"]["team_overall"]["additive"]["spend.performance"]["delta"] == 100.0
