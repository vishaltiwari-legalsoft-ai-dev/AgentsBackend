"""An unavailable image provider must reach the client WITH its reason.

``providers.ImageProviderUnavailable`` is the honest-failure path: auto mode
refuses to pass a brand-gradient placeholder off as a real generation and names
the cause. Unmapped, it fell through to the catch-all in ``app.main``, which
outside development flattens every detail to "Internal server error" — so the
only actionable sentence lived in the server log and the user saw a bare 500.

Harness mirrors test_gd_prompt_images_api.py: fs run-storage under GD_RUNS_DIR,
auth overridden per-test and restored on teardown (never at import time — the
overrides map is process-global and shared with every other router suite).
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import get_current_user
from graphics_designer_agent import pipeline, providers

client = TestClient(fastapi_app)

USER = {"id": "u1", "email": "t@legalsoft.com"}
REASON = ("no image-model API key configured (set OPENROUTER_API_KEY, or the admin "
          "key override in the Secrets panel)")


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GD_RUNS_DIR", str(tmp_path))
    prev = fastapi_app.dependency_overrides.get(get_current_user)
    fastapi_app.dependency_overrides[get_current_user] = lambda: dict(USER)
    yield
    if prev is None:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    else:
        fastapi_app.dependency_overrides[get_current_user] = prev


def test_an_unavailable_image_provider_is_a_503_carrying_the_reason(monkeypatch):
    run_id = client.post("/api/gd/runs", json={}).json()["id"]

    def _unavailable(run, stage, variant=None):
        raise providers.ImageProviderUnavailable(REASON)

    monkeypatch.setattr(pipeline, "generate", _unavailable)

    r = client.post(f"/api/gd/runs/{run_id}/generate", json={"stage": 1})
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == REASON


def test_a_pipeline_error_still_maps_to_409(monkeypatch):
    """The pre-existing mapping is unchanged — the two failures stay distinct."""
    run_id = client.post("/api/gd/runs", json={}).json()["id"]

    def _boom(run, stage, variant=None):
        raise pipeline.PipelineError("approve stage 1 first")

    monkeypatch.setattr(pipeline, "generate", _boom)

    r = client.post(f"/api/gd/runs/{run_id}/generate", json={"stage": 1})
    assert r.status_code == 409 and r.json()["detail"] == "approve stage 1 first"
