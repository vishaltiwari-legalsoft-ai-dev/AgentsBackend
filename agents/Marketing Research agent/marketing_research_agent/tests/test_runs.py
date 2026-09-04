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


# --- the tenant key is not optional ------------------------------------------

def test_list_runs_refuses_to_read_without_a_workspace(tmp_path, monkeypatch):
    """The latent finding this closes: ``user_id`` defaulted to ``None`` and the
    Python filter read ``if user_id is not None``, so ``list_runs()`` returned
    EVERY tenant's runs. Unreachable through sign-in — ``payload["sub"]`` is
    always a Firestore doc id — but one careless cron or backfill call site from
    being live, and it is now also the read eviction decides from.
    """
    import pytest

    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    runs.save_run({"id": "a1", "kind": "daily_summary", "user_id": "u1"})
    runs.save_run({"id": "b1", "kind": "daily_summary", "user_id": "u2"})
    with pytest.raises(TypeError):
        runs.list_runs()                     # no workspace at all
    for blank in (None, "", "   "):
        with pytest.raises(ValueError):
            runs.list_runs(blank)
    assert {r["id"] for r in runs.list_runs("u1")} == {"a1"}


def test_json_that_is_not_a_run_never_becomes_one(tmp_path, monkeypatch):
    """``MR_RUNS_DIR`` is shared with ``profiles._cache_path``, so the glob in
    ``list_runs`` sees ``workbook_profiles*.json`` too. Those entered the map
    keyed by filename and were only ever dropped by the user filter — not a
    filter eviction should lean on. They are now refused at admission, and
    retention leaves the cache file alone."""
    import json as _json

    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "2")
    cache = tmp_path / "workbook_profiles.json"
    cache.write_text(_json.dumps({"signature": "sig", "profiles": []}), encoding="utf-8")
    (tmp_path / "junk.json").write_text("[1, 2, 3]", encoding="utf-8")

    for n in range(4):
        runs.save_run({"id": f"r{n}", "kind": "daily_summary", "user_id": "u1",
                       "generated_at": f"2026-07-0{n + 1}T00:00:00"})

    assert cache.exists(), "retention deleted the workbook-profile cache"
    assert (tmp_path / "junk.json").exists()
    assert {r["id"] for r in runs.list_runs("u1")} == {"r3", "r2"}


# --- retention: the store's lifecycle ----------------------------------------

def test_report_runs_are_capped_per_workspace_per_kind(tmp_path, monkeypatch):
    """The policy itself. ``POST /mr/reports/{kind}`` mints a fresh uuid run per
    call with no dedup and nothing bounded it; the cap is enforced at
    ``save_run``, so every kind and every route is covered."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "3")
    for n in range(7):
        runs.save_run({"id": f"r{n}", "kind": "daily_summary", "user_id": "u1",
                       "generated_at": f"2026-07-0{n + 1}T00:00:00"})
    kept = [r["id"] for r in runs.list_runs("u1", kind="daily_summary")]
    assert kept == ["r6", "r5", "r4"], kept          # the newest three, in order
    assert runs.get_run("r0") is None                 # and the oldest are gone


def test_each_kind_is_capped_independently(tmp_path, monkeypatch):
    """Per-kind, not per-workspace-total: a click-happy ``daily_summary`` must
    not evict the one ``quarterly_summary`` the board is reading."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "2")
    runs.save_run({"id": "q1", "kind": "quarterly_summary", "user_id": "u1",
                   "generated_at": "2026-06-01T00:00:00"})
    for n in range(6):
        runs.save_run({"id": f"d{n}", "kind": "daily_summary", "user_id": "u1",
                       "generated_at": f"2026-07-0{n + 1}T00:00:00"})
    assert runs.get_run("q1") is not None
    assert [r["id"] for r in runs.list_runs("u1", kind="daily_summary")] == ["d5", "d4"]


def test_the_workspaces_ingested_data_is_never_evicted(tmp_path, monkeypatch):
    """``STATE_KINDS`` are the workspace's numbers, not a deliverable —
    ``mr_runs`` is the only copy of parsed tracker state and ``_load_dataset``
    reads every one of them. Capping these would blank the dashboard, so the
    sheet pull's fetch-then-swap stays their only lifecycle."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "2")
    for kind in runs.STATE_KINDS:
        for n in range(5):
            runs.save_run({"id": f"{kind}-{n}", "kind": kind, "user_id": "u1",
                           "generated_at": f"2026-07-0{n + 1}T00:00:00"})
        kept = runs.list_runs("u1", kind=kind)
        assert len(kept) == 5, f"{kind} was capped — the workspace just lost data"
    assert runs.STATE_KINDS == frozenset({"dataset", "official_spend", "lead_analysis"})


def test_retention_never_reaches_another_workspace(tmp_path, monkeypatch):
    """The failure mode that would be worse than the growth it fixes.

    ``u2`` sits well under the cap and never writes again; ``u1`` saturates its
    own. Every one of ``u2``'s runs must still be there. This fails if the
    eviction candidates stop being read through ``list_runs(user_id, ...)``."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "2")
    for n in range(2):   # u2 sits exactly AT its own cap and never writes again
        runs.save_run({"id": f"other{n}", "kind": "daily_summary", "user_id": "u2",
                       "generated_at": f"2026-01-0{n + 1}T00:00:00"})   # oldest of all
    for n in range(9):
        runs.save_run({"id": f"mine{n}", "kind": "daily_summary", "user_id": "u1",
                       "generated_at": f"2026-07-0{n + 1}T00:00:00"})

    survivors = {r["id"] for r in runs.list_runs("u2", kind="daily_summary")}
    assert survivors == {"other0", "other1"}, (
        "retention crossed a tenant boundary — it evicted another workspace's runs")
    assert [r["id"] for r in runs.list_runs("u1", kind="daily_summary")] == ["mine8", "mine7"]


def test_retention_refuses_a_candidate_from_another_workspace(tmp_path, monkeypatch):
    """The second lock, pinned on its own.

    ``delete_run`` takes a bare id and is unscoped, so if the query above ever
    loosened, nothing else would stand between eviction and another tenant's
    data. Here ``list_runs`` is made to hand back a foreign run past the cap —
    exactly what a loosened query would do — and eviction must decline it."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "1")
    runs.save_run({"id": "theirs", "kind": "daily_summary", "user_id": "u2",
                   "generated_at": "2026-01-01T00:00:00"})
    mine = {"id": "mine", "kind": "daily_summary", "user_id": "u1",
            "generated_at": "2026-07-01T00:00:00"}
    leaky = [mine, {"id": "theirs", "kind": "daily_summary", "user_id": "u2",
                    "generated_at": "2026-01-01T00:00:00"}]
    monkeypatch.setattr(runs, "list_runs", lambda *_a, **_kw: list(leaky))

    assert runs._enforce_retention(mine) == []
    assert runs.get_run("theirs") is not None, (
        "eviction deleted a run it was handed from another workspace")


def test_retention_waits_for_a_durable_write(tmp_path, monkeypatch):
    """A replacement that only reached this instance's ephemeral disk is not a
    replacement — the next deploy loses it. Trading a durable old run for that
    is the data loss ``_pull_and_swap`` already refuses, so retention does too."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "1")
    runs.save_run({"id": "durable", "kind": "daily_summary", "user_id": "u1",
                   "generated_at": "2026-07-01T00:00:00"})

    class _Doc:
        def set(self, _payload):
            raise RuntimeError("503 the datastore is unavailable")

        def delete(self):
            raise AssertionError("retention deleted a run after a failed write")

    class _Coll:
        def document(self, _id):
            return _Doc()

    monkeypatch.setattr(runs, "_use_cloud", lambda: True)
    monkeypatch.setattr(runs, "_collection", lambda: _Coll())
    assert runs.save_run({"id": "ephemeral", "kind": "daily_summary", "user_id": "u1",
                          "generated_at": "2026-07-02T00:00:00"}) is False
    assert runs.get_run("durable") is not None


def test_an_unreadable_store_does_not_fail_the_save(tmp_path, monkeypatch):
    """Retention is best effort. The run is already written by the time it runs;
    a Firestore blip leaves more runs than the cap — the old behaviour, not a
    new failure — and must never turn a successful save into an error."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "1")

    def _dead(*_a, **_kw):
        raise runs.RunStoreError("the saved-runs store could not be read")

    monkeypatch.setattr(runs, "list_runs", _dead)
    assert runs.save_run({"id": "r1", "kind": "daily_summary", "user_id": "u1"}) is True
    assert runs.get_run("r1") is not None


def test_an_unstamped_run_is_kept_rather_than_evicted_globally(tmp_path, monkeypatch):
    """A run carrying no ``user_id`` belongs to no workspace, so there is no
    scope to evict within. It is left alone and logged — the alternative is an
    unscoped delete pass, which is the bug this whole design exists to avoid."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "1")
    runs.save_run({"id": "owned", "kind": "daily_summary", "user_id": "u1"})
    assert runs.save_run({"id": "orphan", "kind": "daily_summary"}) is True
    assert runs.get_run("owned") is not None
    assert runs.get_run("orphan") is not None


def test_the_cap_is_configurable_and_a_bad_value_is_not_a_purge(monkeypatch):
    monkeypatch.delenv("MR_RUN_RETENTION_PER_KIND", raising=False)
    assert runs.retention_cap() == runs._DEFAULT_RETENTION
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "50")
    assert runs.retention_cap() == 50
    for bad in ("", "   ", "nope", "0", "-5"):
        monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", bad)
        assert runs.retention_cap() == runs._DEFAULT_RETENTION, bad
