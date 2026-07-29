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


# Stage 2-3 endpoints

OUTLINE_DOC = {"competitor_outlines": [], "meta": {"title": "T", "description": "D", "slug": "s"},
               "targets": {"word_count": 40, "links": 1},
               "outline": [{"heading": "Costs", "level": 2, "note": "", "keywords": []}],
               "evaluator": {"rounds": 1, "beats_all": True, "scores": {}, "note": ""}, "degraded": []}
CITS = {"items": [{"id": "c1", "claim": "x", "source_name": "Clio 2025 Legal Trends Report",
                   "url": "https://clio.com/report", "domain": "clio.com", "dr": None,
                   "dr_status": "unverified", "section": "Costs", "verified": True}],
        "short_by": 0, "rounds": 1, "degraded": []}
DRAFT_MD = ("# T\n\n## Costs\n\nlegal virtual assistant costs, and again legal virtual assistant, "
            "with virtual paralegal help per [Clio 2025 Legal Trends Report](https://clio.com/report). "
            + "word " * 20)


def _stage2_ready(monkeypatch):
    run = client.post("/api/seo-blog/runs", json={"keyword": "legal virtual assistant"}).json()
    client.post(f"/api/seo-blog/runs/{run['id']}/approve-keywords", json={"sheet": SHEET})
    monkeypatch.setattr(blog_router.outline, "competitor_profile", lambda url, **kw: {"url": url, "degraded": []})
    monkeypatch.setattr(blog_router.outline, "build_outline", lambda sheet, profiles, **kw: dict(OUTLINE_DOC))
    monkeypatch.setattr(blog_router.citations, "source_citations", lambda od, dr, **kw: dict(CITS))
    return run["id"]


def test_build_outline_requires_gate1(monkeypatch):
    run = client.post("/api/seo-blog/runs", json={"keyword": "x"}).json()
    assert client.post(f"/api/seo-blog/runs/{run['id']}/build-outline").status_code == 409


def test_build_outline_then_gate2(monkeypatch):
    rid = _stage2_ready(monkeypatch)
    body = client.post(f"/api/seo-blog/runs/{rid}/build-outline").json()
    assert body["outline_doc"]["meta"]["slug"] == "s"
    assert body["citations"]["items"][0]["dr_status"] == "unverified"
    edited = [{"heading": "Costs (edited)", "level": 2, "note": "", "keywords": []}]
    body = client.post(f"/api/seo-blog/runs/{rid}/approve-outline", json={"outline": edited}).json()
    assert body["gates"]["outline"] is True and body["stage"] == "draft"
    assert body["outline_doc"]["outline"][0]["heading"] == "Costs (edited)"


def test_vet_citations_applies_dr(monkeypatch):
    rid = _stage2_ready(monkeypatch)
    client.post(f"/api/seo-blog/runs/{rid}/build-outline")
    body = client.post(f"/api/seo-blog/runs/{rid}/vet-citations", json={"dr_paste": "clio.com 91"}).json()
    assert body["citations"]["items"][0]["dr"] == 91
    assert body["pasted"]["dr"] == {"clio.com": 91}


def test_draft_flow_and_export(monkeypatch):
    rid = _stage2_ready(monkeypatch)
    client.post(f"/api/seo-blog/runs/{rid}/build-outline")
    client.post(f"/api/seo-blog/runs/{rid}/approve-outline",
                json={"outline": OUTLINE_DOC["outline"]})
    monkeypatch.setattr(blog_router.drafting, "build_draft",
                        lambda s, o, c, **kw: {"markdown": DRAFT_MD, "meta": OUTLINE_DOC["meta"],
                                               "compliance": blog_router.drafting.check_compliance(
                                                   DRAFT_MD, SHEET, OUTLINE_DOC, CITS),
                                               "edited": False})
    body = client.post(f"/api/seo-blog/runs/{rid}/draft").json()
    assert body["draft"]["markdown"].startswith("# T")
    patched = client.patch(f"/api/seo-blog/runs/{rid}/draft",
                           json={"markdown": DRAFT_MD + " more"}).json()
    assert patched["draft"]["edited"] is True
    md = client.get(f"/api/seo-blog/runs/{rid}/export?format=md")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
    docx = client.get(f"/api/seo-blog/runs/{rid}/export?format=docx")
    assert docx.status_code == 200 and len(docx.content) > 1000


def test_draft_requires_both_gates(monkeypatch):
    rid = _stage2_ready(monkeypatch)
    assert client.post(f"/api/seo-blog/runs/{rid}/draft").status_code == 409
    assert client.get(f"/api/seo-blog/runs/{rid}/export?format=md").status_code == 404
