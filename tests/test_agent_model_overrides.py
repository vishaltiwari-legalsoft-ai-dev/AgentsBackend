# backend/tests/test_agent_model_overrides.py
"""Per-agent model overrides must actually reach the LLM entry points.

The creator's Agent Configuration panel stores per-agent model choices under
``app_config/global`` → ``agents.{agent_id}``. Before this unit, only the
Graphics Designer image/planner path consumed them — every other agent resolved
models globally, so the panel's dropdowns were dead switches. These tests pin
the real wiring for the live agents:

- a1 Graphic Designer (image + planner already wired; text/fast/vision join here)
- a6 Marketing Research (reasoning model)

Full-app TestClient pattern follows ``test_admin_refresh_packs.py``.
"""
from __future__ import annotations

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import get_current_user
from app.services import firestore_repo, runtime_config
from app.services import openrouter as openrouter_service

_CREATOR = {
    "id": "creator1",
    "email": "creator@legalsoft.com",
    "is_admin": True,
    "is_creator": True,
    "session_id": "",
    "timezone": "UTC",
}
client = TestClient(fastapi_app)


@pytest.fixture(autouse=True)
def _as_creator():
    """(Re)install the creator auth override per test — module-level overrides
    get popped by other routers' test fixtures when the whole suite runs."""
    prev = fastapi_app.dependency_overrides.get(get_current_user)
    fastapi_app.dependency_overrides[get_current_user] = lambda: dict(_CREATOR)
    yield
    if prev is None:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    else:
        fastapi_app.dependency_overrides[get_current_user] = prev


@pytest.fixture()
def app_config(monkeypatch):
    """Pin the app-config doc (the Firestore ``app_config/global`` snapshot)."""
    cfg: dict = {
        "openrouter_api_key": "sk-test",
        "openrouter_model": "global/reasoning-model",
        "openrouter_fast_model": "global/fast-model",
        "agents": {
            "a6": {"openrouter_model": "anthropic/claude-opus-4.6"},
            "a1": {"openrouter_fast_model": "anthropic/claude-haiku-4.5"},
        },
    }
    monkeypatch.setattr(firestore_repo, "get_app_config", lambda **_kw: cfg)
    return cfg


# --------------------------------------------------------------------------- #
# get_llm: the single text-model chokepoint resolves per agent.
# --------------------------------------------------------------------------- #

def test_get_llm_without_agent_uses_global_model(app_config):
    llm = openrouter_service.get_llm()
    assert llm.model_name == "global/reasoning-model"


def test_get_llm_resolves_a6_reasoning_override(app_config):
    llm = openrouter_service.get_llm(agent_id="a6")
    assert llm.model_name == "anthropic/claude-opus-4.6"


def test_get_llm_resolves_a1_fast_override(app_config):
    llm = openrouter_service.get_llm(fast=True, agent_id="a1")
    assert llm.model_name == "anthropic/claude-haiku-4.5"


def test_get_llm_agent_without_override_inherits_global(app_config):
    llm = openrouter_service.get_llm(fast=True, agent_id="a6")
    assert llm.model_name == "global/fast-model"


# --------------------------------------------------------------------------- #
# Call-site binding: the MR adapter identifies itself.
# --------------------------------------------------------------------------- #

class _StubLLM:
    def invoke(self, _prompt):
        class R:
            content = "{}"

        return R()


def test_mr_llm_wrappers_pass_a6(monkeypatch):
    from marketing_research_agent import analysis

    seen: list[str | None] = []

    def fake_get_llm(*_a, **kw):
        seen.append(kw.get("agent_id"))
        return _StubLLM()

    monkeypatch.setattr(openrouter_service, "get_llm", fake_get_llm)
    monkeypatch.setattr(analysis, "is_offline", lambda: False)

    analysis.llm_text("hello")
    assert seen == ["a6"]


# --------------------------------------------------------------------------- #
# Admin endpoints: panel lists only live agents, each with its real fields.
# --------------------------------------------------------------------------- #

def test_agents_payload_lists_only_live_agents_with_their_fields(app_config):
    r = client.get("/api/admin/agents")
    assert r.status_code == 200, r.text
    body = r.json()

    by_id = {a["id"]: a for a in body["agents"]}
    assert set(by_id) == {"a1", "a6"}
    assert all(a["live"] for a in body["agents"])

    assert by_id["a6"]["fields"] == ["openrouter_model"]
    # GD keeps its full set, planner included.
    assert set(by_id["a1"]["fields"]) == set(runtime_config.AGENT_OVERRIDE_FIELDS)

    # The planner dropdown needs options too.
    assert body["catalog"].get("gd_planner_model")


def test_update_rejects_field_the_agent_does_not_use(app_config, monkeypatch):
    monkeypatch.setattr(firestore_repo, "set_agent_config", lambda *a, **k: {})
    r = client.post(
        "/api/admin/agents/a6",
        json={"openrouter_image_model": "google/gemini-3-pro-image-preview"},
    )
    assert r.status_code == 400
    assert "does not use" in r.json()["detail"]


def test_update_saves_allowed_field(app_config, monkeypatch):
    written: dict = {}

    def fake_set(agent_id, patch):
        written[agent_id] = patch
        return {}

    monkeypatch.setattr(firestore_repo, "set_agent_config", fake_set)
    r = client.post(
        "/api/admin/agents/a6",
        json={"openrouter_model": "anthropic/claude-sonnet-4.5"},
    )
    assert r.status_code == 200, r.text
    assert written == {"a6": {"openrouter_model": "anthropic/claude-sonnet-4.5"}}


def test_update_gd_planner_model_saves_without_500(app_config, monkeypatch):
    """Regression: the save loop iterated AGENT_OVERRIDE_FIELDS (5 fields) but the
    request body only defined 4 — ``getattr(body, "gd_planner_model")`` raised
    AttributeError, so EVERY save 500'd. Empty body must round-trip cleanly."""
    monkeypatch.setattr(firestore_repo, "set_agent_config", lambda *a, **k: {})

    r = client.post("/api/admin/agents/a1", json={})
    assert r.status_code == 200, r.text

    r = client.post(
        "/api/admin/agents/a1",
        json={"gd_planner_model": "anthropic/claude-opus-4.6"},
    )
    assert r.status_code == 200, r.text
