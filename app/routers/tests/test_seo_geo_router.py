"""Integration tests for the SEO agent router (/api/seo-geo). Fully offline."""

import os

os.environ["SEO_OFFLINE"] = "1"

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.security import get_current_user

USER = {"id": "u1", "email": "t@legalsoft.com", "is_admin": False, "is_creator": False}
CREATOR = {**USER, "is_creator": True}

client = TestClient(app)


@pytest.fixture(autouse=True)
def _offline_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_LOCAL_DIR", str(tmp_path))
    monkeypatch.delenv("SEO_CRON_KEY", raising=False)
    app.dependency_overrides[get_current_user] = lambda: dict(USER)
    yield
    app.dependency_overrides.pop(get_current_user, None)


def as_creator():
    app.dependency_overrides[get_current_user] = lambda: dict(CREATOR)


def test_overview_lists_default_brand_and_sources():
    body = client.get("/api/seo-geo/overview").json()
    assert body["sources"] == {"gsc": False, "serp": False}
    assert body["brands"][0]["brand"]["id"] == "legalsoft"
    assert body["brands"][0]["last_run"] is None


def test_brand_create_requires_creator():
    payload = {"name": "Acme", "domain": "acme.com"}
    assert client.post("/api/seo-geo/brands", json=payload).status_code == 403
    as_creator()
    body = client.post("/api/seo-geo/brands", json=payload).json()
    assert any(b["id"] == "acme" and b["gsc_property"] == "sc-domain:acme.com" for b in body["brands"])


def test_brand_domain_validation():
    as_creator()
    r = client.post("/api/seo-geo/brands", json={"name": "Bad", "domain": "not-a-domain"})
    assert r.status_code == 422


def test_run_and_detail_roundtrip():
    r = client.post("/api/seo-geo/run/legalsoft")
    assert r.status_code == 200, r.text
    assert r.json()["degraded"]  # offline: no Search Console
    detail = client.get("/api/seo-geo/brands/legalsoft").json()
    assert detail["run"]["trigger"] == "manual:t@legalsoft.com"


def test_unknown_brand_404():
    assert client.post("/api/seo-geo/run/nope").status_code == 404
    assert client.get("/api/seo-geo/brands/nope").status_code == 404


def test_todo_status_update_validates():
    assert client.post("/api/seo-geo/todos/legalsoft/abc123", json={"status": "later"}).status_code == 422
    r = client.post("/api/seo-geo/todos/legalsoft/abc123", json={"status": "assigned"})
    assert r.json() == {"id": "abc123", "status": "assigned"}


def test_keyword_lab_offline_runs_heuristic():
    r = client.post("/api/seo-geo/keywords/legalsoft/run")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["clusters"] and body["degraded"]
    assert client.get("/api/seo-geo/keywords/legalsoft").json()["lab"]["brand_id"] == "legalsoft"


def test_competitors_flow():
    assert client.get("/api/seo-geo/competitors/legalsoft").json()["tracked"] == []
    assert client.put("/api/seo-geo/competitors/legalsoft", json={"domains": ["comp.com"]}).status_code == 403
    as_creator()
    r = client.put("/api/seo-geo/competitors/legalsoft", json={"domains": [" Comp.com "]})
    assert r.json()["tracked"] == ["comp.com"]
    t = client.post("/api/seo-geo/competitors/legalsoft/track")
    assert t.status_code == 200 and t.json()["degraded"]  # offline: no Serper, no fetches


def test_live_analysis_endpoints_offline_503():
    assert client.post("/api/seo-geo/serp/legalsoft", json={"query": "x"}).status_code == 503
    assert client.post("/api/seo-geo/briefs/legalsoft", json={"keyword": "x"}).status_code == 503
    assert client.post("/api/seo-geo/audit/legalsoft/run").status_code == 503
    assert client.get("/api/seo-geo/briefs/legalsoft").json()["briefs"] == []
    assert client.get("/api/seo-geo/audit/legalsoft").json()["report"] is None


def test_draft_score_endpoint():
    r = client.post("/api/seo-geo/draft-score/legalsoft",
                    json={"text": "Buy now.", "keyword": "legal virtual assistant"})
    assert r.status_code == 200
    assert r.json()["verdict"] == "rework"


def test_cron_inert_without_key_then_gated(monkeypatch):
    assert client.post("/api/seo-geo/cron/run").status_code == 503
    monkeypatch.setenv("SEO_CRON_KEY", "s3cret")
    assert client.post("/api/seo-geo/cron/run", headers={"x-cron-key": "wrong"}).status_code == 403
    body = client.post("/api/seo-geo/cron/run", headers={"x-cron-key": "s3cret"}).json()
    assert body["brands"]["legalsoft"]["ok"] is True
