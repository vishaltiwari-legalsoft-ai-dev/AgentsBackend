"""The GEO cron: the endpoint that replaced a half-hour browser loop.

Scheduled polling is unattended, so every way it can silently do nothing has
to be a tested behaviour rather than something a human notices weeks later in
a report that stopped moving. Offline: no engine is ever called.
"""
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.routers.geo import MIN_BRAND_SECONDS
from final_geo_agent import geo_poll
from seo_geo_agent import insights
from seo_geo_agent.sources import CredentialMissing

CRON_KEY = "test-geo-cron-key"
BRANDS = [
    {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com", "enabled": True},
    {"id": "paused", "name": "Paused Co", "domain": "paused.com", "enabled": False},
]


@pytest.fixture()
def client():
    return TestClient(fastapi_app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_LOCAL_DIR", str(tmp_path))
    monkeypatch.setenv("GEO_CRON_KEY", CRON_KEY)
    monkeypatch.delenv("GEO_CRON_BUDGET_SECONDS", raising=False)
    monkeypatch.setattr(insights, "list_brands", lambda: list(BRANDS))
    # the sweep itself is proven in the agent's own tests; here we care about
    # who gets swept, and what the scheduler is told
    monkeypatch.setattr("app.routers.geo.run_tracking.record_activity",
                        lambda *a, **k: None)


def _sweeps(monkeypatch, calls: list) -> None:
    def fake(brand, **kwargs):
        calls.append((brand["id"], kwargs.get("budget_seconds")))
        return {"brand_id": brand["id"], "steps": 3, "done": 9, "total": 9,
                "completed": True, "stopped_because": "completed"}

    monkeypatch.setattr(geo_poll, "poll_until_done", fake)


# ------------------------------------------------------------------ auth ----

def test_cron_rejects_a_missing_or_wrong_key(client):
    assert client.post("/api/geo/cron/poll").status_code == 403
    assert client.post("/api/geo/cron/poll", headers={"x-cron-key": "nope"}).status_code == 403


def test_cron_refuses_to_run_when_no_key_is_configured(client, monkeypatch):
    # an unset key must fail closed, not authenticate everybody
    monkeypatch.delenv("GEO_CRON_KEY", raising=False)
    assert client.post("/api/geo/cron/poll", headers={"x-cron-key": ""}).status_code == 503


# --------------------------------------------------------------- sweeping ----

def test_cron_sweeps_due_brands_and_skips_disabled_ones(client, monkeypatch):
    calls: list = []
    _sweeps(monkeypatch, calls)
    monkeypatch.setattr(geo_poll, "ensure_config", lambda brand: {})

    res = client.post("/api/geo/cron/poll", headers={"x-cron-key": CRON_KEY})

    assert res.status_code == 200
    assert [c[0] for c in calls] == ["legalsoft"]      # the disabled brand never runs
    assert res.json()["ok"] == 1


def test_cron_skips_a_brand_that_is_not_due_yet(client, monkeypatch):
    calls: list = []
    _sweeps(monkeypatch, calls)
    cfg = {"last_poll_completed_at": "2999-01-01T00:00:00+00:00", "poll_interval_days": 2}
    monkeypatch.setattr(geo_poll, "ensure_config", lambda brand: cfg)

    body = client.post("/api/geo/cron/poll", headers={"x-cron-key": CRON_KEY}).json()

    assert calls == []                                  # nothing polled, nothing billed
    assert "legalsoft" in body["skipped"]
    assert body["ok"] == 0 and body["failed"] == 0
    assert body["status"] == "ok"                       # not due is not a failure


def test_a_brand_without_engine_keys_is_skipped_not_failed(client, monkeypatch):
    def boom(brand, **kwargs):
        raise CredentialMissing("No engine keys configured")

    monkeypatch.setattr(geo_poll, "ensure_config", lambda brand: {})
    monkeypatch.setattr(geo_poll, "poll_until_done", boom)

    res = client.post("/api/geo/cron/poll", headers={"x-cron-key": CRON_KEY})

    # 200, deliberately: a missing key is a config fact. A 5xx here would put
    # Cloud Scheduler into a retry loop that can never succeed.
    assert res.status_code == 200
    assert "not configured" in res.json()["skipped"]["legalsoft"]


# ------------------------------------------------------- honest status ----

def test_every_brand_failing_reports_502_to_the_scheduler(client, monkeypatch):
    def boom(brand, **kwargs):
        raise RuntimeError("firestore unreachable")

    monkeypatch.setattr(geo_poll, "ensure_config", lambda brand: {})
    monkeypatch.setattr(geo_poll, "poll_until_done", boom)

    res = client.post("/api/geo/cron/poll", headers={"x-cron-key": CRON_KEY})

    assert res.status_code == 502
    assert res.json()["status"] == "failed"


def test_budget_is_env_tunable_and_clamped(client, monkeypatch):
    calls: list = []
    _sweeps(monkeypatch, calls)
    monkeypatch.setattr(geo_poll, "ensure_config", lambda brand: {})
    monkeypatch.setenv("GEO_CRON_BUDGET_SECONDS", "99999")

    client.post("/api/geo/cron/poll", headers={"x-cron-key": CRON_KEY})

    # clamped, never an unbounded request. The brand gets what is LEFT of the
    # fire's budget, so this is "at most the ceiling", not "exactly" it.
    assert 1700.0 < calls[0][1] <= 1800.0


def test_a_junk_budget_falls_back_to_the_default(client, monkeypatch):
    calls: list = []
    _sweeps(monkeypatch, calls)
    monkeypatch.setattr(geo_poll, "ensure_config", lambda brand: {})
    monkeypatch.setenv("GEO_CRON_BUDGET_SECONDS", "soon")

    client.post("/api/geo/cron/poll", headers={"x-cron-key": CRON_KEY})

    default = geo_poll.DEFAULT_CRON_BUDGET_SECONDS
    assert default - 10 < calls[0][1] <= default


# --------------------------------------------------- one request, one budget
# Cloud Run kills a request at its timeout. A per-brand slice multiplied by an
# unknown brand count is a config that breaks when someone adds a brand, so the
# budget covers the whole fire and the stalest brand gets the time.

THREE = [
    {"id": "fresh", "name": "Fresh", "domain": "a.com", "enabled": True},
    {"id": "stale", "name": "Stale", "domain": "b.com", "enabled": True},
    {"id": "never", "name": "Never", "domain": "c.com", "enabled": True},
]

CFGS = {
    "fresh": {"last_poll_completed_at": "2026-08-17T00:00:00+00:00", "poll_interval_days": 1},
    "stale": {"last_poll_completed_at": "2026-08-01T00:00:00+00:00", "poll_interval_days": 1},
    "never": {},
}


@pytest.fixture()
def three_brands(monkeypatch):
    monkeypatch.setattr(insights, "list_brands", lambda: list(THREE))
    monkeypatch.setattr(geo_poll, "ensure_config", lambda brand: CFGS[brand["id"]])


def test_brands_are_swept_stalest_first(client, monkeypatch, three_brands):
    order: list[str] = []
    monkeypatch.setattr(geo_poll, "poll_until_done", lambda brand, **k: (
        order.append(brand["id"]) or {"brand_id": brand["id"], "done": 9, "total": 9, "completed": True}))

    client.post("/api/geo/cron/poll", headers={"x-cron-key": CRON_KEY})

    # never-polled outranks everything; between two polled brands the older one wins
    assert order == ["never", "stale", "fresh"]


def test_each_brand_gets_what_is_left_not_a_fixed_slice(client, monkeypatch, three_brands):
    budgets: list[float] = []

    def slow(brand, **kwargs):
        budgets.append(kwargs["budget_seconds"])
        return {"brand_id": brand["id"], "done": 9, "total": 9, "completed": True}

    monkeypatch.setattr(geo_poll, "poll_until_done", slow)
    monkeypatch.setenv("GEO_CRON_BUDGET_SECONDS", "300")

    client.post("/api/geo/cron/poll", headers={"x-cron-key": CRON_KEY})

    # three brands must not be promised 300s each — that is 900s inside a request
    # the platform kills at its timeout
    assert budgets[0] <= 300
    assert budgets == sorted(budgets, reverse=True)
    assert sum(budgets) < 900


def test_brands_the_clock_never_reached_are_named_not_silently_skipped(
    client, monkeypatch, three_brands
):
    # budget leaves room for exactly one brand: the floor is MIN_BRAND_SECONDS,
    # and the first brand burns just enough wall clock to drop below it
    monkeypatch.setenv("GEO_CRON_BUDGET_SECONDS", str(MIN_BRAND_SECONDS + 0.05))

    def burn_the_clock(brand, **kwargs):
        time.sleep(0.1)
        return {"brand_id": brand["id"], "done": 1, "total": 9, "completed": False}

    monkeypatch.setattr(geo_poll, "poll_until_done", burn_the_clock)

    body = client.post("/api/geo/cron/poll", headers={"x-cron-key": CRON_KEY}).json()

    # the stalest brand got the time; the other two are named, and stay due, so
    # a response listing only successes can never read as "everything is fine"
    assert list(body["brands"]) == ["never"]
    assert sorted(body["unreached"]) == ["fresh", "stale"]


def test_a_brand_with_unreadable_config_is_skipped_not_failed(client, monkeypatch):
    monkeypatch.setattr(insights, "list_brands", lambda: [THREE[0]])

    def boom(brand):
        raise RuntimeError("firestore unreachable")

    monkeypatch.setattr(geo_poll, "ensure_config", boom)

    res = client.post("/api/geo/cron/poll", headers={"x-cron-key": CRON_KEY})

    assert res.status_code == 200
    assert "config unreadable" in res.json()["skipped"]["fresh"]
