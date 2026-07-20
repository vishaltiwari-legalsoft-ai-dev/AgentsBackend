"""I3: serpapi_key wired into the admin Secrets panel (masked settings payload).

Uses the full app (mirrors app/routers/tests/test_gd_elements_api.py and
tests/test_admin_refresh_packs.py: full ``app.main.app`` + ``get_current_user``
overridden via dependency_overrides). ``firestore_repo.get_app_config`` /
``set_app_config`` are monkeypatched to an in-memory stand-in so this test
never touches real Firestore.
"""
from __future__ import annotations

import contextlib

import app  # noqa: F401 - side effect: registers agent roots on sys.path (see app/__init__.py)
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import get_current_user
from app.services import firestore_repo

fastapi_app.dependency_overrides[get_current_user] = lambda: {
    "id": "creator1", "email": "creator@legalsoft.com", "is_admin": True, "is_creator": True,
}
client = TestClient(fastapi_app)


@contextlib.contextmanager
def _as_user(user: dict):
    """Temporarily override get_current_user, restoring whatever override (if
    any) was previously in place. ``dependency_overrides`` is a single shared
    dict on the app singleton, mutated at module level by several test files —
    a test must never clobber it with a hardcoded value on exit, or it can
    silently break other files' tests depending on collection/run order."""
    previous = fastapi_app.dependency_overrides.get(get_current_user)
    fastapi_app.dependency_overrides[get_current_user] = lambda: user
    try:
        yield
    finally:
        if previous is not None:
            fastapi_app.dependency_overrides[get_current_user] = previous
        else:
            fastapi_app.dependency_overrides.pop(get_current_user, None)


class _FakeAppConfigStore:
    """In-memory stand-in for the ``app_config/global`` Firestore doc."""

    def __init__(self):
        self.data: dict = {}

    def get(self, *, use_cache: bool = True):
        return dict(self.data)

    def set(self, patch: dict):
        self.data.update(patch)
        return dict(self.data)


def test_serpapi_key_roundtrips_and_is_masked(monkeypatch):
    fake = _FakeAppConfigStore()
    monkeypatch.setattr(firestore_repo, "get_app_config", fake.get)
    monkeypatch.setattr(firestore_repo, "set_app_config", fake.set)

    with _as_user({
        "id": "creator1", "email": "creator@legalsoft.com", "is_admin": True, "is_creator": True,
    }):
        before = client.get("/api/admin/settings").json()
        assert before["serpapi"] == {"api_key_set": False, "api_key_hint": "", "api_key_source": "unset"}

        resp = client.post("/api/admin/settings", json={"serpapi_key": "a1b2c3d4e5f6g7h8"})
        assert resp.status_code == 200
        after = resp.json()
        assert after["serpapi"]["api_key_set"] is True
        assert after["serpapi"]["api_key_source"] == "override"
        assert after["serpapi"]["api_key_hint"] != "a1b2c3d4e5f6g7h8"   # never returned raw
        assert "a1b2c3" in after["serpapi"]["api_key_hint"]              # recognisable hint

        # Persisted server-side unmasked (the point of the roundtrip).
        assert fake.data["serpapi_key"] == "a1b2c3d4e5f6g7h8"


def test_serpapi_key_masked_in_db_viewer():
    from app.routers.admin import _sanitize

    assert _sanitize("a1b2c3d4e5f6g7h8", key="serpapi_key") != "a1b2c3d4e5f6g7h8"
    assert _sanitize("a1b2c3d4e5f6g7h8", key="serpapi_key") == "a1b2c3…g7h8"
