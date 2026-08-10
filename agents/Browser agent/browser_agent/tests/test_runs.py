"""Run state machine: idempotent seq, policy veto, sensitive confirm, caps.

Fully offline (BROWSER_OFFLINE=1 via conftest). The brain is stubbed so each
test pins the loop mechanics, not the model.
"""
from __future__ import annotations

import json

import app  # noqa: F401 - registers agent roots on sys.path
import pytest

from browser_agent import actions, brain, planner, runs

USER = {"id": "u1", "email": "owner@legalsoft.com"}


_PLAN = {
    "subtasks": [
        {"id": "s1", "title": "Open compose", "goal": "open it", "steps": [],
         "edge_cases": [], "done_when": "visible", "status": "pending"},
        {"id": "s2", "title": "Fill recipient", "goal": "fill it", "steps": [],
         "edge_cases": [], "done_when": "filled", "status": "pending"},
    ],
    "notes": "", "planned": True, "goal": "send an email",
}


@pytest.fixture(autouse=True)
def _local(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSER_OFFLINE", "1")
    monkeypatch.setenv("BROWSER_LOCAL_DIR", str(tmp_path / "browser_state"))
    # Planning is exercised in test_planner; here it must not make a real call.
    monkeypatch.setattr(
        planner, "build_plan",
        lambda goal, **_kw: json.loads(json.dumps(_PLAN)) | {"goal": goal},
    )


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


def _page(seq: int, *, url="https://mail.google.com/", labels=("To", "Subject"), **kw):
    """One observation of a screen; same labels + url = the same screen."""
    return _step_body(
        seq,
        tab={"id": 1, "url": url, "title": "Gmail"},
        dom={"elements": [{"i": i, "tag": "div", "text": t} for i, t in enumerate(labels)]},
        **kw,
    )


def test_repeated_click_on_an_unchanged_page_gives_up(monkeypatch):
    """The real Gmail failure: the click 'succeeds' every time (CDP dispatched
    it) but the page never moves, so counting failures alone never fires."""
    _stub_brain(monkeypatch, actions.Action(kind="click", index=0, why="focus the To field"))
    run = runs.create_run(USER, "send an email", "act", None)
    resp = None
    for seq in range(1, runs._STUCK_AFTER + 1):
        run, resp = runs.step(run, _page(seq, last_result={"ok": True}))
    assert resp["action"]["kind"] == "fail"
    assert "never changed" in resp["action"]["reason"]
    assert run["steps_used"] < run["step_cap"]


def test_same_action_on_a_changed_page_is_not_stuck(monkeypatch):
    """Scrolling repeats the same step by design; each scroll shows new elements."""
    _stub_brain(monkeypatch, actions.Action(kind="scroll", why="more"))
    run = runs.create_run(USER, "read it all", "act", None)
    resp = None
    for seq in range(1, runs._STUCK_AFTER + 2):
        run, resp = runs.step(
            run, _page(seq, labels=(f"row {seq}", f"row {seq + 1}"), last_result={"ok": True})
        )
    assert resp["action"]["kind"] == "scroll"
    assert run["status"] == "running"


def test_varied_flailing_on_one_screen_is_caught(monkeypatch):
    """The Google Sheets run: Escape, click, reload, Escape, click… every action
    different, so the repeat guard never fires, and 40 steps go nowhere."""
    moves = [
        actions.Action(kind="key", text="Escape", why="close overlays"),
        actions.Action(kind="click", index=10, why="close the panel"),
        actions.Action(kind="key", text="Escape", why="close overlays"),
        actions.Action(kind="navigate", url="https://docs.google.com/x", why="reload"),
        actions.Action(kind="click", index=19, why="close the popup"),
        actions.Action(kind="key", text="Escape", why="close overlays"),
        actions.Action(kind="click", index=88, why="close another panel"),
        actions.Action(kind="scroll", why="look around"),
        actions.Action(kind="key", text="Escape", why="close overlays"),
    ]
    it = iter(moves)
    monkeypatch.setattr(brain, "decide", lambda run, obs: next(it, moves[-1]))

    run = runs.create_run(USER, "put the names in the sheet", "act", None)
    resp = None
    for seq in range(1, runs._NO_PROGRESS_AFTER + 2):
        run, resp = runs.step(run, _page(seq, last_result={"ok": True}))
        if resp["done"]:
            break
    assert resp["action"]["kind"] == "fail"
    assert "no progress" in resp["action"]["reason"]
    assert run["steps_used"] <= runs._NO_PROGRESS_AFTER + 1


def test_canvas_app_failure_says_why(monkeypatch):
    monkeypatch.setattr(
        brain, "decide", lambda run, obs: actions.Action(kind="key", text="Escape", why="x")
    )
    run = runs.create_run(USER, "type into the sheet", "act", None)
    resp = None
    for seq in range(1, runs._NO_PROGRESS_AFTER + 2):
        body = _page(seq, last_result={"ok": True})
        body["dom"]["canvas_app"] = True
        run, resp = runs.step(run, body)
        if resp["done"]:
            break
    assert resp["action"]["kind"] == "fail"
    assert "canvas" in resp["action"]["reason"]


def test_fingerprint_notices_a_change_anywhere_in_the_list(monkeypatch):
    """An autocomplete dropdown opening below the fold is real progress; a
    fingerprint that only looked at the first few labels would miss it and call
    a working run stuck."""
    base = _page(1, labels=tuple(f"row {i}" for i in range(20)))
    changed = _page(1, labels=tuple(f"row {i}" for i in range(19)) + ("suggestion",))
    assert runs.page_fingerprint(base) != runs.page_fingerprint(changed)


def test_fingerprint_is_stable_for_an_unchanged_screen():
    a = _page(1, labels=("To", "Subject", "Body"))
    b = _page(2, labels=("To", "Subject", "Body"))
    assert runs.page_fingerprint(a) == runs.page_fingerprint(b)


def test_progress_resets_the_stall_counter(monkeypatch):
    """A screen that keeps changing is work, however long it takes."""
    _stub_brain(monkeypatch, actions.Action(kind="scroll", why="more"))
    run = runs.create_run(USER, "read a long page", "act", None)
    resp = None
    for seq in range(1, runs._NO_PROGRESS_AFTER + 4):
        run, resp = runs.step(
            run, _page(seq, labels=(f"row {seq}",), last_result={"ok": True})
        )
    assert resp["action"]["kind"] == "scroll"
    assert run["status"] == "running"


def test_screenshot_is_requested_once_repetition_starts(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="click", index=0, why="focus"))
    run = runs.create_run(USER, "send an email", "act", None)

    run, first = runs.step(run, _page(1, last_result={"ok": True}))
    assert first["want_screenshot"] is False  # one attempt is not a loop

    run, second = runs.step(run, _page(2, last_result={"ok": True}))
    assert second["want_screenshot"] is True  # same click, same screen — look


def test_no_screenshot_requested_when_one_was_supplied(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="click", index=0, why="focus"))
    run = runs.create_run(USER, "send an email", "act", None)
    run, _ = runs.step(run, _page(1, last_result={"ok": True}))
    run, resp = runs.step(
        run, _page(2, last_result={"ok": True}, screenshot="data:image/jpeg;base64,AAAA")
    )
    assert resp["want_screenshot"] is False


def test_screenshot_reaches_the_brain(monkeypatch):
    seen: dict = {}

    def capture(run, obs):
        seen["screenshot"] = obs.get("screenshot")
        return actions.Action(kind="wait", why="settle")

    monkeypatch.setattr(brain, "decide", capture)
    run = runs.create_run(USER, "goal", "act", None)
    runs.step(run, _page(1, screenshot="data:image/jpeg;base64,ZZZ"))
    assert seen["screenshot"] == "data:image/jpeg;base64,ZZZ"


# --------------------------------------------------------------------------- #
# Plan-driven execution
# --------------------------------------------------------------------------- #

def test_run_starts_on_the_first_subtask():
    run = runs.create_run(USER, "send an email", "act", None)
    assert run["plan"]["subtasks"][0]["title"] == "Open compose"
    assert planner.current(run["plan"])["id"] == "s1"


def test_next_subtask_advances_and_asks_again_for_a_real_action(monkeypatch):
    """The model saying "milestone reached" must still yield ONE executable
    action, because the extension has nothing to do with a bookkeeping reply."""
    replies = iter([
        actions.Action(kind="next_subtask", why="compose is open"),
        actions.Action(kind="click", index=3, why="focus the To field"),
    ])
    monkeypatch.setattr(brain, "decide", lambda run, obs: next(replies))

    run = runs.create_run(USER, "send an email", "act", None)
    run, resp = runs.step(run, _step_body(1))

    assert resp["action"]["kind"] == "click"  # never "next_subtask"
    assert planner.current(run["plan"])["id"] == "s2"
    assert resp["subtask"] == "Fill recipient"
    assert resp["subtask_progress"] == [1, 2]
    assert run["steps"][-1]["completed"] == ["Open compose"]


def test_finishing_the_last_subtask_completes_the_run(monkeypatch):
    monkeypatch.setattr(
        brain, "decide", lambda run, obs: actions.Action(kind="next_subtask", why="done")
    )
    run = runs.create_run(USER, "send an email", "act", None)
    run, resp = runs.step(run, _step_body(1))
    assert run["status"] == "completed"
    assert resp["done"] is True
    assert planner.progress(run["plan"]) == (2, 2)


def test_steps_record_which_subtask_they_belong_to(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="wait", why="settle"))
    run = runs.create_run(USER, "send an email", "act", None)
    run, _ = runs.step(run, _step_body(1))
    assert run["steps"][-1]["subtask"] == "Open compose"


def test_a_failed_plan_still_runs(monkeypatch):
    monkeypatch.setattr(
        planner, "build_plan", lambda goal, **_kw: planner.fallback_plan(goal, "model down")
    )
    _stub_brain(monkeypatch, actions.Action(kind="wait", why="settle"))
    run = runs.create_run(USER, "do a thing", "act", None)
    assert run["plan"]["planned"] is False
    run, resp = runs.step(run, _step_body(1))
    assert resp["action"]["kind"] == "wait"  # degraded, not dead


def test_extracted_result_becomes_a_finding(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="scroll", why="more"))
    run = runs.create_run(USER, "research", "act", None)
    run, _ = runs.step(run, _step_body(1))
    run, _ = runs.step(run, _step_body(2, last_result={"ok": True, "extracted": "a headline"}))
    assert "a headline" in run["findings"]
