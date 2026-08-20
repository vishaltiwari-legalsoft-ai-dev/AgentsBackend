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


def test_cloud_save_payload_is_json_safe(tmp_path, monkeypatch):
    """Dataset runs embed datetime.date objects; the Firestore client rejects
    those, so the cloud copy must be serialized like the disk copy."""
    from datetime import date

    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_OFFLINE", "1")
    captured = {}

    class _Doc:
        def set(self, payload):
            captured.update(payload)

    class _Coll:
        def document(self, _id):
            return _Doc()

    monkeypatch.setattr(runs, "_use_cloud", lambda: True)
    monkeypatch.setattr(runs, "_collection", lambda: _Coll())
    runs.save_run({"id": "r1", "kind": "dataset", "user_id": "u1",
                   "metrics": [{"channel": "Google", "date": date(2026, 7, 9)}]})
    assert captured["metrics"][0]["date"] == "2026-07-09"  # str, not date


def test_list_runs_merges_cloud_disk_wins(tmp_path, monkeypatch):
    """Cloud Run disk is ephemeral - list_runs must also surface Firestore docs."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    runs.save_run({"id": "local1", "kind": "dataset", "user_id": "u1",
                   "generated_at": "2026-07-08T10:00:00", "metrics": [1, 2]})
    monkeypatch.setattr(runs, "_use_cloud", lambda: True)
    monkeypatch.setattr(runs, "_cloud_list", lambda *a, **kw: [
        {"id": "cloud1", "kind": "dataset", "user_id": "u1", "generated_at": "2026-07-07T10:00:00"},
        {"id": "local1", "kind": "dataset", "user_id": "u1", "generated_at": "2026-07-08T09:00:00",
         "metrics": []},  # stale cloud copy of a disk run
    ])
    out = runs.list_runs("u1")
    assert [r["id"] for r in out] == ["local1", "cloud1"]
    assert out[0]["metrics"] == [1, 2]  # local copy wins


def test_save_run_reports_whether_the_cloud_write_landed(tmp_path, monkeypatch):
    """Callers that delete a superseded run need to know the replacement is
    durable. Offline, disk IS durable (True); cloud-configured, a failed set()
    means the run is only on this instance's ephemeral disk (False)."""
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_OFFLINE", "1")
    assert runs.save_run({"id": "off1", "kind": "dataset", "user_id": "u1"}) is True

    class _Doc:
        def set(self, payload):
            raise RuntimeError("400 the document exceeds the maximum allowed size")

    class _Coll:
        def document(self, _id):
            return _Doc()

    monkeypatch.setattr(runs, "_use_cloud", lambda: True)
    monkeypatch.setattr(runs, "_collection", lambda: _Coll())
    assert runs.save_run({"id": "cloud1", "kind": "dataset", "user_id": "u1"}) is False
    assert runs.get_run("cloud1") is not None  # local copy still written


# --- an unreadable store must never look like an empty one -------------------

def test_list_runs_raises_when_the_cloud_read_fails(tmp_path, monkeypatch):
    """The defect this pins: ``_cloud_list`` swallowed its own failure and
    returned []. On Cloud Run the disk is empty too, so a Firestore outage
    reached the dashboard as "this workspace has no data" — and the sheet pull
    computed its superseded set from a list missing every durable run."""
    import pytest

    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    runs.save_run({"id": "local1", "kind": "dataset", "user_id": "u1"})

    def _dead(*_a, **_kw):
        raise RuntimeError("503 the datastore is unavailable")

    monkeypatch.setattr(runs, "_use_cloud", lambda: True)
    monkeypatch.setattr(runs, "_collection", _dead)
    with pytest.raises(runs.RunStoreError):
        runs.list_runs("u1")


def test_cloud_list_reports_none_not_empty_on_failure(monkeypatch):
    """``[]`` = the workspace has no runs; ``None`` = we could not find out.
    Same contract as ``firestore_repo.count_collection``."""
    def _dead(*_a, **_kw):
        raise RuntimeError("503 the datastore is unavailable")

    monkeypatch.setattr(runs, "_collection", _dead)
    assert runs._cloud_list("u1") is None


def test_an_empty_cloud_is_still_an_empty_list(tmp_path, monkeypatch):
    """The other half of the contract: a genuinely empty store must NOT raise."""
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))

    class _Empty:
        def where(self, **_kw):
            return self

        def stream(self):
            return iter(())

    monkeypatch.setattr(runs, "_use_cloud", lambda: True)
    monkeypatch.setattr(runs, "_collection", _Empty)
    assert runs.list_runs("u1") == []
