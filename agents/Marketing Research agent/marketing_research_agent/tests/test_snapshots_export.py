from marketing_research_agent import snapshots


def _snap(d, spend):
    return {"vendor": "Meta 360 RA", "vendor_slug": "meta-360-ra", "gid": 1,
            "date": d, "month": d[:7], "captured_at": d + "T18:00:00+00:00",
            "raw": {"team_overall": [], "channels": {}},
            "canonical": {"team_overall": {
                "budget": {"performance": 10000.0, "investment": 12200.0},
                "spend": {"performance": spend, "investment": None},
                "kpis": {"revenue_sold_goal": 185000.0},
            }, "channels": {"google": {"spend": {"performance": 1.0, "investment": None}}}},
            "prev_month_raw": {"team_overall": [], "channels": {}}}


def test_month_export_user_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("2026-02-06", 200.0))
    snapshots.save_snapshot(_snap("2026-02-07", 300.0))
    doc = snapshots.month_export("meta-360-ra", "2026-02")
    assert doc["metadata"]["schema_version"] == "1.0.0"
    assert doc["metadata"]["vendor"] == "Meta 360 RA"
    assert doc["metadata"]["last_updated"] == "2026-02-07"
    m = doc["months"]["2026-02"]
    assert m["targets"]["budget_performance"] == 10000.0
    assert m["targets"]["budget_investment"] == 12200.0
    assert m["targets"]["revenue_sold_goal"] == 185000.0
    assert set(m["daily_snapshots"].keys()) == {"2026-02-06", "2026-02-07"}
    day = m["daily_snapshots"]["2026-02-07"]
    assert day["team_overall"]["spend"]["performance"] == 300.0
    assert day["channels"]["google"]["spend"]["performance"] == 1.0


def test_month_export_none_when_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    assert snapshots.month_export("meta-360-ra", "2026-02") is None


def test_export_all_offline_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap("2026-02-07", 300.0))
    from datetime import date
    assert snapshots.export_all_to_gcs(date(2026, 2, 7)) == []
