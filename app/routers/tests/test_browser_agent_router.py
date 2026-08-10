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


def test_get_run_hides_replay_cache(monkeypatch):
    _stub(monkeypatch, actions.Action(kind="wait", why="settle"))
    run = _create()
    _step(run["run_id"], 1)
    body = client.get(f"/api/browser/runs/{run['run_id']}").json()
    assert "last_decision" not in body
    assert body["steps"][0]["action"]["kind"] == "wait"
