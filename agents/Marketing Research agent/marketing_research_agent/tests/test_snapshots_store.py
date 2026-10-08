from datetime import date

from marketing_research_agent import snapshots
from marketing_research_agent.workbook import TabGrid


def _snap(slug="meta-360-ra", d="2026-02-07", spend=100.0):
    return {"vendor": "Meta 360 RA", "vendor_slug": slug, "gid": 1, "date": d,
            "month": d[:7], "captured_at": "2026-02-07T18:00:00+00:00",
            "raw": {"team_overall": [], "channels": {}},
            "canonical": {"team_overall": {"spend": {"performance": spend, "investment": None}},
                          "channels": {}},
            "prev_month_raw": {"team_overall": [], "channels": {}}}


def test_save_and_get_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap())
    got = snapshots.get_snapshot("meta-360-ra", "2026-02-07")
    assert got["canonical"]["team_overall"]["spend"]["performance"] == 100.0


def test_same_day_overwrite_last_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap(spend=100.0))
    snapshots.save_snapshot(_snap(spend=250.0))
    assert len(snapshots.list_snapshots(slug="meta-360-ra")) == 1
    assert snapshots.get_snapshot("meta-360-ra", "2026-02-07")["canonical"]["team_overall"]["spend"]["performance"] == 250.0


def test_list_filters_by_month(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap(d="2026-01-31"))
    snapshots.save_snapshot(_snap(d="2026-02-06"))
    snapshots.save_snapshot(_snap(d="2026-02-07"))
    feb = snapshots.list_snapshots(slug="meta-360-ra", month="2026-02")
    assert [s["date"] for s in feb] == ["2026-02-06", "2026-02-07"]


def test_use_cloud_respects_app_settings(monkeypatch):
    """Cloud Run doesn't set GOOGLE_CLOUD_PROJECT/GCP_PROJECT — the gate must key
    off the same config firestore_repo uses (settings.gcp_project_id)."""
    from app.config import settings

    monkeypatch.delenv("MR_OFFLINE", raising=False)
    monkeypatch.setattr(settings, "gcp_project_id", "some-project")
    assert snapshots._use_cloud() is True
    monkeypatch.setattr(settings, "gcp_project_id", "")
    assert snapshots._use_cloud() is False
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setattr(settings, "gcp_project_id", "some-project")
    assert snapshots._use_cloud() is False


def test_get_snapshot_falls_back_to_cloud(monkeypatch, tmp_path):
    """Cloud Run has ephemeral disk — a doc missing locally must be readable
    from Firestore."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    monkeypatch.setattr(snapshots, "_use_cloud", lambda: True)
    monkeypatch.setattr(
        snapshots, "_cloud_get",
        lambda doc_id: _snap() if doc_id == "meta-360-ra_2026-02-07" else None)
    got = snapshots.get_snapshot("meta-360-ra", "2026-02-07")
    assert got["vendor_slug"] == "meta-360-ra"
    assert snapshots.get_snapshot("meta-360-ra", "2026-02-01") is None


def test_list_snapshots_merges_cloud_disk_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    snapshots.save_snapshot(_snap(d="2026-02-07", spend=250.0))  # disk copy
    monkeypatch.setattr(snapshots, "_use_cloud", lambda: True)
    monkeypatch.setattr(snapshots, "_cloud_list", lambda *a, **kw: [
        _snap(d="2026-02-06", spend=100.0),          # cloud-only day
        _snap(d="2026-02-07", spend=999.0),          # stale cloud copy of a disk day
    ])
    out = snapshots.list_snapshots(slug="meta-360-ra")
    assert [s["date"] for s in out] == ["2026-02-06", "2026-02-07"]
    # same-day conflict: the local (freshest) copy wins
    assert out[1]["canonical"]["team_overall"]["spend"]["performance"] == 250.0


def test_capture_workbook_filters_and_reports(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    tracker_rows = [
        ["Meta 360 RA", "Feb (Performance)", "Feb (Investment)"],
        ["Spend", "$10.00", "$20.00"],
        ["Leads", "1", ""],
    ]
    grids = [
        TabGrid(title="Meta 360 RA", gid=1, hidden=False, rows=tracker_rows, n_rows=3, n_cols=3),
        TabGrid(title="All Contacts", gid=2, hidden=False, rows=[["Record ID"], ["9"]], n_rows=2, n_cols=1),
    ]
    results = snapshots.capture_workbook(grids, year=2026, today=date(2026, 2, 7))
    assert {r["tab"]: r.get("captured", r.get("skipped")) for r in results} == {
        "Meta 360 RA": True, "All Contacts": True}
    assert snapshots.get_snapshot("meta-360-ra", "2026-02-07") is not None
    assert snapshots.get_snapshot("all-contacts", "2026-02-07") is None


def _tracker(title):
    return [[title, "Feb (Performance)", "Feb (Investment)"],
            ["Spend", "$10.00", "$20.00"], ["Leads", "1", ""]]


def test_capture_skips_hidden_tabs_like_the_dataset_path(monkeypatch, tmp_path):
    """sheets_source.fetch_all_trackers skips hidden tabs; capture did not,
    so the two paths disagreed on what a vendor is."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    grids = [
        TabGrid(title="Copy of Meta 360 RA", gid=7, hidden=True,
                rows=_tracker("Copy of Meta 360 RA"), n_rows=3, n_cols=3),
        TabGrid(title="Meta 360 RA", gid=1, hidden=False,
                rows=_tracker("Meta 360 RA"), n_rows=3, n_cols=3),
    ]
    results = snapshots.capture_workbook(grids, year=2026, today=date(2026, 2, 7))
    assert results[0] == {"tab": "Copy of Meta 360 RA", "skipped": True, "reason": "hidden"}
    assert snapshots.get_snapshot("copy-of-meta-360-ra", "2026-02-07") is None
    snap = snapshots.get_snapshot("meta-360-ra", "2026-02-07")
    assert snap["hidden"] is False and snap["gid"] == 1


def test_one_capture_run_stamps_one_sweep_id(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    grids = [TabGrid(title=t, gid=i, hidden=False, rows=_tracker(t), n_rows=3, n_cols=3)
             for i, t in enumerate(["Meta 360 RA", "Hawksem LS Google"])]
    snapshots.capture_workbook(grids, year=2026, today=date(2026, 2, 7))
    first = {s["sweep_id"] for s in snapshots.list_snapshots(month="2026-02")}
    assert len(first) == 1 and None not in first
    snapshots.capture_workbook(grids[:1], year=2026, today=date(2026, 2, 7))
    ids = {s["vendor_slug"]: s["sweep_id"] for s in snapshots.list_snapshots(month="2026-02")}
    assert ids["meta-360-ra"] not in first        # a second run is a new sweep
    sw = snapshots.vendor_sweep("2026-02")
    assert [s["vendor_slug"] for s in sw["docs"]] == ["meta-360-ra"]
    assert sw["excluded"][0]["slug"] == "hawksem-ls-google"   # not in the final run


def test_a_hidden_rollup_still_ends_the_capture(monkeypatch, tmp_path):
    """Everything after the Overall tab is ops sheets — hiding the roll-up
    must not let the capture walk on into them."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    grids = [TabGrid(title="Marketing 2026 Overall Report", gid=1, hidden=True,
                     rows=_tracker("Overall"), n_rows=3, n_cols=3),
             TabGrid(title="Raw Information", gid=2, hidden=False,
                     rows=_tracker("Raw Information"), n_rows=3, n_cols=3)]
    results = snapshots.capture_workbook(grids, year=2026, today=date(2026, 2, 7))
    assert [r["tab"] for r in results] == ["Marketing 2026 Overall Report"]


# --- an unreadable store must never look like an empty one -------------------

def _dead_db(*_a, **_kw):
    raise RuntimeError("503 the datastore is unavailable")


def test_list_snapshots_raises_when_the_cloud_read_fails(monkeypatch, tmp_path):
    """``_cloud_list`` used to swallow its failure and return []. On Cloud Run
    the disk is empty too, so a Firestore outage rendered as "no snapshots for
    this vendor" — a 404/empty board instead of an outage."""
    import pytest

    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    monkeypatch.setattr(snapshots, "_use_cloud", lambda: True)
    monkeypatch.setattr(snapshots, "_cloud_list", lambda *a, **kw: None)
    with pytest.raises(snapshots.SnapshotStoreError):
        snapshots.list_snapshots(slug="meta-360-ra")


def test_cloud_list_reports_none_not_empty_on_failure(monkeypatch):
    from app.services import firestore_repo

    monkeypatch.setattr(firestore_repo, "_db", _dead_db)
    assert snapshots._cloud_list("meta-360-ra") is None


def test_an_empty_cloud_is_still_an_empty_list(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    monkeypatch.setattr(snapshots, "_use_cloud", lambda: True)
    monkeypatch.setattr(snapshots, "_cloud_list", lambda *a, **kw: [])
    assert snapshots.list_snapshots(slug="meta-360-ra") == []
