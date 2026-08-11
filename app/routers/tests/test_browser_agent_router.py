"""Integration tests for the Browser Agent router (/api/browser). Fully offline.

The run state machine runs end-to-end; only the LLM brain is stubbed."""
from __future__ import annotations

import os

os.environ["BROWSER_OFFLINE"] = "1"

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import get_current_user
from browser_agent import actions, brain

client = TestClient(fastapi_app)

USER = {"id": "u1", "email": "owner@legalsoft.com", "is_admin": False,
        "is_creator": False, "session_id": "", "timezone": "UTC"}


@pytest.fixture(autouse=True)
def _harness(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSER_OFFLINE", "1")
    monkeypatch.setenv("BROWSER_LOCAL_DIR", str(tmp_path / "browser_state"))
    monkeypatch.delenv("BROWSER_AGENT_DISABLED", raising=False)
    prev = fastapi_app.dependency_overrides.get(get_current_user)
    fastapi_app.dependency_overrides[get_current_user] = lambda: dict(USER)
    yield
    if prev is None:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    else:
        fastapi_app.dependency_overrides[get_current_user] = prev


def _stub(monkeypatch, action: actions.Action):
    monkeypatch.setattr(brain, "decide", lambda run, obs: action)


def _create(goal="Find the top AI story", mode="act"):
    r = client.post("/api/browser/runs", json={"goal": goal, "mode": mode})
    assert r.status_code == 200, r.text
    return r.json()


def _step(run_id, seq, **kw):
    body = {"protocol": actions.PROTOCOL, "seq": seq, "dom": {"elements": []}, **kw}
    return client.post(f"/api/browser/runs/{run_id}/step", json=body)


def test_status_reports_email_and_policy():
    r = client.get("/api/browser/status")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "owner@legalsoft.com"
    assert body["protocol"] == actions.PROTOCOL
    assert "blocked" in body


def test_requires_auth():
    fastapi_app.dependency_overrides.pop(get_current_user, None)
    try:
        assert client.get("/api/browser/status").status_code in (401, 403)
    finally:
        fastapi_app.dependency_overrides[get_current_user] = lambda: dict(USER)


def test_kill_switch_403(monkeypatch):
    monkeypatch.setenv("BROWSER_AGENT_DISABLED", "1")
    r = client.post("/api/browser/runs", json={"goal": "anything"})
    assert r.status_code == 403
    assert "BROWSER_AGENT_DISABLED" in r.json()["detail"]


def test_full_step_loop(monkeypatch):
    _stub(monkeypatch, actions.Action(kind="click", index=0, why="open story"))
    run = _create()
    r = _step(run["run_id"], 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"]["kind"] == "click"
    assert body["done"] is False
    assert body["steps_remaining"] == run["step_cap"] - 1


def test_out_of_sync_step_is_409(monkeypatch):
    _stub(monkeypatch, actions.Action(kind="wait", why="settle"))
    run = _create()
    assert _step(run["run_id"], 7).status_code == 409


def test_protocol_mismatch_is_409():
    run = _create()
    r = client.post(
        f"/api/browser/runs/{run['run_id']}/step",
        json={"protocol": 999, "seq": 1, "dom": {"elements": []}},
    )
    assert r.status_code == 409
    assert "update the extension" in r.json()["detail"]


def test_done_completes_and_lists(monkeypatch):
    _stub(monkeypatch, actions.Action(kind="done", summary="3 stories found", why="done"))
    run = _create()
    r = _step(run["run_id"], 1)
    assert r.json()["done"] is True
    listed = client.get("/api/browser/runs").json()["runs"]
    assert any(x["id"] == run["run_id"] and x["status"] == "completed" for x in listed)


def test_stop_marks_stopped():
    run = _create()
    r = client.post(f"/api/browser/runs/{run['run_id']}/stop")
    assert r.status_code == 200 and r.json()["status"] == "stopped"


def test_other_users_run_is_404(monkeypatch):
    _stub(monkeypatch, actions.Action(kind="wait", why="settle"))
    run = _create()
    fastapi_app.dependency_overrides[get_current_user] = lambda: {
        "id": "u2", "email": "someone-else@x.com", "is_admin": False,
        "session_id": "", "timezone": "UTC",
    }
    assert client.get(f"/api/browser/runs/{run['run_id']}").status_code == 404


# --------------------------------------------------------------------------- #
# Extension download — company accounts only (console login is NOT domain-gated)
# --------------------------------------------------------------------------- #

def _as(email: str, *, creator: bool = False, admin: bool = False) -> None:
    fastapi_app.dependency_overrides[get_current_user] = lambda: {
        "id": "u9", "email": email, "is_admin": admin, "is_creator": creator,
        "session_id": "", "timezone": "UTC",
    }


def test_company_account_downloads_the_extension():
    r = client.get("/api/browser/extension")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert len(r.content) > 1000


def test_outside_account_is_refused_with_a_clear_reason():
    _as("stranger@gmail.com")
    r = client.get("/api/browser/extension")
    assert r.status_code == 403
    assert "legalsoft.com" in r.json()["detail"]


def test_owner_on_another_domain_still_gets_it():
    # CREATOR_EMAILS_DEFAULT includes a gmail.com owner — a blanket domain rule
    # would lock them out of their own tool.
    _as("owner@gmail.com", creator=True)
    assert client.get("/api/browser/extension").status_code == 200


def test_allowed_domains_are_configurable(monkeypatch):
    monkeypatch.setenv("BROWSER_EXT_DOMAINS", "example.org")
    _as("someone@example.org")
    assert client.get("/api/browser/extension").status_code == 200
    _as("someone@legalsoft.com")
    assert client.get("/api/browser/extension").status_code == 403


def test_status_reports_download_eligibility_and_version():
    body = client.get("/api/browser/status").json()
    assert body["can_download"] is True
    assert body["extension_version"]

    _as("stranger@gmail.com")
    assert client.get("/api/browser/status").json()["can_download"] is False


def test_download_needs_auth():
    fastapi_app.dependency_overrides.pop(get_current_user, None)
    try:
        assert client.get("/api/browser/extension").status_code in (401, 403)
    finally:
        fastapi_app.dependency_overrides[get_current_user] = lambda: dict(USER)


def test_missing_bundle_is_honest_503(monkeypatch):
    from app.routers import browser_agent as router_mod
    from pathlib import Path

    monkeypatch.setattr(router_mod, "EXTENSION_ZIP", Path("nope.zip"))
    r = client.get("/api/browser/extension")
    assert r.status_code == 503
    assert "missing" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Skills — saved routes are private, and only saved on purpose
# --------------------------------------------------------------------------- #

_ROUTE = [
    {"kind": "navigate", "url": "https://example.com/"},
    {"kind": "click", "expect": "Learn more", "role": "a"},
]


def _save(name="Open the info page", **kw):
    return client.post("/api/browser/skills", json={"name": name, "steps": _ROUTE,
                                                    "goal": "open the info page",
                                                    "host": "example.com", **kw})


def test_save_then_list_a_skill():
    r = _save()
    assert r.status_code == 200, r.text
    assert len(r.json()["steps"]) == 2
    listed = client.get("/api/browser/skills").json()["skills"]
    assert listed[0]["name"] == "Open the info page"


def test_saving_something_unrepeatable_is_a_clear_422():
    r = client.post("/api/browser/skills", json={"name": "Nothing", "steps": [{"kind": "wait"}]})
    assert r.status_code == 422
    assert "repeatable" in r.json()["detail"]


def test_a_skill_learned_from_a_run_uses_the_runs_own_steps(monkeypatch):
    from browser_agent import brain

    monkeypatch.setattr(
        brain, "decide",
        lambda run, obs: actions.Action(kind="navigate", url="https://example.com/", why="go"),
    )
    run = _create()
    _step(run["run_id"], 1)
    _step(run["run_id"], 2, last_result={"ok": True})  # confirms step 1 worked

    r = client.post("/api/browser/skills", json={"name": "From the run", "run_id": run["run_id"]})
    assert r.status_code == 200, r.text
    assert r.json()["steps"][0]["url"] == "https://example.com/"


def test_another_users_skill_is_invisible_and_undeletable():
    skill_id = _save().json()["id"]
    _as("stranger@legalsoft.com")
    assert client.get(f"/api/browser/skills/{skill_id}").status_code == 404
    assert client.delete(f"/api/browser/skills/{skill_id}").status_code == 404
    assert client.get("/api/browser/skills").json()["skills"] == []


def test_an_admin_can_see_and_remove_any_skill():
    skill_id = _save().json()["id"]
    _as("boss@legalsoft.com", admin=True)
    assert client.get(f"/api/browser/skills/{skill_id}").status_code == 200
    assert client.delete(f"/api/browser/skills/{skill_id}").status_code == 200


def test_deleting_removes_it_for_good():
    skill_id = _save().json()["id"]
    assert client.delete(f"/api/browser/skills/{skill_id}").status_code == 200
    assert client.get(f"/api/browser/skills/{skill_id}").status_code == 404
    assert client.get("/api/browser/skills").json()["skills"] == []


def test_skills_need_auth():
    fastapi_app.dependency_overrides.pop(get_current_user, None)
    try:
        assert client.get("/api/browser/skills").status_code in (401, 403)
    finally:
        fastapi_app.dependency_overrides[get_current_user] = lambda: dict(USER)


def test_kill_switch_covers_skills(monkeypatch):
    monkeypatch.setenv("BROWSER_AGENT_DISABLED", "1")
    for call in (
        lambda: client.get("/api/browser/skills"),
        lambda: _save(),
        lambda: client.get("/api/browser/skills/anything"),
        lambda: client.delete("/api/browser/skills/anything"),
    ):
        assert call().status_code == 403


def test_get_run_hides_replay_cache(monkeypatch):
    _stub(monkeypatch, actions.Action(kind="wait", why="settle"))
    run = _create()
    _step(run["run_id"], 1)
    body = client.get(f"/api/browser/runs/{run['run_id']}").json()
    assert "last_decision" not in body
    assert body["steps"][0]["action"]["kind"] == "wait"
