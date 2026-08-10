"""Integration tests for the GEO router (/api/geo). Fully offline.

The real geo modules run end-to-end; only the outer seams are faked: the
engine adapters and the a2 brand registry."""
from __future__ import annotations

import os

os.environ["SEO_OFFLINE"] = "1"

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import get_current_user
from final_geo_agent import geo_engines
from seo_geo_agent import insights
from final_geo_agent.geo_engines import EngineAnswer

client = TestClient(fastapi_app)

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com",
         "seeds": ["legal virtual assistant"], "enabled": True}


@pytest.fixture(autouse=True)
def _harness(monkeypatch, tmp_path):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_LOCAL_DIR", str(tmp_path / "geo_state"))
    monkeypatch.setattr(insights, "list_brands", lambda: [dict(BRAND)])
    prev = fastapi_app.dependency_overrides.get(get_current_user)
    fastapi_app.dependency_overrides[get_current_user] = lambda: {
        "id": "u1", "email": "owner@legalsoft.com", "is_admin": False,
        "is_creator": True, "session_id": "", "timezone": "UTC",
    }
    yield
    if prev is None:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    else:
        fastapi_app.dependency_overrides[get_current_user] = prev


@pytest.fixture()
def fake_engines(monkeypatch):
    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {"perplexity": True, "gemini": False, "chatgpt": False})
    monkeypatch.setattr(
        geo_engines, "poll_engine",
        lambda engine, prompt: EngineAnswer(
            engine=engine, model="fake",
            text=f"Legal Soft and Clio both handle: {prompt}",
            citations=[{"url": "https://g2.com/x", "domain": "g2.com", "title": "G2"}],
        ),
    )


def put_prompts(n=2):
    prompts = [{"id": f"p{i}", "text": f"best legal va {i}", "intent": "category",
                "stage": "consideration", "enabled": True} for i in range(1, n + 1)]
    resp = client.put(f"/api/geo/brands/{BRAND['id']}/prompts", json={"prompts": prompts})
    assert resp.status_code == 200
    return prompts


def test_geo_config_reports_engine_availability():
    resp = client.get("/api/geo/config")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["engines"]) == {"perplexity", "gemini", "chatgpt"}
    # offline + keyless: nothing should claim to be available
    assert not any(body["engines"].values())


def test_unknown_brand_404():
    assert client.get("/api/geo/brands/nope/prompts").status_code == 404
    assert client.post("/api/geo/brands/nope/poll/step", json={}).status_code == 404


def test_prompts_roundtrip_and_brand_listing():
    put_prompts(2)
    got = client.get(f"/api/geo/brands/{BRAND['id']}/prompts").json()
    assert len(got["prompts"]) == 2
    brands = client.get("/api/geo/brands").json()["brands"]
    assert brands[0]["prompts"] == 2


def test_put_prompts_empty_422():
    resp = client.put(f"/api/geo/brands/{BRAND['id']}/prompts", json={"prompts": []})
    assert resp.status_code == 422


def test_poll_step_without_keys_503():
    put_prompts(1)
    resp = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step", json={})
    assert resp.status_code == 503
    assert "Secrets" in resp.json()["detail"]


def test_poll_step_before_prompts_409(fake_engines):
    resp = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step", json={})
    assert resp.status_code == 409


def test_poll_report_answers_flow(fake_engines):
    put_prompts(2)
    resp = client.post(
        f"/api/geo/brands/{BRAND['id']}/poll/step",
        json={"runs": 1, "batch_size": 10},
    )
    assert resp.status_code == 200
    progress = resp.json()
    assert progress["done"] == progress["total"] == 2

    report = client.get(f"/api/geo/brands/{BRAND['id']}/report").json()
    assert report["blended"]["mention"]["rate"] == 1.0
    assert report["blended"]["mention"]["n_answers"] == 2
    assert report["engines"]["perplexity"]["citation"]["rate"] == 0.0
    assert report["source_gap"][0]["domain"] == "g2.com"

    answers = client.get(
        f"/api/geo/brands/{BRAND['id']}/answers", params={"engine": "perplexity"}
    ).json()
    assert answers["total"] == 2
    assert answers["answers"][0]["brand_mentioned"] is True


def test_brand_config_roundtrip():
    resp = client.put(
        f"/api/geo/brands/{BRAND['id']}/config",
        json={"competitors": [{"key": "clio", "name": "Clio", "aliases": ["Clio"]}],
              "daily_cap": 100},
    )
    assert resp.status_code == 200
    cfg = client.get(f"/api/geo/brands/{BRAND['id']}/config").json()
    assert cfg["daily_cap"] == 100
    assert cfg["competitors"][0]["key"] == "clio"
