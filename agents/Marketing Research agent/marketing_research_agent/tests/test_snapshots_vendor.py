from marketing_research_agent import snapshots


def _snap(d, spend):
    return {"vendor": "Meta 360 RA", "vendor_slug": "meta-360-ra", "gid": 1,
            "date": d, "month": d[:7], "captured_at": d + "T18:00:00+00:00",
            "raw": {"team_overall": [], "channels": {}},
            "canonical": {"team_overall": {"spend": {"performance": spend, "investment": None}},
                          "channels": {}},
            "prev_month_raw": {"team_overall": [], "channels": {}}}


def test_vendor_detail_latest_default(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("2026-02-06", 200.0))
    snapshots.save_snapshot(_snap("2026-02-07", 300.0))
    out = snapshots.vendor_detail("meta-360-ra")
    assert out["dates"] == ["2026-02-06", "2026-02-07"]
    assert out["snapshot"]["date"] == "2026-02-07"
    assert out["delta"]["blocks"]["team_overall"]["additive"]["spend.performance"]["delta"] == 100.0


def test_vendor_detail_specific_date(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("2026-02-06", 200.0))
    snapshots.save_snapshot(_snap("2026-02-07", 300.0))
    out = snapshots.vendor_detail("meta-360-ra", "2026-02-06")
    assert out["snapshot"]["date"] == "2026-02-06"
    assert out["delta"]["month_start"] is True  # no prior capture that month


def test_vendor_detail_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    assert snapshots.vendor_detail("nope") is None
    snapshots.save_snapshot(_snap("2026-02-07", 300.0))
    assert snapshots.vendor_detail("meta-360-ra", "2026-01-01") is None
