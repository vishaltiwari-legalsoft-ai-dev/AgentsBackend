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


# --- the workspace key -------------------------------------------------------
# The key the workbook-derived kinds (dataset / official_spend / lead_analysis)
# are stored and read under. Resolved from the SERVER's configuration only; the
# router-level behaviour it buys is pinned in ``test_mr_router.py`` ("the shared
# MR workspace") and ``test_mr_cross_tenant.py``. Env is read on every call, so
# each test sets exactly the variables it means and nothing leaks in from the
# machine running it.
#
# Sharing is an OPT-IN: ON only when ``MR_WORKSPACE_SHARED`` positively says so
# (1 / true / yes / on), OFF for unset and for every other value. Production
# already has ``MR_CRON_USER_ID`` set, so a switch that defaulted on would have
# made merely deploying the code the decision to show one account's data to
# everyone; and one that only recognised "0" as off would fail OPEN on a typo.

def _workspace_env(monkeypatch, *, workspace_id=None, cron_user_id=None, shared=None):
    for var, value in (("MR_WORKSPACE_ID", workspace_id),
                       ("MR_CRON_USER_ID", cron_user_id),
                       ("MR_WORKSPACE_SHARED", shared)):
        if value is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, value)


def test_with_no_key_configured_the_workspace_is_the_callers_own(monkeypatch):
    """The parity gate: local, dev and every pre-existing suite set neither
    variable, and for them the workspace must BE the caller."""
    from marketing_research_agent import workspace

    _workspace_env(monkeypatch)
    assert workspace.configured_key() == ""
    assert workspace.is_shared() is False
    assert workspace.workspace_id("u1") == "u1"
    assert workspace.workspace_id("u2") == "u2"

    # Switching sharing on with nothing to share is inert, not an error.
    _workspace_env(monkeypatch, shared="1")
    assert workspace.sharing_enabled() is True and workspace.is_shared() is False
    assert workspace.workspace_id("u1") == "u1"


def test_the_cron_account_is_the_workspace_when_nothing_more_specific_is_set(monkeypatch):
    from marketing_research_agent import workspace

    _workspace_env(monkeypatch, cron_user_id="owner-uid", shared="1")
    assert workspace.is_shared() is True
    assert workspace.configured_key() == "owner-uid"
    # Whoever asks — the owner, a colleague — reads the one key.
    assert workspace.workspace_id("owner-uid") == "owner-uid"
    assert workspace.workspace_id("a-colleague") == "owner-uid"


def test_an_explicit_workspace_id_beats_the_cron_account(monkeypatch):
    """Precedence: MR_WORKSPACE_ID > MR_CRON_USER_ID > the caller."""
    from marketing_research_agent import workspace

    _workspace_env(monkeypatch, workspace_id="team-ws", cron_user_id="owner-uid", shared="1")
    assert workspace.configured_key() == "team-ws"
    assert workspace.workspace_id("a-colleague") == "team-ws"

    _workspace_env(monkeypatch, workspace_id="team-ws", shared="1")  # no cron account at all
    assert workspace.is_shared() is True
    assert workspace.workspace_id("a-colleague") == "team-ws"


def test_a_whitespace_only_value_counts_as_unset(monkeypatch):
    """A stray space in a deployment's env must fall back to per-caller keys. It
    must never become a key nobody can match — or, worse, one everybody does."""
    from marketing_research_agent import workspace

    _workspace_env(monkeypatch, workspace_id="   ", cron_user_id="\t ", shared="1")
    assert workspace.configured_key() == ""
    assert workspace.is_shared() is False
    assert workspace.workspace_id("u1") == "u1"

    # …and a blank explicit id defers to the cron account rather than blanking it.
    _workspace_env(monkeypatch, workspace_id="  ", cron_user_id="owner-uid", shared="1")
    assert workspace.configured_key() == "owner-uid"


def test_the_configured_key_is_stripped(monkeypatch):
    """A trailing newline (an ``echo``-created secret) must not change the key."""
    from marketing_research_agent import workspace

    _workspace_env(monkeypatch, cron_user_id="  owner-uid\n", shared="1")
    assert workspace.workspace_id("someone") == "owner-uid"


def test_sharing_turns_on_only_for_a_value_the_code_recognises(monkeypatch):
    """The opt-in, both directions. Case and surrounding whitespace are ignored;
    the four words are the whole vocabulary."""
    from marketing_research_agent import workspace

    for on in ("1", "true", "yes", "on", "TRUE", " Yes ", "On", "\t1\n"):
        _workspace_env(monkeypatch, cron_user_id="owner-uid", shared=on)
        assert workspace.sharing_enabled() is True, repr(on)
        assert workspace.workspace_id("a-colleague") == "owner-uid", repr(on)

    for off in ("0", "false", "off", "no", "OFF", " Off ", ""):
        _workspace_env(monkeypatch, cron_user_id="owner-uid", shared=off)
        assert workspace.sharing_enabled() is False, repr(off)
        assert workspace.is_shared() is False, repr(off)
        assert workspace.workspace_id("a-colleague") == "a-colleague", repr(off)


def test_a_typo_in_the_switch_fails_closed(monkeypatch):
    """The reason for the polarity. A switch that only recognised "0" / "false" /
    "off" as off would leave sharing ON for ``ture`` — a typo made while trying
    to enable it, or worse, while trying to disable it. Here every value the code
    does not positively recognise is OFF, and OFF is the caller's own key."""
    from marketing_research_agent import workspace

    for typo in ("ture", "tru", "enabled", "enable", "y", "yep", "2", "-1", "onn",
                 "of", "flase", "shared", "1 1", "true!", "on;", "yes please"):
        _workspace_env(monkeypatch, cron_user_id="owner-uid", workspace_id="team-ws",
                       shared=typo)
        assert workspace.sharing_enabled() is False, repr(typo)
        assert workspace.is_shared() is False, repr(typo)
        assert workspace.workspace_id("a-colleague") == "a-colleague", repr(typo)


def test_deploying_without_the_switch_changes_nothing(monkeypatch):
    """Production ALREADY has ``MR_CRON_USER_ID`` set. Shipping this code must
    therefore not, by itself, share anything: key configured, switch unset ->
    every caller is still their own workspace, exactly as before this module
    existed. Enabling it is a separate, deliberate env change."""
    from marketing_research_agent import workspace

    _workspace_env(monkeypatch, cron_user_id="owner-uid")
    assert workspace.configured_key() == "owner-uid"      # the key IS configured…
    assert workspace.sharing_enabled() is False           # …and still nothing is shared
    assert workspace.is_shared() is False
    for who in ("owner-uid", "a-colleague", "another"):
        assert workspace.workspace_id(who) == who

    # An explicit workspace id alone is no different: it is a key, not a decision.
    _workspace_env(monkeypatch, workspace_id="team-ws", cron_user_id="owner-uid")
    assert workspace.is_shared() is False
    assert workspace.workspace_id("a-colleague") == "a-colleague"


def test_a_blank_caller_can_never_become_a_blank_workspace(monkeypatch):
    """Same contract as ``runs.list_runs``: when the resolver has to fall back to
    the caller and there is no caller, it raises instead of returning a key that
    would widen every read keyed on it."""
    import pytest

    from marketing_research_agent import workspace

    _workspace_env(monkeypatch)
    for blank in (None, "", "   "):
        with pytest.raises(ValueError):
            workspace.workspace_id(blank)

    # Switched off — or never switched on — is a fallback too: the same refusal.
    for shared in ("0", None, "ture"):
        _workspace_env(monkeypatch, cron_user_id="owner-uid", shared=shared)
        with pytest.raises(ValueError):
            workspace.workspace_id("")

    # A configured, enabled workspace needs no caller id at all — not a widening.
    _workspace_env(monkeypatch, cron_user_id="owner-uid", shared="1")
    assert workspace.workspace_id("") == "owner-uid"


def test_the_fallback_hands_the_callers_id_back_unchanged(monkeypatch):
    """MR compares tenant ids raw — ``7`` and ``"7"`` are two tenants, pinned by
    ``test_tenant_id_type_contract`` — so the unshared mode must not normalise
    what it was given."""
    from marketing_research_agent import workspace

    _workspace_env(monkeypatch)
    assert workspace.workspace_id(7) == 7 and workspace.workspace_id(7) != "7"
    assert workspace.workspace_id("7") == "7"


def test_the_environment_is_read_on_every_call(monkeypatch):
    """Nothing is cached at import, so a deployment can flip the switch and a
    test can monkeypatch it."""
    from marketing_research_agent import workspace

    _workspace_env(monkeypatch)
    assert workspace.workspace_id("u1") == "u1"
    monkeypatch.setenv("MR_CRON_USER_ID", "owner-uid")
    assert workspace.workspace_id("u1") == "u1"           # key alone: still off
    monkeypatch.setenv("MR_WORKSPACE_SHARED", "1")
    assert workspace.workspace_id("u1") == "owner-uid"
    monkeypatch.setenv("MR_WORKSPACE_SHARED", "0")
    assert workspace.workspace_id("u1") == "u1"
    monkeypatch.delenv("MR_WORKSPACE_SHARED")             # rollback by unsetting
    assert workspace.workspace_id("u1") == "u1"


# --- report templates: workspace-wide, append-only, versioned ------------------

import pytest  # noqa: E402


@pytest.fixture
def tpl_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.delenv("MR_RUN_RETENTION_PER_KIND", raising=False)
    return tmp_path


def _upload(ws, n, who="a@x.com"):
    return runs.save_template_version(ws, uploaded_by=who, source_kind="builder",
                                      spec={"layout": f"v{n}"})


def test_no_template_ever_saved_means_the_builtin(tpl_store):
    active = runs.active_template("ws-a")
    assert active["builtin"] is True and active["id"] == runs.BUILTIN_TEMPLATE_ID
    assert runs.list_template_versions("ws-a") == []


def test_the_newest_version_is_active_and_history_is_newest_first(tpl_store):
    v1, v2, v3 = (_upload("ws-a", i) for i in (1, 2, 3))
    assert runs.active_template("ws-a")["id"] == v3["id"]
    assert [r["id"] for r in runs.list_template_versions("ws-a")] == [v3["id"], v2["id"], v1["id"]]
    assert [r["id"] for r in runs.list_template_versions("ws-a", limit=2)] == [v3["id"], v2["id"]]
    assert v3["uploaded_by"] == "a@x.com" and v3["user_id"] == "ws-a"
    assert v3["kind"] == "report_template:vendor_performance"


def test_workspace_b_can_never_read_or_revert_workspace_as_templates(tpl_store):
    va = _upload("ws-a", 1)
    assert runs.list_template_versions("ws-b") == []
    assert runs.active_template("ws-b")["builtin"] is True
    with pytest.raises(LookupError):
        runs.revert_template("ws-b", va["id"], uploaded_by="b@x.com")
    assert runs.list_template_versions("ws-b") == []          # nothing copied across
    assert runs.active_template("ws-a")["id"] == va["id"]     # A untouched


def test_revert_appends_a_copy_and_never_rewrites_history(tpl_store):
    v1, v2 = _upload("ws-a", 1), _upload("ws-a", 2)
    r = runs.revert_template("ws-a", v1["id"], uploaded_by="b@x.com")
    assert r["id"] not in (v1["id"], v2["id"]) and r["reverted_from"] == v1["id"]
    assert r["spec"] == {"layout": "v1"} and r["uploaded_by"] == "b@x.com"
    assert runs.active_template("ws-a")["id"] == r["id"]
    assert len(runs.list_template_versions("ws-a")) == 3


def test_revert_to_builtin_is_a_version_too(tpl_store):
    _upload("ws-a", 1)
    r = runs.revert_template("ws-a", runs.BUILTIN_TEMPLATE_ID, uploaded_by="a@x.com")
    active = runs.active_template("ws-a")
    assert active["id"] == r["id"] and active["builtin"] is True and active["spec"] is None
    assert len(runs.list_template_versions("ws-a")) == 2


def test_retention_trims_the_oldest_versions_never_the_active(tpl_store, monkeypatch):
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "3")
    ids = [_upload("ws-a", i)["id"] for i in range(5)]
    kept = [r["id"] for r in runs.list_template_versions("ws-a")]
    assert kept == ids[:1:-1]                    # the newest three, newest first
    assert runs.active_template("ws-a")["id"] == ids[-1]


def test_the_default_template_retention_is_25(tpl_store):
    for i in range(27):
        _upload("ws-a", i)
    assert len(runs.list_template_versions("ws-a")) == 25


def test_another_workspaces_uploads_never_evict_mine(tpl_store, monkeypatch):
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "2")
    mine = _upload("ws-a", 1)
    for i in range(4):
        _upload("ws-b", i)
    assert [r["id"] for r in runs.list_template_versions("ws-a")] == [mine["id"]]


def test_templates_are_validated_before_they_are_stored(tpl_store):
    with pytest.raises(ValueError):
        runs.save_template_version("", uploaded_by="a@x.com", source_kind="builder", spec={})
    with pytest.raises(ValueError):
        runs.save_template_version("ws-a", uploaded_by=" ", source_kind="builder", spec={})
    with pytest.raises(ValueError):
        runs.save_template_version("ws-a", uploaded_by="a@x.com", source_kind="docx", spec={})
    with pytest.raises(ValueError):   # html only in html mode
        runs.save_template_version("ws-a", uploaded_by="a@x.com", source_kind="pdf",
                                   spec={}, html="<p>x</p>")
    with pytest.raises(ValueError):
        runs.save_template_version("ws-a", uploaded_by="a@x.com", source_kind="html",
                                   html="x" * 1_000_000)
    with pytest.raises(ValueError):
        runs.list_template_versions(None)
    with pytest.raises(ValueError):
        runs.template_kind("board")
    assert runs.list_template_versions("ws-a") == []


def test_a_version_that_misses_the_durable_store_is_a_loud_failure(tpl_store, monkeypatch):
    """No fake success: a template only one instance's disk holds would be
    'active' there and invisible everywhere else."""
    class _Doc:
        def set(self, _payload):
            raise RuntimeError("503")

        def delete(self):
            pass

    class _Coll:
        def document(self, _id):
            return _Doc()

    monkeypatch.setattr(runs, "_use_cloud", lambda: True)
    monkeypatch.setattr(runs, "_collection", lambda: _Coll())
    monkeypatch.setattr(runs, "_cloud_list", lambda *a, **kw: [])
    with pytest.raises(runs.RunStoreError):
        _upload("ws-a", 1)
    assert list(tpl_store.glob("*.json")) == []                # local copy removed


def test_the_template_read_is_one_scoped_equality_query(tpl_store, monkeypatch):
    seen = []
    monkeypatch.setattr(runs, "_use_cloud", lambda: True)
    monkeypatch.setattr(runs, "_cloud_list",
                        lambda user_id=None, kind=None: seen.append((user_id, kind)) or [])
    runs.list_template_versions("ws-a")
    assert seen == [("ws-a", "report_template:vendor_performance")]


def test_templates_never_appear_as_saved_reports():
    from marketing_research_agent import reports

    kind = runs.template_kind()
    assert kind not in reports.KINDS and kind not in runs.STATE_KINDS
