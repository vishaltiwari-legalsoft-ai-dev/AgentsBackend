# backend/tests/test_model_catalog.py
"""Live premium model catalog for the Agent Configuration dropdowns.

The panel's dropdowns are fed from OpenRouter's public /api/v1/models list,
filtered to premium model families (Anthropic / OpenAI / Google / xAI /
DeepSeek / Mistral + top image providers) and classified into tiers
(flagship / balanced / fast) so the UI can group them. The curated lists in
``agent_config`` stay as the offline fallback and carry the ``recommended``
flags. Fetches are cached; tests never touch the network.
"""
from __future__ import annotations

import pytest

from app.services import model_catalog


def _or_model(mid: str, name: str, inputs: list[str], outputs: list[str]) -> dict:
    """A minimal OpenRouter /models entry."""
    return {
        "id": mid,
        "name": name,
        "architecture": {
            "input_modalities": inputs,
            "output_modalities": outputs,
        },
    }


FAKE_MODELS = [
    _or_model("anthropic/claude-opus-4.6", "Anthropic: Claude Opus 4.6", ["text", "image"], ["text"]),
    _or_model("anthropic/claude-haiku-4.5", "Anthropic: Claude Haiku 4.5", ["text", "image"], ["text"]),
    _or_model("openai/gpt-5.2", "OpenAI: GPT-5.2", ["text", "image"], ["text"]),
    _or_model("openai/gpt-5.2-mini", "OpenAI: GPT-5.2 Mini", ["text", "image"], ["text"]),
    _or_model("google/gemini-3-flash", "Google: Gemini 3 Flash", ["text", "image"], ["text"]),
    _or_model("google/gemini-3-pro-image-preview", "Google: Gemini 3 Pro Image", ["text", "image"], ["image", "text"]),
    _or_model("black-forest-labs/flux.2-max", "BFL: Flux 2 Max", ["text"], ["image"]),
    # Must be filtered out: not a premium family / free variant.
    _or_model("qwen/qwen-2-7b-instruct", "Qwen 2 7B", ["text"], ["text"]),
    _or_model("meta-llama/llama-3-8b:free", "Llama 3 8B (free)", ["text"], ["text"]),
]


@pytest.fixture()
def live_catalog(monkeypatch):
    monkeypatch.setattr(model_catalog, "fetch_openrouter_models", lambda: FAKE_MODELS)
    model_catalog.clear_cache()
    yield model_catalog.get_catalog()
    model_catalog.clear_cache()


def _ids(options: list[dict]) -> list[str]:
    return [str(o["id"]) for o in options]


# --------------------------------------------------------------------------- #
# Premium filtering + tier classification
# --------------------------------------------------------------------------- #

def test_text_catalog_keeps_premium_families_and_drops_the_rest(live_catalog):
    text_ids = _ids(live_catalog["openrouter_model"])
    assert "anthropic/claude-opus-4.6" in text_ids
    assert "openai/gpt-5.2" in text_ids
    assert "google/gemini-3-flash" in text_ids
    assert "qwen/qwen-2-7b-instruct" not in text_ids
    assert "meta-llama/llama-3-8b:free" not in text_ids
    # Image-output models don't belong in the text dropdown.
    assert "google/gemini-3-pro-image-preview" not in text_ids


def test_tiers_are_assigned_by_family(live_catalog):
    by_id = {o["id"]: o for o in live_catalog["openrouter_model"]}
    assert by_id["anthropic/claude-opus-4.6"]["tier"] == "flagship"
    assert by_id["openai/gpt-5.2"]["tier"] == "flagship"
    assert by_id["openai/gpt-5.2-mini"]["tier"] == "balanced"
    assert by_id["anthropic/claude-haiku-4.5"]["tier"] == "fast"
    assert by_id["google/gemini-3-flash"]["tier"] == "balanced"


def test_options_are_ordered_flagship_first(live_catalog):
    tiers = [o["tier"] for o in live_catalog["openrouter_model"]]
    order = {"flagship": 0, "balanced": 1, "fast": 2}
    assert tiers == sorted(tiers, key=order.__getitem__)


def test_image_catalog_takes_image_output_models(live_catalog):
    image_ids = _ids(live_catalog["openrouter_image_model"])
    assert "google/gemini-3-pro-image-preview" in image_ids
    assert "black-forest-labs/flux.2-max" in image_ids
    assert "anthropic/claude-opus-4.6" not in image_ids


def test_vision_catalog_takes_image_input_text_models(live_catalog):
    vision_ids = _ids(live_catalog["openrouter_vision_model"])
    assert "anthropic/claude-opus-4.6" in vision_ids
    assert "black-forest-labs/flux.2-max" not in vision_ids


# --------------------------------------------------------------------------- #
# Curated merge + fallback
# --------------------------------------------------------------------------- #

def test_curated_recommended_flag_survives_the_merge(live_catalog):
    by_id = {o["id"]: o for o in live_catalog["openrouter_model"]}
    # anthropic/claude-opus-4.6 is the curated recommended text model AND in
    # the live list — merged entry keeps the flag, no duplicate row.
    assert by_id["anthropic/claude-opus-4.6"].get("recommended") is True
    assert _ids(live_catalog["openrouter_model"]).count("anthropic/claude-opus-4.6") == 1


def test_offline_fallback_serves_curated_lists_with_tiers(monkeypatch):
    monkeypatch.setattr(model_catalog, "fetch_openrouter_models", lambda: None)
    model_catalog.clear_cache()
    catalog = model_catalog.get_catalog()
    for field in (
        "openrouter_model",
        "openrouter_fast_model",
        "openrouter_image_model",
        "openrouter_vision_model",
        "gd_planner_model",
    ):
        assert catalog[field], f"{field} empty in offline fallback"
        assert all(o.get("tier") for o in catalog[field])
    model_catalog.clear_cache()


def test_fetch_result_is_cached(monkeypatch):
    calls = {"n": 0}

    def counting_fetch():
        calls["n"] += 1
        return FAKE_MODELS

    monkeypatch.setattr(model_catalog, "fetch_openrouter_models", counting_fetch)
    model_catalog.clear_cache()
    model_catalog.get_catalog()
    model_catalog.get_catalog()
    assert calls["n"] == 1
    model_catalog.clear_cache()


def test_allowed_ids_matches_catalog(live_catalog):
    allowed = model_catalog.allowed_ids("openrouter_model")
    assert "openai/gpt-5.2" in allowed
    assert "qwen/qwen-2-7b-instruct" not in allowed


# --------------------------------------------------------------------------- #
# Admin integration: the panel serves and validates against the live catalog.
# --------------------------------------------------------------------------- #

def test_admin_saves_a_live_catalog_model(live_catalog, monkeypatch):
    import app  # noqa: F401 - registers agent roots
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app
    from app.security import get_current_user
    from app.services import firestore_repo

    creator = {
        "id": "creator1",
        "email": "creator@legalsoft.com",
        "is_admin": True,
        "is_creator": True,
        "session_id": "",
        "timezone": "UTC",
    }
    prev = fastapi_app.dependency_overrides.get(get_current_user)
    fastapi_app.dependency_overrides[get_current_user] = lambda: dict(creator)
    try:
        monkeypatch.setattr(firestore_repo, "get_app_config", lambda **_kw: {})
        monkeypatch.setattr(firestore_repo, "set_agent_config", lambda *a, **k: {})
        client = TestClient(fastapi_app)

        # A model that exists ONLY in the live catalog (not curated) saves fine.
        r = client.post(
            "/api/admin/agents/a6", json={"openrouter_model": "openai/gpt-5.2"}
        )
        assert r.status_code == 200, r.text

        # Junk that the live catalog filtered out is rejected.
        r = client.post(
            "/api/admin/agents/a6",
            json={"openrouter_model": "qwen/qwen-2-7b-instruct"},
        )
        assert r.status_code == 400
    finally:
        if prev is None:
            fastapi_app.dependency_overrides.pop(get_current_user, None)
        else:
            fastapi_app.dependency_overrides[get_current_user] = prev
