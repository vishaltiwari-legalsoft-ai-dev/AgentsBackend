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
    # a real OPENROUTER_API_KEY in local .env must never leak into offline
    # tests — with the fallback live it would fire REAL paid engine polls
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "")
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
    assert set(body["engines"]) == {"perplexity", "gemini", "chatgpt", "aio"}
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


# ----------------------- competitor comparison + score history ----------------


def track_clio():
    resp = client.put(
        f"/api/geo/brands/{BRAND['id']}/config",
        json={"competitors": [
            {"key": "clio", "name": "Clio", "aliases": ["Clio"], "domain": "clio.com"},
        ]},
    )
    assert resp.status_code == 200


def test_comparison_scores_rivals_on_the_same_answers(fake_engines):
    track_clio()
    put_prompts(2)
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 10})

    doc = client.get(f"/api/geo/brands/{BRAND['id']}/comparison").json()
    rows = {r["key"]: r for r in doc["rows"]}
    assert doc["tracked_competitors"] == 1
    # the stub answer names both of us, us first
    assert rows["self"]["mention"]["rate"] == 1.0
    assert rows["clio"]["mention"]["rate"] == 1.0
    assert rows["self"]["avg_position"] == 1.0
    assert rows["clio"]["avg_position"] == 2.0
    # both have a domain on record and neither is cited — g2.com is
    assert rows["self"]["citation"]["rate"] == 0.0
    assert rows["clio"]["citation"]["rate"] == 0.0
    assert rows["clio"]["vs_self"]["tied"] == 2   # named in every answer we are

    # every question shows both of us, and g2 is the untracked co-citation
    assert len(doc["questions"]) == 2
    assert doc["questions"][0]["rates"]["clio"] == 1.0
    assert [d["domain"] for d in doc["untracked_domains"]] == ["g2.com"]


def test_comparison_with_nobody_tracked_says_so(fake_engines):
    put_prompts(1)
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 10})
    doc = client.get(f"/api/geo/brands/{BRAND['id']}/comparison").json()
    assert doc["tracked_competitors"] == 0
    assert [r["key"] for r in doc["rows"]] == ["self"]


def test_comparison_unknown_brand_404():
    assert client.get("/api/geo/brands/nope/comparison").status_code == 404


def test_history_banks_a_point_when_a_sweep_completes(fake_engines):
    track_clio()
    put_prompts(10)
    progress = client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                           json={"runs": 1, "batch_size": 10}).json()
    assert progress["done"] == progress["total"] == 10

    body = client.get(f"/api/geo/brands/{BRAND['id']}/history").json()
    assert len(body["points"]) == 1
    point = body["points"][0]
    assert point["source"] == "sweep"
    assert point["n_measured"] == 10
    assert point["mention_rate"] == 1.0
    assert point["competitors"]["clio"] == 1.0
    assert 0 < point["score"] <= 100
    # first measurement: no move to report, and we say that rather than "0%"
    assert body["trend"]["since_last"]["direction"] == "unknown"
    assert body["names"]["clio"] == "Clio"


def test_history_is_empty_and_honest_before_any_sweep():
    body = client.get(f"/api/geo/brands/{BRAND['id']}/history").json()
    assert body["points"] == []
    assert body["trend"]["current"] is None
    assert body["min_point_answers"] > 0


def test_history_unknown_brand_404():
    assert client.get("/api/geo/brands/nope/history").status_code == 404


def test_tracking_a_competitor_first_does_not_erase_the_brand_aliases(fake_engines):
    """Regression: PUT /config used to create the document, so a competitor
    added before anything read the config produced a config with no `self`
    aliases — and the brand then went unnamed in its own answers."""
    track_clio()
    cfg = client.get(f"/api/geo/brands/{BRAND['id']}/config").json()
    assert cfg["aliases"]["self"]
    put_prompts(1)
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 10})
    report = client.get(f"/api/geo/brands/{BRAND['id']}/report").json()
    assert report["blended"]["mention"]["rate"] == 1.0


def test_rescan_finds_a_competitor_tracked_after_the_poll(fake_engines):
    """The whole point of the rescan: a rival added today is measurable today,
    from answers already on disk, without re-billing a single engine call."""
    put_prompts(2)
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 10})

    before = client.get(f"/api/geo/brands/{BRAND['id']}/comparison").json()
    assert before["tracked_competitors"] == 0

    track_clio()   # Clio is named in the stored answers, but nobody was looking
    stale = client.get(f"/api/geo/brands/{BRAND['id']}/comparison").json()
    assert next(r for r in stale["rows"] if r["key"] == "clio")["mention"]["rate"] == 0.0

    result = client.post(f"/api/geo/brands/{BRAND['id']}/rescan", json={"days": 7}).json()
    assert result["answers_scanned"] == 2
    assert result["answers_updated"] == 2
    assert "clio" in result["entities"]

    after = client.get(f"/api/geo/brands/{BRAND['id']}/comparison").json()
    assert next(r for r in after["rows"] if r["key"] == "clio")["mention"]["rate"] == 1.0
    # and we are still measured exactly as before — a rescan is not a re-poll
    assert next(r for r in after["rows"] if r["is_self"])["mention"]["rate"] == 1.0


def test_rescan_is_creator_only():
    prev = fastapi_app.dependency_overrides[get_current_user]
    fastapi_app.dependency_overrides[get_current_user] = lambda: {
        "id": "u2", "email": "viewer@legalsoft.com", "is_admin": False,
        "is_creator": False, "session_id": "", "timezone": "UTC",
    }
    try:
        assert client.post(f"/api/geo/brands/{BRAND['id']}/rescan", json={}).status_code == 403
    finally:
        fastapi_app.dependency_overrides[get_current_user] = prev


def test_rescan_keeps_a_live_point_labelled_live(fake_engines):
    track_clio()
    put_prompts(10)
    client.post(f"/api/geo/brands/{BRAND['id']}/poll/step",
                json={"runs": 1, "batch_size": 10})
    client.post(f"/api/geo/brands/{BRAND['id']}/rescan", json={"days": 7})

    points = client.get(f"/api/geo/brands/{BRAND['id']}/history").json()["points"]
    assert len(points) == 1
    assert points[0]["source"] == "sweep"   # rebuilt, not relabelled
