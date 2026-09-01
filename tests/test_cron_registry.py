"""Cron Manager v1 — the registry, the merge, the cache, the guard, and the
honest-partial outage envelope. Fully offline: the Cloud Scheduler REST call is
stubbed at the session/fetch seam, so no test resolves credentials or opens a
socket (the repo-root conftest guards hold untouched).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import get_current_user
from app.services import cron_registry
from app.services.agent_config import AGENT_LABELS

client = TestClient(fastapi_app)

CREATOR = {
    "id": "u-creator", "email": "boss@legalsoft.com",
    "is_admin": True, "is_creator": True,
}
COLLEAGUE = {
    "id": "u-colleague", "email": "colleague@legalsoft.com",
    "is_admin": False, "is_creator": False,
}

#: Every key a job row carries; either side's absence is nulls, never a
#: missing key — the frontend must not have to guess the shape.
ROW_KEYS = {
    "id", "name", "agent_id", "agent_label", "endpoint", "purpose", "why_time",
    "schedule", "state", "last_attempt", "next_time", "origin",
}
ENVELOPE_KEYS = {"generated_at", "scheduler_ok", "scheduler_error", "jobs"}


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _fresh_cache():
    """Every test starts and ends with a cold scheduler cache — the module
    holds process state, and a warm entry from one test must not decide what
    the next one 'fetched'."""
    cron_registry.invalidate_cache()
    yield
    cron_registry.invalidate_cache()


@pytest.fixture(autouse=True)
def _isolated_overrides():
    """Restore ``dependency_overrides`` around every test (same discipline as
    ``app/routers/tests/conftest.py`` — this module lives outside that
    directory, so it carries its own copy)."""
    saved = dict(fastapi_app.dependency_overrides)
    yield
    fastapi_app.dependency_overrides.clear()
    fastapi_app.dependency_overrides.update(saved)


@pytest.fixture()
def as_user(_isolated_overrides) -> Callable[[dict[str, Any]], None]:
    def _install(user: dict[str, Any]) -> None:
        caller = dict(user)
        fastapi_app.dependency_overrides[get_current_user] = lambda: dict(caller)
    return _install


def _raw_job(name: str, **over: Any) -> dict[str, Any]:
    """One Cloud Scheduler REST job resource, as the API returns it."""
    job: dict[str, Any] = {
        "name": (
            f"projects/{cron_registry.SCHEDULER_PROJECT}/locations/"
            f"{cron_registry.SCHEDULER_LOCATION}/jobs/{name}"
        ),
        "schedule": "*/15 * * * *",
        "timeZone": "Asia/Kolkata",
        "state": "ENABLED",
        "status": {},
        "lastAttemptTime": "2026-09-01T21:00:00.123456Z",
        "scheduleTime": "2026-09-01T21:15:00Z",
    }
    job.update(over)
    return job


def _shaped(name: str, **over: Any) -> dict[str, Any]:
    """One entry of ``fetch_live_jobs``'s output, for stubbing the fetch seam."""
    row: dict[str, Any] = {
        "id": name,
        "schedule": {"cron": "*/15 * * * *", "timezone": "Asia/Kolkata"},
        "state": "ENABLED",
        "last_attempt": {"time": "2026-09-01T21:00:00.123456Z", "ok": True},
        "next_time": "2026-09-01T21:15:00Z",
    }
    row.update(over)
    return row


def _all_registered_live() -> dict[str, dict[str, Any]]:
    return {e["id"]: _shaped(e["id"]) for e in cron_registry.CRON_REGISTRY}


class _FakeResp:
    def __init__(self, payload: dict[str, Any], status: int = 200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} from Cloud Scheduler")

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeSession:
    """Stands in for the AuthorizedSession — records every request and asserts
    the deadline is stated on each one."""

    def __init__(self, pages: list[_FakeResp]):
        self._pages, self.calls = list(pages), 0

    def get(self, url: str, params: Any = None, timeout: Any = None) -> _FakeResp:
        assert url == cron_registry.SCHEDULER_JOBS_URL
        assert timeout == cron_registry.SCHEDULER_TIMEOUT_SECONDS, (
            "the scheduler call must state its deadline"
        )
        self.calls += 1
        if not self._pages:
            raise AssertionError("fetched more pages than the fake provided")
        return self._pages.pop(0)


@pytest.fixture()
def stub_session(monkeypatch) -> Callable[[list[_FakeResp]], _FakeSession]:
    def _install(pages: list[_FakeResp]) -> _FakeSession:
        session = _FakeSession(pages)
        monkeypatch.setattr(cron_registry, "_session", lambda: session)
        return session
    return _install


# --------------------------------------------------------------------------- #
# Registry conformance
# --------------------------------------------------------------------------- #

def test_every_registry_entry_is_fully_described():
    seen_ids: set[str] = set()
    for entry in cron_registry.CRON_REGISTRY:
        for field in ("id", "agent_id", "name", "endpoint", "purpose", "why_time"):
            assert str(entry.get(field) or "").strip(), (
                f"registry entry {entry.get('id')!r} has an empty {field!r}"
            )
        expected = entry.get("expected") or {}
        assert str(expected.get("cron") or "").strip(), (
            f"registry entry {entry['id']!r} has no expected cron"
        )
        assert str(expected.get("timezone") or "").strip(), (
            f"registry entry {entry['id']!r} has no expected timezone"
        )
        assert entry["id"] not in seen_ids, f"duplicate registry id {entry['id']!r}"
        seen_ids.add(entry["id"])


def test_every_registry_endpoint_is_a_real_route():
    """A registry whose endpoint drifts from the route table is documentation
    lying about the system — the exact failure this panel exists to catch.
    ``endpoint`` carries the method too, so both halves are checked."""
    served = {
        (method, route.path)
        for route in fastapi_app.routes if isinstance(route, APIRoute)
        for method in route.methods - {"HEAD", "OPTIONS"}
    }
    for entry in cron_registry.CRON_REGISTRY:
        method, _, path = entry["endpoint"].partition(" ")
        assert (method, path) in served, (
            f"registry entry {entry['id']!r} points at {entry['endpoint']!r}, "
            "which is not a route this app serves"
        )


def test_agent_labels_come_from_the_shared_label_dict():
    """The frontend joins rows to agents by label — it must be the same label
    the run tables already use, not a synonym typed here."""
    for entry in cron_registry.CRON_REGISTRY:
        assert entry["agent_id"] in AGENT_LABELS
    rows = {r["id"]: r for r in cron_registry.merge_jobs(_all_registered_live())}
    for entry in cron_registry.CRON_REGISTRY:
        assert rows[entry["id"]]["agent_label"] == AGENT_LABELS[entry["agent_id"]]


# --------------------------------------------------------------------------- #
# Live fetch: field extraction + ok/failed derivation
# --------------------------------------------------------------------------- #

def test_fetch_extracts_the_panel_fields(stub_session):
    stub_session([_FakeResp({"jobs": [_raw_job("geo-poll-daily", schedule="0 2 * * *")]})])
    live = cron_registry.fetch_live_jobs()
    assert set(live) == {"geo-poll-daily"}
    assert live["geo-poll-daily"] == {
        "id": "geo-poll-daily",
        "schedule": {"cron": "0 2 * * *", "timezone": "Asia/Kolkata"},
        "state": "ENABLED",
        "last_attempt": {"time": "2026-09-01T21:00:00.123456Z", "ok": True},
        "next_time": "2026-09-01T21:15:00Z",
    }


def test_an_empty_status_means_the_last_attempt_succeeded(stub_session):
    # Cloud Scheduler serialises an OK google.rpc.Status as {} — or omits it.
    absent = _raw_job("b")
    del absent["status"]
    stub_session([_FakeResp({"jobs": [_raw_job("a", status={}), absent]})])
    live = cron_registry.fetch_live_jobs()
    assert live["a"]["last_attempt"]["ok"] is True
    assert live["b"]["last_attempt"]["ok"] is True


def test_a_failed_status_code_reads_as_a_failed_attempt(stub_session):
    stub_session([_FakeResp({"jobs": [
        _raw_job("mr-refresh-3min", status={"code": 7, "message": "PERMISSION_DENIED"}),
    ]})])
    row = cron_registry.fetch_live_jobs()["mr-refresh-3min"]
    assert row["last_attempt"] == {"time": "2026-09-01T21:00:00.123456Z", "ok": False}


def test_a_job_that_never_fired_reports_no_verdict(stub_session):
    """No lastAttemptTime → last_attempt null, not ok:true. 'Has never run'
    and 'ran fine' must not be the same green."""
    raw = _raw_job("seo-sweep-daily")
    del raw["lastAttemptTime"]
    stub_session([_FakeResp({"jobs": [raw]})])
    assert cron_registry.fetch_live_jobs()["seo-sweep-daily"]["last_attempt"] is None


def test_scheduler_states_map_onto_the_two_the_panel_knows(stub_session):
    stub_session([_FakeResp({"jobs": [
        _raw_job("a", state="ENABLED"),
        _raw_job("b", state="PAUSED"),
        _raw_job("c", state="DISABLED"),          # knocked out ≙ not firing
        _raw_job("d", state="STATE_UNSPECIFIED"),  # no claim, not a guess
    ]})])
    live = cron_registry.fetch_live_jobs()
    assert live["a"]["state"] == "ENABLED"
    assert live["b"]["state"] == "PAUSED"
    assert live["c"]["state"] == "PAUSED"
    assert live["d"]["state"] is None


def test_fetch_follows_the_page_token_to_the_end(stub_session):
    session = stub_session([
        _FakeResp({"jobs": [_raw_job("one")], "nextPageToken": "tok"}),
        _FakeResp({"jobs": [_raw_job("two")]}),
    ])
    live = cron_registry.fetch_live_jobs()
    assert set(live) == {"one", "two"}
    assert session.calls == 2


def test_fetch_refuses_an_endless_pager(stub_session):
    pages = [
        _FakeResp({"jobs": [_raw_job(f"j{i}")], "nextPageToken": "tok"})
        for i in range(cron_registry.MAX_LIST_PAGES + 3)
    ]
    session = stub_session(pages)
    with pytest.raises(cron_registry.SchedulerUnavailable, match="partial"):
        cron_registry.fetch_live_jobs()
    assert session.calls == cron_registry.MAX_LIST_PAGES


def test_fetch_wraps_a_transport_failure_honestly(monkeypatch):
    class _Broken:
        def get(self, *a, **kw):
            raise ConnectionError("connection reset by peer")

    monkeypatch.setattr(cron_registry, "_session", lambda: _Broken())
    with pytest.raises(cron_registry.SchedulerUnavailable, match="connection reset"):
        cron_registry.fetch_live_jobs()


def test_fetch_wraps_an_http_error_honestly(stub_session):
    stub_session([_FakeResp({}, status=403)])
    with pytest.raises(cron_registry.SchedulerUnavailable, match="HTTP 403"):
        cron_registry.fetch_live_jobs()


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #

def test_merge_pairs_registry_with_live_state():
    rows = cron_registry.merge_jobs(_all_registered_live())
    assert len(rows) == len(cron_registry.CRON_REGISTRY)
    by_id = {r["id"]: r for r in rows}
    geo = by_id["geo-poll-daily"]
    assert geo["origin"] == "live_registered"
    assert geo["name"] == "AI answer poll"
    assert geo["agent_id"] == "a10"
    assert geo["agent_label"] == AGENT_LABELS["a10"]
    assert geo["endpoint"] == "POST /api/geo/cron/poll"
    assert geo["purpose"] and geo["why_time"]
    # Live side comes from the scheduler, not from the registry expectation.
    assert geo["schedule"] == {"cron": "*/15 * * * *", "timezone": "Asia/Kolkata"}
    assert geo["state"] == "ENABLED"
    assert geo["last_attempt"]["ok"] is True
    assert geo["next_time"] == "2026-09-01T21:15:00Z"


def test_merge_renders_a_dead_cron_as_registry_only():
    """The worst failure mode: registered, believed running, gone from the
    scheduler. The row stays — expected schedule shown, live claims null."""
    live = _all_registered_live()
    del live["seo-sweep-daily"]
    rows = {r["id"]: r for r in cron_registry.merge_jobs(live)}
    dead = rows["seo-sweep-daily"]
    assert dead["origin"] == "registry_only"
    assert dead["agent_id"] == "a2" and dead["purpose"]
    # The expectation is curated knowledge and is shown as the schedule…
    assert dead["schedule"] == {"cron": "30 3 * * *", "timezone": "Asia/Kolkata"}
    # …while everything only the scheduler could know stays null.
    assert dead["state"] is None
    assert dead["last_attempt"] is None
    assert dead["next_time"] is None
    # The other rows are untouched.
    assert rows["geo-poll-daily"]["origin"] == "live_registered"


def test_merge_renders_an_unwritten_job_as_live_only():
    live = _all_registered_live()
    live["mystery-nightly"] = _shaped(
        "mystery-nightly", schedule={"cron": "0 4 * * *", "timezone": "Etc/UTC"}
    )
    rows = {r["id"]: r for r in cron_registry.merge_jobs(live)}
    stray = rows["mystery-nightly"]
    assert stray["origin"] == "live_only"
    assert stray["schedule"] == {"cron": "0 4 * * *", "timezone": "Etc/UTC"}
    for field in ("name", "agent_id", "agent_label", "endpoint", "purpose", "why_time"):
        assert stray[field] is None, field


def test_every_merged_row_carries_the_full_contract():
    """Nulls where a side is absent — never a missing key."""
    live = _all_registered_live()
    del live["mr-refresh-3min"]                           # a registry_only row
    live["mystery-nightly"] = _shaped("mystery-nightly")  # a live_only row
    for row in cron_registry.merge_jobs(live):
        assert set(row) == ROW_KEYS, row["id"]


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

def test_the_scheduler_fetch_is_cached_for_a_minute(monkeypatch):
    calls = {"n": 0}

    def counting_fetch():
        calls["n"] += 1
        return _all_registered_live()

    monkeypatch.setattr(cron_registry, "fetch_live_jobs", counting_fetch)
    cron_registry.merged_jobs()
    cron_registry.merged_jobs()
    cron_registry.merged_jobs()
    assert calls["n"] == 1, "three reads inside the TTL must cost one fetch"


def test_the_cache_expires_after_the_ttl(monkeypatch):
    calls = {"n": 0}
    clock = {"t": 1000.0}
    monkeypatch.setattr(cron_registry, "_now", lambda: clock["t"])

    def counting_fetch():
        calls["n"] += 1
        return _all_registered_live()

    monkeypatch.setattr(cron_registry, "fetch_live_jobs", counting_fetch)
    cron_registry.merged_jobs()
    clock["t"] += cron_registry.CACHE_TTL_SECONDS - 1
    cron_registry.merged_jobs()
    assert calls["n"] == 1
    clock["t"] += 2  # now past the TTL
    cron_registry.merged_jobs()
    assert calls["n"] == 2


def test_a_failed_fetch_is_not_cached(monkeypatch):
    """An outage must not be remembered for 60s — the next request retries."""
    calls = {"n": 0}

    def flaky_fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            raise cron_registry.SchedulerUnavailable("transient")
        return _all_registered_live()

    monkeypatch.setattr(cron_registry, "fetch_live_jobs", flaky_fetch)
    with pytest.raises(cron_registry.SchedulerUnavailable):
        cron_registry.merged_jobs()
    rows = cron_registry.merged_jobs()
    assert calls["n"] == 2
    assert all(r["origin"] == "live_registered" for r in rows)


# --------------------------------------------------------------------------- #
# The route: guard, envelope, honest-partial outage
# --------------------------------------------------------------------------- #

def test_a_creator_gets_the_merged_table(monkeypatch, as_user):
    as_user(CREATOR)
    monkeypatch.setattr(cron_registry, "fetch_live_jobs", _all_registered_live)
    resp = client.get("/api/cron/jobs")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["scheduler_ok"] is True
    assert body["scheduler_error"] is None
    datetime.fromisoformat(body["generated_at"])  # parseable, tz-aware ISO
    jobs = body["jobs"]
    assert {j["id"] for j in jobs} == {e["id"] for e in cron_registry.CRON_REGISTRY}
    for job in jobs:
        assert set(job) == ROW_KEYS
        assert job["origin"] == "live_registered"


def test_page_loads_share_one_scheduler_fetch(monkeypatch, as_user):
    as_user(CREATOR)
    calls = {"n": 0}

    def counting_fetch():
        calls["n"] += 1
        return _all_registered_live()

    monkeypatch.setattr(cron_registry, "fetch_live_jobs", counting_fetch)
    for _ in range(4):
        assert client.get("/api/cron/jobs").status_code == 200
    assert calls["n"] == 1


def test_a_signed_in_non_creator_is_refused(monkeypatch, as_user):
    as_user(COLLEAGUE)
    monkeypatch.setattr(
        cron_registry, "fetch_live_jobs",
        lambda: pytest.fail("the guard must run before any scheduler fetch"),
    )
    resp = client.get("/api/cron/jobs")
    assert resp.status_code == 403, resp.text


def test_an_anonymous_caller_is_refused(monkeypatch):
    monkeypatch.setattr(
        cron_registry, "fetch_live_jobs",
        lambda: pytest.fail("the guard must run before any scheduler fetch"),
    )
    resp = client.get("/api/cron/jobs")
    assert resp.status_code == 401, resp.text


def test_an_outage_is_a_labelled_partial_not_a_disguise(monkeypatch, as_user):
    """Scheduler down → 200, scheduler_ok false, one plain sentence, and every
    row explicitly registry_only with the *expected* schedule — the frontend
    renders these as Unconfirmed. No live claim (state, attempts, next fire)
    survives into the rows."""
    as_user(CREATOR)

    def broken_fetch():
        raise cron_registry.SchedulerUnavailable("HTTP 503 from Cloud Scheduler")

    monkeypatch.setattr(cron_registry, "fetch_live_jobs", broken_fetch)
    resp = client.get("/api/cron/jobs")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scheduler_ok"] is False
    error = body["scheduler_error"]
    assert "Cloud Scheduler" in error and "HTTP 503" in error
    assert "\n" not in error and "Traceback" not in error
    jobs = {j["id"]: j for j in body["jobs"]}
    assert set(jobs) == {e["id"] for e in cron_registry.CRON_REGISTRY}
    for entry in cron_registry.CRON_REGISTRY:
        row = jobs[entry["id"]]
        assert row["origin"] == "registry_only"
        assert row["schedule"] == entry["expected"]
        assert row["state"] is None
        assert row["last_attempt"] is None
        assert row["next_time"] is None
        assert set(row) == ROW_KEYS


def test_the_outage_sentence_is_bounded(monkeypatch, as_user):
    """A pathological exception (multi-line, huge) still yields one short
    sentence — the log gets the whole thing, the panel does not."""
    as_user(CREATOR)
    noisy = "boom\nTraceback (most recent call last):\n  " + "x" * 5000

    def broken_fetch():
        raise cron_registry.SchedulerUnavailable(noisy)

    monkeypatch.setattr(cron_registry, "fetch_live_jobs", broken_fetch)
    error = client.get("/api/cron/jobs").json()["scheduler_error"]
    assert "\n" not in error
    assert len(error) < 300
