"""The GEO cron: the endpoint that replaced a half-hour browser loop.

Scheduled polling is unattended, so every way it can silently do nothing has
to be a tested behaviour rather than something a human notices weeks later in
a report that stopped moving. Offline: no engine is ever called.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
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

    assert calls[0][1] == 1800.0        # clamped, never an unbounded request


def test_a_junk_budget_falls_back_to_the_default(client, monkeypatch):
    calls: list = []
    _sweeps(monkeypatch, calls)
    monkeypatch.setattr(geo_poll, "ensure_config", lambda brand: {})
    monkeypatch.setenv("GEO_CRON_BUDGET_SECONDS", "soon")

    client.post("/api/geo/cron/poll", headers={"x-cron-key": CRON_KEY})

    assert calls[0][1] == geo_poll.DEFAULT_CRON_BUDGET_SECONDS
