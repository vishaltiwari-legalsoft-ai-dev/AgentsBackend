"""Integration tests for the SEO Blog router (/api/seo-blog). Fully offline."""

import os

os.environ["SEO_OFFLINE"] = "1"

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.security import get_current_user
from app.routers import seo_blog as blog_router

USER = {"id": "u1", "email": "t@legalsoft.com", "is_admin": False, "is_creator": False}
client = TestClient(app)

SHEET = {"keyword": "legal virtual assistant", "metrics": {"volume": 5400, "kd": 42, "traffic_potential": None},
         "serp": {"top3": [{"url": "https://a.com/post", "title": "T", "position": 1}], "paa": [], "related": [],
                  "aio_present": False},
         "competitors": [], "mixed_intent": False,
         "gap": [{"keyword": "legal virtual assistant", "tag": "main", "volume": 5400, "overlap": 1,
                  "source": "ahrefs_pasted"}],
         "usage": {"main_count_top1": 4, "target_min": 4, "target_max": 6, "frequent_terms": []},
         "lsi": [{"term": "virtual paralegal", "fit_note": "n"}],
         "data_source": "ahrefs_pasted", "degraded": []}


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_BLOG_LOCAL_DIR", str(tmp_path))
    app.dependency_overrides[get_current_user] = lambda: dict(USER)
    monkeypatch.setattr(blog_router.research, "build_research",
                        lambda keyword, pasted, **kw: dict(SHEET, keyword=keyword))
    yield
    app.dependency_overrides.pop(get_current_user, None)


def test_kickoff_creates_run_with_sheet():
    body = client.post("/api/seo-blog/runs", json={"keyword": "legal virtual assistant"}).json()
    assert body["stage"] == "research"
    assert body["sheet"]["keyword"] == "legal virtual assistant"
    assert body["gates"] == {"keywords": False, "outline": False}
    runs = client.get("/api/seo-blog/runs").json()["runs"]
    assert runs[0]["id"] == body["id"]


def test_kickoff_parses_pastes():
    payload = {"keyword": "legal virtual assistant",
               "metrics_paste": "Volume: 5.4K\nKD: 42",
               "competitor_keywords_paste": {"https://a.com/post": "Keyword,Volume\nx,100\n"}}
    body = client.post("/api/seo-blog/runs", json=payload).json()
    assert body["pasted"]["metrics"]["volume"] == 5400
    assert body["pasted"]["competitor_keywords"]["https://a.com/post"][0]["keyword"] == "x"


def test_kickoff_rejects_blank_keyword():
    assert client.post("/api/seo-blog/runs", json={"keyword": "  "}).status_code == 422


def test_kickoff_503_when_serper_missing(monkeypatch):
    from seo_geo_agent.sources import CredentialMissing

    def down(keyword, pasted, **kw):
        raise CredentialMissing("SEO_SERPER_API_KEY not set")
    monkeypatch.setattr(blog_router.research, "build_research", down)
    r = client.post("/api/seo-blog/runs", json={"keyword": "x"})
    assert r.status_code == 503
    assert "SEO_SERPER_API_KEY" in r.json()["detail"]


def test_gate1_approve_keywords():
    run = client.post("/api/seo-blog/runs", json={"keyword": "legal virtual assistant"}).json()
    edited = dict(SHEET, lsi=[{"term": "edited term", "fit_note": "writer edit"}])
    body = client.post(f"/api/seo-blog/runs/{run['id']}/approve-keywords", json={"sheet": edited}).json()
    assert body["gates"]["keywords"] is True
    assert body["stage"] == "outline"
    assert body["sheet"]["lsi"][0]["term"] == "edited term"


def test_run_404():
    assert client.get("/api/seo-blog/runs/nope").status_code == 404


def test_kickoff_409_when_same_day_run_has_progress():
    run = client.post("/api/seo-blog/runs", json={"keyword": "legal virtual assistant"}).json()
    client.post(f"/api/seo-blog/runs/{run['id']}/approve-keywords", json={"sheet": SHEET})
    r = client.post("/api/seo-blog/runs", json={"keyword": "legal virtual assistant"})
    assert r.status_code == 409
    assert "approved progress" in r.json()["detail"]


def test_kickoff_twice_without_approve_succeeds():
    run1 = client.post("/api/seo-blog/runs", json={"keyword": "legal virtual assistant"}).json()
    run2 = client.post("/api/seo-blog/runs", json={"keyword": "legal virtual assistant"}).json()
    assert run1["id"] == run2["id"]
    assert run2["stage"] == "research"
