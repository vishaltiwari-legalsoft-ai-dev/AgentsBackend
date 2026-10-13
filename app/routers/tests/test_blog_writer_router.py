"""Integration tests for the Blog Writer router (/api/blog). Fully offline.

The real engine modules run end-to-end; only the outer seams are faked:
Serper search, page fetch, sitemap, and the a9 LLM adapter.
"""
from __future__ import annotations

import logging
import os
from types import SimpleNamespace

os.environ["BLOG_OFFLINE"] = "1"

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest

from app.routers.tests.conftest import client
from app.services import firestore_repo
from blog_writer_agent import llm as bw_llm
from blog_writer_agent import research
from seo_geo_agent import insights, sources

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com", "enabled": True}

#: A plain writer — nothing in this router is creator-gated.
WRITER = {"id": "u1", "email": "writer@legalsoft.com", "is_admin": False,
          "is_creator": False, "session_id": "", "timezone": "UTC"}

#: An admin who is not a creator — admin alone is what unlocks every row.
ADMIN = {"id": "admin-9", "email": "admin@legalsoft.com", "is_admin": True,
         "is_creator": False, "session_id": "", "timezone": "UTC"}


@pytest.fixture(autouse=True)
def _harness(monkeypatch, tmp_path, as_caller):
    monkeypatch.setenv("BLOG_OFFLINE", "1")
    monkeypatch.setenv("BLOG_LOCAL_DIR", str(tmp_path / "blog_state"))
    monkeypatch.setenv("SEO_OFFLINE", "1")  # keeps the serper app-config fallback off Firestore
    monkeypatch.delenv("SEO_SERPER_API_KEY", raising=False)
    monkeypatch.setattr(insights, "list_brands", lambda: [dict(BRAND)])
    as_caller(WRITER)


@pytest.fixture()
def live_seams(monkeypatch):
    """Fake Serper + fetch + LLM so the full pipeline runs offline."""
    monkeypatch.setattr(sources, "serper_available", lambda: True)
    monkeypatch.setattr(
        sources, "serper_search",
        lambda q, client=None: {"organic": [{"link": f"https://src.example/{abs(hash(q)) % 1000}", "title": q}]},
    )
    monkeypatch.setattr(
        sources, "fetch_page",
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
        if "house line editor" in s:
            return {"meta": {}, "blocks": [
                {"id": "b1", "heading": "", "text": "Opening, line-edited.", "cites": ["ev-1"]},
                {"id": "b2", "heading": "Body", "text": "Evidence-backed body, line-edited.", "cites": ["ev-1"]},
            ]}
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


def test_voice_endpoints(monkeypatch, live_seams):
    monkeypatch.setattr(
        sources, "fetch_sitemap",
        lambda d, client=None: ["https://legalsoft.com/blog/one/"],
    )
    monkeypatch.setattr(
        sources, "fetch_page",
        lambda u, client=None: {"url": u, "title": "One Post", "text": "How we write.", "status": 200},
    )
    from blog_writer_agent import llm as _bw_llm

    real_scripted = _bw_llm.llm_json

    def with_voice(system, prompt, **kw):
        if "voice analyst" in system.lower():
            return {"tone": "direct", "summary": "Practical posts for law firms."}
        return real_scripted(system, prompt, **kw)

    monkeypatch.setattr(_bw_llm, "llm_json", with_voice)

    assert client.get("/api/blog/brands/legalsoft/voice").status_code == 404
    assert client.post("/api/blog/brands/legalsoft/voice").status_code == 409  # no inventory yet
    client.post("/api/blog/brands/legalsoft/inventory")
    studied = client.post("/api/blog/brands/legalsoft/voice").json()
    assert studied["count"] == 1
    assert studied["profile"]["tone"] == "direct"
    assert client.get("/api/blog/brands/legalsoft/voice").json()["profile"]["summary"]
    assert client.get("/api/blog/brands").json()["brands"][0]["voice"]["count"] == 1


def test_create_run_validation():
    assert client.post("/api/blog/runs", json={"brand_id": "legalsoft", "topic": "  "}).status_code == 422
    assert client.post("/api/blog/runs", json={"brand_id": "nope", "topic": "x"}).status_code == 404
    run = _create_run()
    assert run["status"] == "research"
    assert client.get("/api/blog/runs").json()["runs"][0]["id"] == run["id"]
    assert client.get(f"/api/blog/runs/{run['id']}").json()["topic"] == "virtual receptionists"
    assert client.get("/api/blog/runs/nope").status_code == 404


def _stored_run(rid: str, owner: str) -> None:
    """A run written straight to the offline store, owned by ``owner``."""
    research.save_run({"id": rid, "brand_id": "legalsoft", "brand_name": "Legal Soft",
                       "topic": f"topic {rid}", "created": "2026-10-01T00:00:00Z",
                       "status": "research", "user_id": owner})


class _UsersCollection:
    """The ``users`` collection, as far as the real ``list_users`` reads it."""

    def __init__(self, docs: dict[str, dict]) -> None:
        self.docs = docs
        self.reads: list[float | None] = []

    def collection(self, name: str) -> "_UsersCollection":
        assert name == "users", name
        return self

    def stream(self, timeout=None):
        self.reads.append(timeout)
        return iter(SimpleNamespace(id=uid, to_dict=lambda d=doc: dict(d))
                    for uid, doc in self.docs.items())


def test_admin_run_list_names_each_author_from_one_directory_read(monkeypatch, as_caller):
    """`by` is the author's name, else their email; no account, no `by`.
    Runs the real ``firestore_repo.list_users`` over a faked ``_db``."""
    _stored_run("bw-named", "u-named")
    _stored_run("bw-email-only", "u-email-only")
    _stored_run("bw-deleted-user", "u-deleted")
    _stored_run("bw-legacy", "")
    users = _UsersCollection({
        "u-named": {"name": "Asha Rao", "email": "asha@legalsoft.com"},
        "u-email-only": {"name": "  ", "email": "ravi@legalsoft.com"},
    })
    reads = users.reads
    monkeypatch.setattr(firestore_repo, "_db", lambda: users)
    as_caller(ADMIN)
    r = client.get("/api/blog/runs")
    assert r.status_code == 200, r.text
    rows = {row["id"]: row for row in r.json()["runs"]}
    assert set(rows) == {"bw-named", "bw-email-only", "bw-deleted-user", "bw-legacy"}
    assert rows["bw-named"]["by"] == "Asha Rao"
    assert rows["bw-email-only"]["by"] == "ravi@legalsoft.com"
    assert "by" not in rows["bw-deleted-user"]
    assert "by" not in rows["bw-legacy"]
    # One bounded read for the whole list, not one per row.
    assert len(reads) == 1 and reads[0]
    # The label lives in the response only; the stored index never carries it.
    assert all("by" not in row for row in research.list_runs())


def test_admin_run_list_survives_a_failed_directory_read(monkeypatch, as_caller, caplog):
    """The names are a label, not the list: a dead directory read is a
    warning in the logs and an unlabelled 200, never a 500."""
    _stored_run("bw-a", "u-a")
    _stored_run("bw-b", "u-b")

    def dead(**_):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(firestore_repo, "list_users", dead)
    as_caller(ADMIN)
    with caplog.at_level(logging.WARNING, logger="agentos.blog_writer"):
        r = client.get("/api/blog/runs")
    assert r.status_code == 200, r.text
    runs = r.json()["runs"]
    assert {row["id"] for row in runs} == {"bw-a", "bw-b"}
    assert all("by" not in row for row in runs)
    assert "could not read the user directory" in caplog.text


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
    assert drafted["draft"]["guidelines_applied"] is True
    assert drafted["draft"]["blocks"][0]["text"] == "Opening, line-edited."

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
