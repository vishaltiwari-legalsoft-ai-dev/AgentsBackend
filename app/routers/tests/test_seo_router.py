"""Integration tests for the SEO router (/api/seo). Fully offline."""

import contextlib
import os

os.environ["SEO_OFFLINE"] = "1"

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.security import get_current_user

app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "email": "t@legalsoft.com"}
client = TestClient(app)


@contextlib.contextmanager
def _as_user(user: dict):
    """Temporarily override get_current_user for one test, restoring whatever
    override (if any) was previously in place — ``app.dependency_overrides``
    is a single shared dict on the app singleton, mutated at module level by
    several test files, so a test must never clobber it with a hardcoded
    value on exit (that would silently break other files' tests depending on
    collection/run order)."""
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        yield
    finally:
        if previous is not None:
            app.dependency_overrides[get_current_user] = previous
        else:
            app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def _runs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_RUNS_DIR", str(tmp_path))
    # load_config()'s TTL cache (I5) is module-global; each test gets a fresh
    # SEO_RUNS_DIR, so a cached value from another test must not leak in.
    from seo_agent import store

    store._config_cache = None
    yield
    store._config_cache = None


def _seed_benchmark() -> str:
    from seo_agent import store

    bid = store.new_id()
    store.save_benchmark({
        "id": bid, "keyword": "legal intake specialist", "location": "US",
        "brand": "legalsoft", "created_at": "2026-07-20T00:00:00Z",
        "serp_fetched_at": "2026-07-20T00:00:00Z",
        "term_targets": [{"term": "intake", "weight": 1.0, "min_count": 2, "max_count": 4}],
        "topics": [{"name": "Role", "terms": ["intake"], "questions": []}],
        "questions": ["What does an intake specialist do?"],
        "word_count_range": [20, 60], "heading_count_range": [1, 4],
        "paa_questions": [], "source_pages": [], "excluded": [],
        "topics_ai": True, "topics_fallback_reason": None,
    })
    return bid


def test_benchmarks_list_and_detail():
    bid = _seed_benchmark()
    listing = client.get("/api/seo/benchmarks").json()
    assert listing["benchmarks"][0]["id"] == bid
    detail = client.get(f"/api/seo/benchmarks/{bid}").json()
    assert detail["keyword"] == "legal intake specialist"
    assert client.get("/api/seo/benchmarks/missing").status_code == 404


def test_score_endpoint_scores_draft_against_benchmark():
    bid = _seed_benchmark()
    body = {"benchmark_id": bid,
            "draft_text": "# What does an intake specialist do?\nintake intake calls " + "w " * 30}
    report = client.post("/api/seo/score", json=body).json()
    assert 0 <= report["score"] <= 100
    assert "missing_terms" in report and "questions_unanswered" in report
    assert client.post("/api/seo/score",
                       json={"benchmark_id": "missing", "draft_text": "x"}).status_code == 404


def test_analyze_endpoint_maps_analysis_error_to_502(monkeypatch):
    from seo_agent.analyze import AnalysisError

    def boom(**kwargs):
        raise AnalysisError("SERP fetch failed: no key")

    monkeypatch.setattr("app.routers.seo.run_analysis", lambda *a, **kw: boom())
    response = client.post("/api/seo/benchmarks",
                           json={"keyword": "kw", "location": "US", "brand": "b"})
    assert response.status_code == 502
    assert "SERP fetch failed" in response.json()["detail"]


def test_brief_endpoint():
    bid = _seed_benchmark()
    brief = client.get(f"/api/seo/briefs/{bid}").json()
    assert brief["keyword"] == "legal intake specialist"
    assert brief["topics_ai"] is True


def test_geo_overview_and_run_detail():
    from seo_agent import store

    store.save_geo_run({
        "id": "g1", "brand": "legalsoft", "week": "2026-W30",
        "captured_at": "2026-07-20T00:00:00Z", "answers": [], "score": 7.5,
        "components": {"mention": 0.8}, "engine_scores": {"gpt": 7.5},
        "no_data_engines": ["gemini"],
    })
    overview = client.get("/api/seo/geo/overview").json()
    assert overview["brands"][0]["brand"] == "legalsoft"
    assert overview["brands"][0]["latest"]["score"] == 7.5
    assert overview["brands"][0]["history"][0]["week"] == "2026-W30"
    run = client.get("/api/seo/geo/runs/g1").json()
    assert run["no_data_engines"] == ["gemini"]


def test_geo_capture_valid_cron_key_no_session_returns_200(monkeypatch):
    monkeypatch.setenv("SEO_CRON_KEY", "shhh")
    monkeypatch.setattr("app.routers.seo.run_geo_capture", lambda: [])
    response = client.post("/api/seo/geo/capture", headers={"X-Cron-Key": "shhh"})
    assert response.status_code == 200
    assert response.json() == {"runs": []}


def test_geo_capture_wrong_cron_key_no_session_returns_401(monkeypatch):
    monkeypatch.setenv("SEO_CRON_KEY", "shhh")
    monkeypatch.setattr("app.routers.seo.run_geo_capture", lambda: [])
    response = client.post("/api/seo/geo/capture", headers={"X-Cron-Key": "wrong"})
    assert response.status_code == 401


def test_geo_capture_no_cron_key_valid_session_returns_200(monkeypatch):
    monkeypatch.delenv("SEO_CRON_KEY", raising=False)
    monkeypatch.setattr("app.routers.seo.run_geo_capture", lambda: [])
    monkeypatch.setattr(
        "app.routers.seo.get_current_user",
        lambda credentials: {"id": "u1", "email": "t@legalsoft.com"},
    )
    response = client.post(
        "/api/seo/geo/capture", headers={"Authorization": "Bearer faketoken"}
    )
    assert response.status_code == 200
    assert response.json() == {"runs": []}


def test_geo_capture_nothing_returns_401(monkeypatch):
    monkeypatch.delenv("SEO_CRON_KEY", raising=False)
    monkeypatch.setattr("app.routers.seo.run_geo_capture", lambda: [])
    response = client.post("/api/seo/geo/capture")
    assert response.status_code == 401


def test_config_get_and_put_reject_non_admin():
    with _as_user({"id": "u1", "email": "t@legalsoft.com"}):
        assert client.get("/api/seo/config").status_code == 403
        assert client.put("/api/seo/config", json={"w_term_coverage": 0.5}).status_code == 403


def test_config_roundtrip_as_admin():
    with _as_user({"id": "a1", "email": "admin@legalsoft.com", "is_admin": True}):
        got = client.get("/api/seo/config").json()
        assert got["w_term_coverage"] == 0.40          # defaults visible
        client.put("/api/seo/config", json={"w_term_coverage": 0.5,
                                            "brands": {"x": {"name": "X", "domain": "x.com"}}})
        assert client.get("/api/seo/config").json()["w_term_coverage"] == 0.5


def test_score_response_includes_surfer_fields():
    bid = _seed_benchmark()
    body = {"benchmark_id": bid, "draft_text": "intake " + "w " * 30}
    r = client.post("/api/seo/score", json=body).json()
    assert {row["term"] for row in r["term_report"]} == {"intake"}
    assert r["term_report"][0]["status"] in ("missing", "low", "ok", "overused")
    assert r["structure"]["word_count_range"] == [20, 60]
    assert isinstance(r["topic_coverage"], list)
