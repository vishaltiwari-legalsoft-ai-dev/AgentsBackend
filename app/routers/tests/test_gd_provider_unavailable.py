"""An unavailable image provider must reach the client WITH its reason.

``providers.ImageProviderUnavailable`` is the honest-failure path: auto mode
refuses to pass a brand-gradient placeholder off as a real generation and names
the cause. Unmapped, it fell through to the catch-all in ``app.main``, which
outside development flattens every detail to "Internal server error" — so the
only actionable sentence lived in the server log and the user saw a bare 500.

Harness mirrors test_gd_prompt_images_api.py: fs run-storage under GD_RUNS_DIR,
caller from the shared harness in ``conftest.py``.
"""

import pytest

from app.routers.tests.conftest import client
from graphics_designer_agent import pipeline, providers

REASON = ("no image-model API key configured (set OPENROUTER_API_KEY, or the admin "
          "key override in the Secrets panel)")


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch, as_caller):
    monkeypatch.setenv("GD_RUNS_DIR", str(tmp_path))
    as_caller()


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


def test_an_image_model_failure_is_a_503_naming_the_model(monkeypatch):
    """The model call itself failed (after its bounded retry): an honest 503
    with the model and cause, not the catch-all 500."""
    from app.services.openrouter import ImageProviderError

    run_id = client.post("/api/gd/runs", json={}).json()["id"]

    def _down(run, stage, variant=None):
        raise ImageProviderError(
            "image model openai/gpt-image-2.5-sunburst failed (502): upstream error",
            model="openai/gpt-image-2.5-sunburst", status=502)

    monkeypatch.setattr(pipeline, "generate", _down)

    r = client.post(f"/api/gd/runs/{run_id}/generate", json={"stage": 2})
    assert r.status_code == 503, r.text
    assert "openai/gpt-image-2.5-sunburst" in r.json()["detail"]


def test_an_image_model_rate_limit_is_a_429_with_retry_after(monkeypatch):
    """A burst of campaigns hits provider rate limits; the client is told to
    back off rather than shown a server fault."""
    from app.services.openrouter import ImageProviderError

    run_id = client.post("/api/gd/runs", json={}).json()["id"]

    def _limited(run, stage, variant=None):
        raise ImageProviderError("image model x failed (429): rate limited",
                                 model="x", status=429, retry_after=12)

    monkeypatch.setattr(pipeline, "generate", _limited)

    r = client.post(f"/api/gd/runs/{run_id}/generate", json={"stage": 1})
    assert r.status_code == 429, r.text
    assert r.headers.get("retry-after") == "12"
