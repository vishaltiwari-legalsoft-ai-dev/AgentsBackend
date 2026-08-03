"""Integration tests for the Blog Writer router (/api/blog). Fully offline.

The real engine modules run end-to-end; only the outer seams are faked:
Serper search, page fetch, sitemap, and the a9 LLM adapter.
"""
from __future__ import annotations

import os

os.environ["BLOG_OFFLINE"] = "1"

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import get_current_user
from blog_writer_agent import llm as bw_llm
from seo_geo_agent import insights, sources

client = TestClient(fastapi_app)

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com", "enabled": True}


@pytest.fixture(autouse=True)
def _harness(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOG_OFFLINE", "1")
    monkeypatch.setenv("BLOG_LOCAL_DIR", str(tmp_path / "blog_state"))
    monkeypatch.delenv("SEO_SERPER_API_KEY", raising=False)
    monkeypatch.setattr(insights, "list_brands", lambda: [dict(BRAND)])
    prev = fastapi_app.dependency_overrides.get(get_current_user)
    fastapi_app.dependency_overrides[get_current_user] = lambda: {
        "id": "u1", "email": "writer@legalsoft.com", "is_admin": False,
        "is_creator": False, "session_id": "", "timezone": "UTC",
    }
    yield
    if prev is None:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    else:
        fastapi_app.dependency_overrides[get_current_user] = prev


@pytest.fixture()
def live_seams(monkeypatch):
    """Fake Serper + fetch + LLM so the full pipeline runs offline."""
    monkeypatch.setattr(sources, "serper_available", lambda: True)
    monkeypatch.setattr(
        sources, "serper_search",
        lambda q, client=None: {"organic": [{"link": f"https://src.example/{abs(hash(q)) % 1000}", "title": q}]},
    )
    monkeypatch.setattr(
        sources, "fetch_text",
        lambda u, client=None: {"url": u, "title": "Source", "text": f"body of {u}", "status": 200},
    )

    def scripted_llm(system, prompt, **kw):
        s = system.lower()
        if "extract evidence" in s:
            return {"evidence": [{
                "claim": f"claim from {prompt.splitlines()[1][-24:]}", "quote": "verbatim",
                "url": next(l for l in prompt.splitlines() if l.startswith("Page URL: ")).removeprefix("Page URL: "),
                "source_name": "Source", "source_class": "studies", "date": "", "credibility": "report",
            }]}
        if "gap analysis" in s:
            return {"gaps": ["deeper stats needed"]}
        if "classif" in s:
            return {"action": "rewrite", "queries": []}
        if "rewrite one block" in s:
            return {"text": "Rewritten per the comment.", "cites": ["ev-1"]}
        if "art director" in s:
            return {"visuals": [{"section": "(top)", "type": "hero", "theme": "navy",
                                 "prompt": "Hero image of a calm reception", "rationale": "tone"}]}
        if "editorial writer" in s:
            return {
                "meta": {"title": "The Post", "description": "d", "slug": "the-post"},
                "blocks": [
                    {"kind": "intro", "heading": "", "text": "Opening.", "cites": ["ev-1"]},
                    {"kind": "section", "heading": "Body", "text": "Evidence-backed body.", "cites": ["ev-1"]},
                ],
                "internal_links": [],
            }
        raise AssertionError(f"unexpected llm call: {system[:60]}")

    monkeypatch.setattr(bw_llm, "llm_json", scripted_llm)


def _create_run() -> dict:
    return client.post("/api/blog/runs", json={"brand_id": "legalsoft", "topic": "virtual receptionists"}).json()


def test_brand_catalogue_lists_enabled_brands():
    body = client.get("/api/blog/brands").json()
    assert body["brands"][0]["id"] == "legalsoft"
    assert body["brands"][0]["inventory"] is None


def test_inventory_scan_and_readback(monkeypatch, live_seams):
    monkeypatch.setattr(
        sources, "fetch_sitemap",
        lambda d, client=None: ["https://legalsoft.com/blog/one/", "https://legalsoft.com/pricing"],
    )
    monkeypatch.setattr(
        sources, "fetch_page",
        lambda u, client=None: type("P", (), {"url": u, "title": "One Post"})(),
    )
    assert client.get("/api/blog/brands/legalsoft/inventory").status_code == 404
    scanned = client.post("/api/blog/brands/legalsoft/inventory").json()
    assert [p["title"] for p in scanned["posts"]] == ["One Post"]
    assert client.get("/api/blog/brands/legalsoft/inventory").json()["posts"] == scanned["posts"]
    assert client.get("/api/blog/brands/nope/inventory").status_code == 404


def test_create_run_validation():
    assert client.post("/api/blog/runs", json={"brand_id": "legalsoft", "topic": "  "}).status_code == 422
    assert client.post("/api/blog/runs", json={"brand_id": "nope", "topic": "x"}).status_code == 404
    run = _create_run()
    assert run["status"] == "research"
    assert client.get("/api/blog/runs").json()["runs"][0]["id"] == run["id"]
    assert client.get(f"/api/blog/runs/{run['id']}").json()["topic"] == "virtual receptionists"
    assert client.get("/api/blog/runs/nope").status_code == 404


def test_research_step_without_key_is_an_honest_424():
    run = _create_run()
    r = client.post(f"/api/blog/runs/{run['id']}/research/step")
    assert r.status_code == 424
    assert "SEO_SERPER_API_KEY" in r.json()["detail"]


def test_full_desk_flow(live_seams):
    run = _create_run()
    rid = run["id"]

    stepped = client.post(f"/api/blog/runs/{rid}/research/step").json()
    assert stepped["ledger"] and stepped["rounds"][0]["added"] >= 1

    drafted = client.post(f"/api/blog/runs/{rid}/draft").json()
    assert [b["id"] for b in drafted["draft"]["blocks"]] == ["b1", "b2"]

    revised = client.post(f"/api/blog/runs/{rid}/blocks/b1/comment", json={"comment": "punchier"}).json()
    assert revised["draft"]["blocks"][0]["text"] == "Rewritten per the comment."
    assert client.post(f"/api/blog/runs/{rid}/blocks/b99/comment", json={"comment": "x"}).status_code == 404

    planned = client.post(f"/api/blog/runs/{rid}/visuals").json()
    assert planned["visuals"]["items"][0]["type"] == "hero"

    md = client.get(f"/api/blog/runs/{rid}/export?format=md")
    assert md.status_code == 200 and md.text.startswith("# The Post")
    assert 'filename="the-post.md"' in md.headers["content-disposition"]
    html = client.get(f"/api/blog/runs/{rid}/export?format=html")
    assert "<!doctype html>" in html.text
    txt = client.get(f"/api/blog/runs/{rid}/export?format=txt")
    assert "THE POST" in txt.text
    vmd = client.get(f"/api/blog/runs/{rid}/export?format=visuals-md")
    assert "Hero image" in vmd.text
    assert client.get(f"/api/blog/runs/{rid}/export?format=nope").status_code == 422


def test_stage_order_guards(live_seams):
    run = _create_run()
    rid = run["id"]
    assert client.post(f"/api/blog/runs/{rid}/draft").status_code == 409
    assert client.post(f"/api/blog/runs/{rid}/blocks/b1/comment", json={"comment": "x"}).status_code == 409
    assert client.post(f"/api/blog/runs/{rid}/visuals").status_code == 409
    assert client.get(f"/api/blog/runs/{rid}/export?format=md").status_code == 404
    assert client.get(f"/api/blog/runs/{rid}/export?format=visuals-md").status_code == 404
