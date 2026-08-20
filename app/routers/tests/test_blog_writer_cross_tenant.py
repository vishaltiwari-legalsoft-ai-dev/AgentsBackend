"""Blog Writer ownership — the one predicate in the codebase that failed open.

`app/routers/blog_writer.py:_run` used to read

    if owner and owner != user.get("id") and not user.get("is_admin"):

The leading ``owner and`` was load-bearing: a run whose stored ``user_id`` was
missing or ``""`` made the whole conjunction False, so the run was handed to
**any** signed-in caller. Every other ownership predicate in the backend uses a
bare ``!=``, where an empty owner simply fails the comparison and denies.

`GET /blog/runs` then published exactly the ids that branch accepted, because
its filter led with ``not r.get("user_id")``. No guessing was required.

This was not latent. A read-only scan of the production ``blog_writer``
collection on 2026-08-21 found 2 run documents, **both** with no ``user_id`` —
artefacts of a smoke run made before runs were stamped on 2026-08-08. Their
yield to any signed-in caller was the full research ledger and draft article
text, plus the ability to spend on ``research/step`` / ``draft`` / ``visuals``
and silently rewrite another user's article body.

These tests pin both halves closed. Written after the fix, then mutation-checked
by restoring each old expression and confirming the matching test fails.
"""
from __future__ import annotations

import os

os.environ["BLOG_OFFLINE"] = "1"

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest

from app.routers.tests.conftest import client
from blog_writer_agent import research
from seo_geo_agent import insights

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com", "enabled": True}

OWNER = {"id": "owner-1", "email": "owner@legalsoft.com", "is_admin": False,
         "is_creator": False, "session_id": "", "timezone": "UTC"}
STRANGER = {"id": "stranger-2", "email": "stranger@legalsoft.com", "is_admin": False,
            "is_creator": False, "session_id": "", "timezone": "UTC"}
ADMIN = {"id": "admin-3", "email": "admin@legalsoft.com", "is_admin": True,
         "is_creator": True, "session_id": "", "timezone": "UTC"}


@pytest.fixture(autouse=True)
def _harness(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOG_OFFLINE", "1")
    monkeypatch.setenv("BLOG_LOCAL_DIR", str(tmp_path / "blog_state"))
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setattr(insights, "list_brands", lambda: [dict(BRAND)])


def _store(run: dict) -> str:
    """Write a run document straight to the offline store, bypassing the API.

    The legacy shape cannot be created through the router any more — runs have
    been stamped since 2026-08-08 — so reproducing it means writing the
    document, which is exactly what production still holds.
    """
    research.save_run(run)
    return run["id"]


def _run_doc(rid: str, topic: str, body: str, owner: str | None) -> dict:
    doc = {"id": rid, "topic": topic, "brand_id": "legalsoft", "brand_name": "Legal Soft",
           "created": "2026-08-03T00:00:00Z", "status": "done", "steps": [],
           "draft": {"body": body}}
    if owner is not None:
        doc["user_id"] = owner
    return doc


def _legacy_run() -> dict:
    """No user_id key at all — the shape production still holds."""
    return _run_doc("bw-legacy-unstamped", "Legacy topic", "unpublished draft body", None)


def _owned_run() -> dict:
    return _run_doc("bw-owned", "Owned topic", "the owner's draft", OWNER["id"])


# --------------------------------------------------------------------------
# The unstamped run — the fail-open case
# --------------------------------------------------------------------------

def test_an_unstamped_run_belongs_to_nobody_and_is_readable_by_nobody(as_caller):
    """A run with no user_id is not a public run. It is an orphan."""
    rid = _store(_legacy_run())
    as_caller(STRANGER)
    r = client.get(f"/api/blog/runs/{rid}")
    assert r.status_code == 404, r.text
    assert "unpublished draft body" not in r.text


def test_an_unstamped_run_is_not_published_by_the_run_list(as_caller):
    """The list route used to lead with `not r.get("user_id")`, handing out the
    very ids the fail-open branch then accepted."""
    _store(_legacy_run())
    as_caller(STRANGER)
    body = client.get("/api/blog/runs").text
    assert "bw-legacy-unstamped" not in body
    assert "Legacy topic" not in body


def test_not_even_the_creator_of_a_sibling_run_reaches_an_unstamped_one(as_caller):
    """Owning *a* run confers nothing over an unowned one."""
    _store(_owned_run())
    rid = _store(_legacy_run())
    as_caller(OWNER)
    assert client.get(f"/api/blog/runs/{rid}").status_code == 404


def test_an_admin_still_reaches_an_unstamped_run(as_caller):
    """The admin clause is the house convention across four routers and is
    env-configured per request, not trusted from the token. Pinned so the
    orphaned documents stay recoverable through the product rather than only
    through the Firebase console."""
    rid = _store(_legacy_run())
    as_caller(ADMIN)
    assert client.get(f"/api/blog/runs/{rid}").status_code == 200


# --------------------------------------------------------------------------
# The ordinary case — a stamped run
# --------------------------------------------------------------------------

def test_the_owner_reads_their_own_run(as_caller):
    """The control. Without this the 404s above could pass vacuously."""
    rid = _store(_owned_run())
    as_caller(OWNER)
    r = client.get(f"/api/blog/runs/{rid}")
    assert r.status_code == 200, r.text
    assert "the owner's draft" in r.text


def test_another_users_stamped_run_is_404(as_caller):
    rid = _store(_owned_run())
    as_caller(STRANGER)
    r = client.get(f"/api/blog/runs/{rid}")
    assert r.status_code == 404
    assert "the owner's draft" not in r.text


def test_the_run_list_shows_only_your_own(as_caller):
    _store(_owned_run())
    _store(_legacy_run())
    as_caller(OWNER)
    body = client.get("/api/blog/runs").text
    assert "bw-owned" in body
    assert "bw-legacy-unstamped" not in body


def test_a_real_run_id_and_an_invented_one_look_the_same_to_a_stranger(as_caller):
    """404 either way, so the response is not an ownership oracle."""
    rid = _store(_owned_run())
    as_caller(STRANGER)
    real = client.get(f"/api/blog/runs/{rid}")
    fake = client.get("/api/blog/runs/run-bw-does-not-exist")
    assert real.status_code == fake.status_code == 404
