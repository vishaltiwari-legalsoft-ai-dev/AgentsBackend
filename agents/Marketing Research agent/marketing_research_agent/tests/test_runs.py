from marketing_research_agent import runs


def test_save_and_get_run(tmp_path, monkeypatch):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    rid = runs.new_run_id()
    runs.save_run({"id": rid, "kind": "daily_summary", "user_id": "u1", "markdown": "# hi"})
    got = runs.get_run(rid)
    assert got and got["kind"] == "daily_summary"


def test_list_runs_filters_by_user(tmp_path, monkeypatch):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    runs.save_run({"id": runs.new_run_id(), "kind": "k", "user_id": "u1"})
    runs.save_run({"id": runs.new_run_id(), "kind": "k", "user_id": "u2"})
    assert len(runs.list_runs("u1")) == 1


def test_list_runs_merges_cloud_disk_wins(tmp_path, monkeypatch):
    """Cloud Run disk is ephemeral - list_runs must also surface Firestore docs."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    runs.save_run({"id": "local1", "kind": "dataset", "user_id": "u1",
                   "generated_at": "2026-07-08T10:00:00", "metrics": [1, 2]})
    monkeypatch.setattr(runs, "_use_cloud", lambda: True)
    monkeypatch.setattr(runs, "_cloud_list", lambda: [
        {"id": "cloud1", "kind": "dataset", "user_id": "u1", "generated_at": "2026-07-07T10:00:00"},
        {"id": "local1", "kind": "dataset", "user_id": "u1", "generated_at": "2026-07-08T09:00:00",
         "metrics": []},  # stale cloud copy of a disk run
    ])
    out = runs.list_runs("u1")
    assert [r["id"] for r in out] == ["local1", "cloud1"]
    assert out[0]["metrics"] == [1, 2]  # local copy wins
