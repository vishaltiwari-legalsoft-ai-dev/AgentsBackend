"""Run state machine: idempotent seq, policy veto, sensitive confirm, caps.

Fully offline (BROWSER_OFFLINE=1 via conftest). The brain is stubbed so each
test pins the loop mechanics, not the model.
"""
from __future__ import annotations

import app  # noqa: F401 - registers agent roots on sys.path
import pytest

from browser_agent import actions, brain, runs

USER = {"id": "u1", "email": "owner@legalsoft.com"}


@pytest.fixture(autouse=True)
def _local(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSER_OFFLINE", "1")
    monkeypatch.setenv("BROWSER_LOCAL_DIR", str(tmp_path / "browser_state"))


def _stub_brain(monkeypatch, action: actions.Action):
    monkeypatch.setattr(brain, "decide", lambda run, obs: action)


def _step_body(seq: int, **kw) -> dict:
    return {"protocol": actions.PROTOCOL, "seq": seq, "dom": {"elements": []}, **kw}


def test_create_run_seeds_policy():
    run = runs.create_run(USER, "do a thing", "act", None)
    assert run["status"] == "running"
    assert run["step_cap"] >= 1
    assert run["user_id"] == "u1"


def test_step_returns_action_and_advances_seq(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="wait", why="settle"))
    run = runs.create_run(USER, "goal", "act", None)
    run, resp = runs.step(run, _step_body(1))
    assert resp["action"]["kind"] == "wait"
    assert run["seq"] == 1 and run["steps_used"] == 1


def test_replayed_seq_returns_cached_decision(monkeypatch):
    calls = {"n": 0}

    def counting(run, obs):
        calls["n"] += 1
        return actions.Action(kind="wait", why="settle")

    monkeypatch.setattr(brain, "decide", counting)
    run = runs.create_run(USER, "goal", "act", None)
    run, first = runs.step(run, _step_body(1))
    run, again = runs.step(run, _step_body(1))  # same seq = resume
    assert calls["n"] == 1  # brain not consulted twice
    assert again["action"] == first["action"]


def test_out_of_order_seq_is_409(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="wait", why="settle"))
    run = runs.create_run(USER, "goal", "act", None)
    with pytest.raises(runs.OutOfSync):
        runs.step(run, _step_body(5))


def test_protocol_mismatch_raises(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="wait", why="settle"))
    run = runs.create_run(USER, "goal", "act", None)
    with pytest.raises(runs.ProtocolMismatch):
        runs.step(run, {"protocol": 999, "seq": 1, "dom": {"elements": []}})


def test_monitor_mode_vetoes_mutations(monkeypatch):
    # Brain proposes a click both times; policy must convert it to an honest fail.
    _stub_brain(monkeypatch, actions.Action(kind="click", index=0, why="go"))
    run = runs.create_run(USER, "watch", "monitor", None)
    run, resp = runs.step(run, _step_body(1))
    assert resp["action"]["kind"] == "fail"
    assert "monitor" in resp["action"]["reason"]


def test_blocked_domain_navigation_fails(monkeypatch):
    _stub_brain(
        monkeypatch, actions.Action(kind="navigate", url="https://paypal.com/pay", why="pay")
    )
    run = runs.create_run(USER, "goal", "act", None)
    run, resp = runs.step(run, _step_body(1))
    assert resp["action"]["kind"] == "fail"


def test_sensitive_action_awaits_confirmation(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="click", index=0, why="pay"))
    run = runs.create_run(USER, "buy it", "act", None)
    body = _step_body(1, dom={"elements": [{"i": 0, "tag": "button", "text": "Pay now"}]})
    run, resp = runs.step(run, body)
    assert resp["requires_confirmation"] is True
    assert run["status"] == "awaiting_confirmation"

    # Re-POST same seq with confirmed=true → cleared to run.
    run, resp2 = runs.step(run, dict(body, confirmed=True))
    assert resp2["requires_confirmation"] is False
    assert run["status"] == "running"


def test_step_cap_ends_run_honestly(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="wait", why="settle"))
    run = runs.create_run(USER, "goal", "act", None)
    run["step_cap"] = 1
    run, _ = runs.step(run, _step_body(1))          # uses the one allowed step
    run, resp = runs.step(run, _step_body(2))       # over the cap
    assert run["status"] == "failed"
    assert "step cap" in (run["fail_reason"] or "")
    assert resp["done"] is True


def test_done_marks_completed_and_indexes(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="done", summary="all set", why="finished"))
    run = runs.create_run(USER, "goal", "act", None)
    run, resp = runs.step(run, _step_body(1))
    assert run["status"] == "completed" and resp["summary"] == "all set"
    listed = {r["id"]: r for r in runs.list_runs(user_id="u1")}
    assert listed[run["id"]]["status"] == "completed"


def test_stop_is_terminal(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="wait", why="settle"))
    run = runs.create_run(USER, "goal", "act", None)
    run = runs.stop_run(run)
    assert run["status"] == "stopped"
    run, resp = runs.step(run, _step_body(1))  # further steps are no-ops
    assert resp["done"] is True and run["status"] == "stopped"


def test_repeated_failing_action_gives_up_honestly(monkeypatch):
    """The click-type-click loop seen on a real site: the same step, failing,
    forever. It should stop and say so, not burn the whole step cap."""
    _stub_brain(monkeypatch, actions.Action(kind="click", index=0, why="try again"))
    run = runs.create_run(USER, "goal", "act", None)
    body = lambda seq: _step_body(  # noqa: E731
        seq, last_result={"ok": False, "error": "nothing happened"}
    )
    resp = None
    for seq in range(1, runs._STUCK_AFTER + 1):
        run, resp = runs.step(run, body(seq))
    assert resp["action"]["kind"] == "fail"
    assert "same" in resp["action"]["reason"]
    assert run["steps_used"] < run["step_cap"]  # gave up early, as intended


def test_repeating_a_working_action_is_fine(monkeypatch):
    """Scrolling a long page repeats the same step on purpose — not stuck."""
    _stub_brain(monkeypatch, actions.Action(kind="scroll", why="more"))
    run = runs.create_run(USER, "read it all", "act", None)
    resp = None
    for seq in range(1, runs._STUCK_AFTER + 2):
        run, resp = runs.step(run, _step_body(seq, last_result={"ok": True}))
    assert resp["action"]["kind"] == "scroll"
    assert run["status"] == "running"


def test_extracted_result_becomes_a_finding(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="scroll", why="more"))
    run = runs.create_run(USER, "research", "act", None)
    run, _ = runs.step(run, _step_body(1))
    run, _ = runs.step(run, _step_body(2, last_result={"ok": True, "extracted": "a headline"}))
    assert "a headline" in run["findings"]
