# backend/tests/test_admin_refresh_packs.py
"""Unit F: admin pack-refresh endpoint (plan Task 12, code part only).

``POST /api/admin/brands/refresh-packs`` lets an admin drop the GD brand-pack
cache (``registry.refresh()``) so a Firestore brand enriched by the ingestion
CLI is picked up by the running app without a redeploy.

Approach (resolution 4): imports the full app (``app.main.app``) rather than a
minimal FastAPI app, because that is the established pattern already used by
``app/routers/tests/test_gd_elements_api.py`` and
``app/routers/tests/test_mr_router.py`` (full app + ``get_current_user``
overridden via ``app.dependency_overrides``) — it has not proven heavy/flaky
in this repo, so there is no reason to diverge.
"""
from __future__ import annotations

import app  # noqa: F401 - side effect: registers agent roots on sys.path (see app/__init__.py)
import pytest
from fastapi.testclient import TestClient
from graphics_designer_agent import registry

from app.main import app as fastapi_app
from app.security import get_current_user

fastapi_app.dependency_overrides[get_current_user] = lambda: {
    "id": "admin1", "email": "admin@legalsoft.com", "is_admin": True,
}
client = TestClient(fastapi_app)


@pytest.fixture(autouse=True)
def clean_registry():
    registry.refresh()
    yield
    registry.register_dynamic_source(None)
    registry.refresh()


def test_refresh_packs_returns_packs():
    r = client.post("/api/admin/brands/refresh-packs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "packs" in body
    packs = body["packs"]
    assert isinstance(packs, list) and packs
    for p in packs:
        assert set(p.keys()) == {"id", "name"}


def test_refresh_packs_picks_up_dynamic_brand(monkeypatch, valid_dyn_spec):
    monkeypatch.setenv("GD_DYNAMIC_BRANDS", "1")
    registry.register_dynamic_source(lambda: [valid_dyn_spec])

    r = client.post("/api/admin/brands/refresh-packs")
    assert r.status_code == 200, r.text
    ids = {p["id"] for p in r.json()["packs"]}
    assert valid_dyn_spec["id"] in ids
