"""An unreadable Firestore must never render as "you have no data".

Three admin reads used to answer ``[]`` on any exception, so a Firestore outage
(or a query whose composite index does not exist) reached the user as an empty
usage dashboard, an empty image library, and a Database panel missing every
per-agent table. ``count_collection`` already modelled this correctly by
returning ``None``; these pin the same contract on the other three.
"""
from __future__ import annotations

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import get_current_user, require_admin
from app.services import firestore_repo

client = TestClient(fastapi_app)

USER = {"id": "u1", "email": "t@legalsoft.com", "is_admin": True, "is_creator": True}


@pytest.fixture(autouse=True)
def _as_admin():
    prev = dict(fastapi_app.dependency_overrides)
    fastapi_app.dependency_overrides[get_current_user] = lambda: dict(USER)
    fastapi_app.dependency_overrides[require_admin] = lambda: dict(USER)
    yield
    fastapi_app.dependency_overrides.clear()
    fastapi_app.dependency_overrides.update(prev)


def _dead(*_a, **_kw):
    raise RuntimeError("503 the datastore is unavailable")


# --- the repo layer: None means "could not read", [] means "nothing there" ---

def test_list_usage_events_reports_none_on_failure(monkeypatch):
    monkeypatch.setattr(firestore_repo, "_db", _dead)
    assert firestore_repo.list_usage_events("u1", "2026-08-01") is None


def test_list_gallery_images_reports_none_on_failure(monkeypatch):
    monkeypatch.setattr(firestore_repo, "_db", _dead)
    assert firestore_repo.list_gallery_images() is None


def test_list_agent_run_collections_reports_none_on_failure(monkeypatch):
    monkeypatch.setattr(firestore_repo, "_db", _dead)
    assert firestore_repo.list_agent_run_collections() is None


# --- the HTTP layer: 502, not a zeroed page ---------------------------------

def test_usage_dashboard_answers_502_when_events_cannot_be_read(monkeypatch):
    monkeypatch.setattr(firestore_repo, "list_usage_events", lambda *a, **kw: None)
    r = client.get("/api/usage")
    assert r.status_code == 502, r.text
    assert "Could not read" in r.json()["detail"]


def test_usage_dashboard_still_renders_a_genuinely_empty_week(monkeypatch):
    """The other half of the contract — an empty result is still a 200."""
    monkeypatch.setattr(firestore_repo, "list_usage_events", lambda *a, **kw: [])
    r = client.get("/api/usage")
    assert r.status_code == 200, r.text
    assert r.json()["totals"]["sessions"] == 0


def test_image_library_answers_502_when_it_cannot_be_read(monkeypatch):
    monkeypatch.setattr(firestore_repo, "list_gallery_images", lambda **kw: None)
    r = client.get("/api/admin/image-library")
    assert r.status_code == 502, r.text


def test_image_library_still_renders_a_genuinely_empty_gallery(monkeypatch):
    monkeypatch.setattr(firestore_repo, "list_gallery_images", lambda **kw: [])
    r = client.get("/api/admin/image-library")
    assert r.status_code == 200 and r.json()["total"] == 0, r.text


def test_db_panel_survives_a_failed_collection_discovery(monkeypatch):
    """Discovery failing must not take the panel down — the catalogued agents
    are still listed, and ``connected`` already tells the truth via the counts."""
    monkeypatch.setattr(firestore_repo, "list_agent_run_collections", lambda: None)
    monkeypatch.setattr(firestore_repo, "count_collection", lambda _n: None)
    r = client.get("/api/admin/db/collections")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connected"] is False
    assert any(c["name"].startswith("agent_runs__") for c in body["collections"])
