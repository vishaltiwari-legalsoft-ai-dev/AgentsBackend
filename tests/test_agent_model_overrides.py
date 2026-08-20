# backend/tests/test_agent_model_overrides.py
"""Per-agent model overrides must actually reach the LLM entry points.

The creator's Agent Configuration panel stores per-agent model choices under
``app_config/global`` → ``agents.{agent_id}``. Before this unit, only the
Graphics Designer image/planner path consumed them — every other agent resolved
models globally, so the panel's dropdowns were dead switches. These tests pin
the real wiring for the live agents:

- a1 Graphic Designer (image + planner already wired; text/fast/vision join here)
- a6 Marketing Research (reasoning model)
- a9 Blog Writer (reasoning model for research + writing)

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
        "openrouter_vision_model": "global/vision-model",
        "openrouter_image_model": "google/gemini-2.5-flash-image",
        "gd_polish_image_model": "global/polish-model",
        "agents": {
            "a6": {"openrouter_model": "anthropic/claude-opus-4.6"},
            "a1": {
                "openrouter_fast_model": "anthropic/claude-haiku-4.5",
                "openrouter_vision_model": "agent/a1-vision-model",
                "gd_polish_image_model": "agent/a1-polish-model",
            },
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


def test_blog_llm_adapter_passes_a9(monkeypatch):
    from blog_writer_agent import llm as blog_llm
    from blog_writer_agent import state as blog_state

    seen: list[str | None] = []

    def fake_get_llm(*_a, **kw):
        seen.append(kw.get("agent_id"))
        return _StubLLM()

    monkeypatch.setattr(openrouter_service, "get_llm", fake_get_llm)
    monkeypatch.setattr(blog_state, "use_cloud", lambda: True)

    blog_llm.llm_json("system", "prompt")
    assert seen == ["a9"]


# --------------------------------------------------------------------------- #
# Admin endpoints: panel lists only live agents, each with its real fields.
# --------------------------------------------------------------------------- #

def test_agents_payload_lists_only_live_agents_with_their_fields(app_config):
    r = client.get("/api/admin/agents")
    assert r.status_code == 200, r.text
    body = r.json()

    by_id = {a["id"]: a for a in body["agents"]}
    assert set(by_id) == {"a1", "a6", "a9", "a10", "a11"}
    assert all(a["live"] for a in body["agents"])

    assert by_id["a6"]["fields"] == ["openrouter_model"]
    assert by_id["a9"]["fields"] == ["openrouter_model"]
    # GEO's LLM use is parsing-grade only (prompt universe, sentiment).
    assert by_id["a10"]["fields"] == ["openrouter_fast_model"]
    # Browser Agent: premium planner up front, reasoning model per act-loop
    # step, fast model for tab digests.
    assert by_id["a11"]["fields"] == [
        "browser_planner_model", "openrouter_model", "openrouter_fast_model",
    ]
    # GD keeps its full set, its planner included. Every field an agent declares
    # must be one a Creator is allowed to override.
    assert set(by_id["a1"]["fields"]) == {
        "openrouter_model", "openrouter_fast_model", "openrouter_image_model",
        "openrouter_vision_model", "gd_planner_model", "gd_polish_image_model",
    }
    for agent in body["agents"]:
        assert set(agent["fields"]) <= set(runtime_config.AGENT_OVERRIDE_FIELDS)

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


# --------------------------------------------------------------------------- #
# Defect 1 - gd_polish_image_model: a documented admin setting that never fired.
#
# ``providers._polish_model`` documented "env -> runtime config
# gd_polish_image_model (admin-settable) -> default", but the field was neither a
# ``Settings`` attribute nor a member of OVERRIDE_FIELDS, so ``runtime_config.get``
# returned "" on every call and the admin layer was unreachable dead code.
# --------------------------------------------------------------------------- #

def test_polish_model_field_is_a_real_settings_attribute():
    """The dead-path root cause: no Settings field, so get() always returned ""."""
    from app.config import settings

    assert isinstance(getattr(settings, "gd_polish_image_model", None), str)
    assert settings.gd_polish_image_model
    assert "gd_polish_image_model" in runtime_config.OVERRIDE_FIELDS
    assert "gd_polish_image_model" in runtime_config.AGENT_OVERRIDE_FIELDS


def test_polish_model_resolves_per_agent_override(app_config, monkeypatch):
    """An admin choice in the panel reaches the Stage-3 polish fan-out."""
    from graphics_designer_agent import providers

    monkeypatch.delenv("GD_POLISH_IMAGE_MODEL", raising=False)
    monkeypatch.setenv("GD_IMAGE_PROVIDER", "openrouter")

    assert providers._polish_model("a1") == "agent/a1-polish-model"
    # And it reaches the provider the pipeline actually builds.
    assert providers.get_polish_provider(agent_id="a1").model == "agent/a1-polish-model"


def test_polish_model_falls_back_global_then_env(app_config, monkeypatch):
    """agent -> global -> env, each layer proven by removing the one above it."""
    from app.config import settings
    from graphics_designer_agent import providers

    monkeypatch.delenv("GD_POLISH_IMAGE_MODEL", raising=False)

    app_config["agents"]["a1"].pop("gd_polish_image_model")
    assert providers._polish_model("a1") == "global/polish-model"

    app_config.pop("gd_polish_image_model")
    assert providers._polish_model("a1") == settings.gd_polish_image_model


def test_polish_model_env_var_still_outranks_the_admin_override(app_config, monkeypatch):
    """The docstring's first link in the chain: an explicit env pin wins."""
    from graphics_designer_agent import providers

    monkeypatch.setenv("GD_POLISH_IMAGE_MODEL", "openai/gpt-5.4-image-2")
    assert providers._polish_model("a1") == "openai/gpt-5.4-image-2"


def test_get_polish_provider_defaults_to_the_gd_agent_id(app_config, monkeypatch):
    """Callers that pass no agent_id must still land on GD's override rather than
    silently short-circuiting to the global value."""
    from graphics_designer_agent import providers

    monkeypatch.delenv("GD_POLISH_IMAGE_MODEL", raising=False)
    monkeypatch.setenv("GD_IMAGE_PROVIDER", "openrouter")
    assert providers.get_polish_provider().model == "agent/a1-polish-model"


def test_panel_surfaces_and_saves_the_polish_model(app_config, monkeypatch):
    """The panel must render the field AND round-trip a save - adding a field to
    AGENT_OVERRIDE_FIELDS without the matching request-body field 500s every
    agent save (the gd_planner_model regression, one field later)."""
    from app.services import model_catalog

    written: dict = {}

    def fake_set(agent_id, patch):
        written[agent_id] = patch
        return {}

    monkeypatch.setattr(firestore_repo, "set_agent_config", fake_set)
    monkeypatch.setattr(
        model_catalog, "allowed_ids",
        lambda field: {"google/gemini-3-pro-image-preview"},
    )

    r = client.get("/api/admin/agents")
    assert r.status_code == 200, r.text
    a1 = {a["id"]: a for a in r.json()["agents"]}["a1"]
    assert "gd_polish_image_model" in a1["fields"]
    assert a1["overrides"]["gd_polish_image_model"] == "agent/a1-polish-model"
    assert a1["effective"]["gd_polish_image_model"] == "agent/a1-polish-model"

    r = client.post(
        "/api/admin/agents/a1",
        json={"gd_polish_image_model": "google/gemini-3-pro-image-preview"},
    )
    assert r.status_code == 200, r.text
    assert written == {
        "a1": {"gd_polish_image_model": "google/gemini-3-pro-image-preview"}
    }


def test_polish_model_is_not_offered_to_agents_that_do_not_use_it(app_config, monkeypatch):
    monkeypatch.setattr(firestore_repo, "set_agent_config", lambda *a, **k: {})
    r = client.post(
        "/api/admin/agents/a6",
        json={"gd_polish_image_model": "google/gemini-3-pro-image-preview"},
    )
    assert r.status_code == 400
    assert "does not use" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Defect 2 - all three GD vision paths must read the per-agent vision model.
#
# providers._agent_image_model already used get_for_agent; the two Stage-3 vision
# brains called runtime_config.get (global only), so the panel's vision dropdown
# changed one GD vision path in three.
# --------------------------------------------------------------------------- #

def _capture_analyze_images(monkeypatch) -> list[str]:
    seen: list[str] = []

    def fake(prompt, images, model=None, **_kw):
        seen.append(model)
        return "{}"

    monkeypatch.setattr(openrouter_service, "analyze_images", fake)
    return seen


def test_placement_brain_uses_the_a1_vision_override(app_config, monkeypatch):
    from graphics_designer_agent.stage3_text import placement_brain

    seen = _capture_analyze_images(monkeypatch)
    placement_brain._call_model("prompt", b"png-bytes")
    assert seen == ["agent/a1-vision-model"]


def test_qa_brain_uses_the_a1_vision_override(app_config, monkeypatch):
    from graphics_designer_agent.stage3_text import qa_brain

    seen = _capture_analyze_images(monkeypatch)
    qa_brain._call_model("prompt", [b"a", b"b"])
    assert seen == ["agent/a1-vision-model"]


def test_vision_paths_inherit_the_global_model_when_a1_has_no_override(
    app_config, monkeypatch
):
    from graphics_designer_agent.stage3_text import placement_brain, qa_brain

    app_config["agents"]["a1"].pop("openrouter_vision_model")
    seen = _capture_analyze_images(monkeypatch)
    placement_brain._call_model("p", b"png")
    qa_brain._call_model("p", [b"a", b"b"])
    assert seen == ["global/vision-model", "global/vision-model"]


def test_all_three_gd_vision_paths_agree_on_the_model(app_config, monkeypatch):
    """One dropdown, three paths - the point of the defect."""
    from graphics_designer_agent.stage3_text import placement_brain, qa_brain

    seen = _capture_analyze_images(monkeypatch)
    placement_brain._call_model("p", b"png")
    qa_brain._call_model("p", [b"a", b"b"])
    direct = runtime_config.get_for_agent("a1", "openrouter_vision_model")
    assert set(seen) == {direct} == {"agent/a1-vision-model"}


# --------------------------------------------------------------------------- #
# Defect 3 - agent_id is an optional kwarg, so omitting it fails silently.
#
# get_llm(agent_id=None) short-circuits get_for_agent to the global value. The
# signature keeps the default (22 call sites, several of which pin ``model=``
# explicitly and have no meaningful agent identity), so the guard is a static
# scan instead: every production call site must bind one or the other.
# --------------------------------------------------------------------------- #

def _agent_get_llm_call_sites() -> list[tuple[str, int, set]]:
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "agents"
    sites: list[tuple[str, int, set]] = []
    for path in sorted(root.rglob("*.py")):
        parts = {p.lower() for p in path.parts}
        if "tests" in parts or path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else ""
            )
            if name != "get_llm":
                continue
            keys = {k.arg for k in node.keywords}
            sites.append((str(path.relative_to(root)), node.lineno, keys))
    return sites


def test_every_agent_get_llm_call_binds_an_agent_or_pins_a_model():
    """Regression for the GD call sites that omitted agent_id: an omission is
    invisible at runtime (it just resolves globally), so it is pinned here."""
    sites = _agent_get_llm_call_sites()
    assert sites, "static scan found no get_llm call sites - the scan is broken"

    unbound = [
        (path, line) for path, line, keys in sites
        # ``**kwargs`` (arg is None) means a wrapper; the wrapper is asserted below.
        if not ({"agent_id", "model"} & keys) and None not in keys
    ]
    assert unbound == [], f"get_llm called without agent_id or model: {unbound}"


def test_gd_suggestions_wrapper_binds_a1(app_config, monkeypatch):
    """The only ``**kwargs`` wrapper the scan waives must actually bind GD."""
    from graphics_designer_agent import suggestions

    seen: list = []

    def fake_get_llm(*_a, **kw):
        seen.append(kw.get("agent_id"))
        return _StubLLM()

    monkeypatch.setattr(openrouter_service, "get_llm", fake_get_llm)
    suggestions._get_llm(temperature=0.9, fast=True)
    assert seen == ["a1"]


def test_gd_remix_reaches_the_a1_fast_override(app_config):
    """remix goes through the wrapper, so the a1 fast override lands for real."""
    from graphics_designer_agent import remix

    llm = remix._get_llm(temperature=0.9, fast=True)
    assert llm.model_name == "anthropic/claude-haiku-4.5"


# --------------------------------------------------------------------------- #
# Defect 4 - default_image_model() read settings directly, bypassing the chain.
# --------------------------------------------------------------------------- #

def test_default_image_model_honours_the_global_admin_override(app_config):
    from app.services import agent_config

    assert agent_config.default_image_model() == "google/gemini-2.5-flash-image"
    # normalize_settings routes through the same function.
    assert agent_config.normalize_settings(None, None, None)["image_model"] == (
        "google/gemini-2.5-flash-image"
    )


def test_default_image_model_falls_back_to_env_without_an_override(app_config):
    from app.config import settings
    from app.services import agent_config

    app_config.pop("openrouter_image_model")
    assert agent_config.default_image_model() == settings.openrouter_image_model


def test_default_image_model_rejects_an_override_outside_the_catalog(app_config):
    """An unknown id must not be handed to the image provider - fall back to the
    vetted default rather than shipping a 400 from OpenRouter mid-generation."""
    from app.services import agent_config

    app_config["openrouter_image_model"] = "someone/typo-model"
    assert agent_config.default_image_model() in agent_config.IMAGE_MODEL_IDS
