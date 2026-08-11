"""Run state machine: idempotent seq, policy veto, sensitive confirm, caps.

Fully offline (BROWSER_OFFLINE=1 via conftest). The brain is stubbed so each
test pins the loop mechanics, not the model.
"""
from __future__ import annotations

import json

import app  # noqa: F401 - registers agent roots on sys.path
import pytest

from browser_agent import actions, brain, planner, runs, skills, tools

USER = {"id": "u1", "email": "owner@legalsoft.com"}


_PLAN = {
    "subtasks": [
        {"id": "s1", "title": "Open compose", "goal": "open it", "rail": "browser",
         "steps": [], "edge_cases": [], "done_when": "visible", "status": "pending"},
        {"id": "s2", "title": "Fill recipient", "goal": "fill it", "rail": "browser",
         "steps": [], "edge_cases": [], "done_when": "filled", "status": "pending"},
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
    _stub_brain(monkeypatch, actions.Action(kind="click", index=0, expect="To", why="focus"))
    run = runs.create_run(USER, "send an email", "act", None)
    resp = None
    for seq in range(1, runs._STUCK_AFTER + 1):
        run, resp = runs.step(run, _page(seq, last_result={"ok": True}))
    # Stopping is the point; with a person present we ask them rather than fail.
    assert resp["action"]["kind"] == "ask_user"
    assert "never changed" in resp["action"]["text"]
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
        if resp["done"] or run["status"] == "awaiting_user":
            break
    assert resp["action"]["kind"] == "ask_user"
    assert "no progress" in resp["action"]["text"]
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


# --------------------------------------------------------------------------- #
# The tool rail — APIs instead of clicking
# --------------------------------------------------------------------------- #

def test_a_tool_call_never_reaches_the_extension(monkeypatch):
    """The whole point of INTERNAL_KINDS: the browser gets one executable
    action per step, whatever happened server-side to produce it."""
    replies = iter([
        actions.Action(kind="tool", tool="sheet_read", sheet="https://x/d/abc123", why="read it"),
        actions.Action(kind="done", summary="Read the sheet", why="have the data"),
    ])
    monkeypatch.setattr(brain, "decide", lambda run, obs: next(replies))
    monkeypatch.setattr(tools, "run_tool", lambda name, args: {"rows": [["a"]]})

    run = runs.create_run(USER, "read my sheet", "act", None)
    run, resp = runs.step(run, _step_body(1))

    assert resp["action"]["kind"] == "done"          # never "tool"
    assert run["tool_calls"][0]["tool"] == "sheet_read"
    assert run["steps"][-1]["tools"] == [{"tool": "sheet_read", "ok": True, "error": None}]


def test_tool_result_is_kept_as_a_finding(monkeypatch):
    replies = iter([
        actions.Action(kind="tool", tool="web_search", query="gyms", why="search"),
        actions.Action(kind="wait", why="settle"),
    ])
    monkeypatch.setattr(brain, "decide", lambda run, obs: next(replies))
    monkeypatch.setattr(tools, "run_tool", lambda n, a: {"results": [{"title": "Gold Gym"}]})

    run = runs.create_run(USER, "find gyms", "act", None)
    run, _ = runs.step(run, _step_body(1))
    assert any("Gold Gym" in f for f in run["findings"])


def test_a_failing_tool_does_not_end_the_run(monkeypatch):
    """A missing share is a thing to report and work around, not a crash."""
    replies = iter([
        actions.Action(kind="tool", tool="sheet_append", sheet="https://x/d/abc123",
                       rows=[["a"]], why="write it"),
        actions.Action(kind="ask_user", text="Please share the sheet with me", why="blocked"),
    ])
    monkeypatch.setattr(brain, "decide", lambda run, obs: next(replies))

    def denied(name, args):
        raise tools.ToolError("I don't have Editor access to that sheet.")

    monkeypatch.setattr(tools, "run_tool", denied)

    run = runs.create_run(USER, "write to my sheet", "act", None)
    run, resp = runs.step(run, _step_body(1))
    assert run["status"] == "awaiting_user"
    assert resp["action"]["kind"] == "ask_user"
    assert run["tool_calls"][0]["ok"] is False
    assert "Editor" in run["tool_calls"][0]["error"]


def test_a_tool_that_explodes_is_contained(monkeypatch):
    replies = iter([
        actions.Action(kind="tool", tool="sheet_read", sheet="https://x/d/abc", why="read"),
        actions.Action(kind="fail", reason="couldn't get the data", why="tool broke"),
    ])
    monkeypatch.setattr(brain, "decide", lambda run, obs: next(replies))

    def explode(name, args):
        raise ZeroDivisionError("boom")

    monkeypatch.setattr(tools, "run_tool", explode)
    run = runs.create_run(USER, "read", "act", None)
    run, resp = runs.step(run, _step_body(1))
    assert resp["action"]["kind"] == "fail"          # honest, not a 500
    assert run["tool_calls"][0]["ok"] is False


def test_tools_per_step_are_capped(monkeypatch):
    """Bounds the latency of one HTTP step; the run continues next step."""
    monkeypatch.setattr(
        brain, "decide",
        lambda run, obs: actions.Action(kind="tool", tool="web_search", query="x", why="again"),
    )
    monkeypatch.setattr(tools, "run_tool", lambda n, a: {"results": []})

    run = runs.create_run(USER, "search a lot", "act", None)
    run, resp = runs.step(run, _step_body(1))
    assert len(run["tool_calls"]) == runs._MAX_TOOLS_PER_STEP
    assert resp["action"]["kind"] == "wait"
    assert run["status"] == "running"                # not failed — just next step


def test_tool_calls_are_never_flagged_sensitive(monkeypatch):
    """Appending to its own tab is not the irreversible kind of step."""
    act = actions.Action(kind="tool", tool="sheet_append", sheet="https://x/d/a",
                         rows=[["delete everything"]], why="write")
    assert actions.is_sensitive(act, []) is False


def test_steps_record_the_rail(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="wait", why="settle"))
    run = runs.create_run(USER, "goal", "act", None)
    run, _ = runs.step(run, _step_body(1))
    assert run["steps"][-1]["rail"] == "browser"


# --------------------------------------------------------------------------- #
# Skills — replaying a route instead of working it out again
# --------------------------------------------------------------------------- #

_SKILL_STEPS = [
    {"kind": "navigate", "url": "https://mail.google.com/"},
    {"kind": "click", "expect": "Compose", "role": "button"},
    {"kind": "type", "expect": "To recipients", "text": "a@b.com"},
]


def _with_skill(monkeypatch, steps=None):
    monkeypatch.setattr(
        skills, "find_match",
        lambda goal, uid, start_url=None: {
            "id": "sk1", "name": "Send a Gmail", "steps": steps or _SKILL_STEPS,
            "match_score": 0.8,
        },
    )
    monkeypatch.setattr(skills, "record_use", lambda sid, ok: None)


def test_replay_costs_no_model_calls(monkeypatch):
    """The whole point: a remembered step is returned without asking the model."""
    _with_skill(monkeypatch)
    calls = {"n": 0}

    def counting(run, obs):
        calls["n"] += 1
        return actions.Action(kind="wait", why="thinking")

    monkeypatch.setattr(brain, "decide", counting)

    run = runs.create_run(USER, "send a hello email", "act", None)
    run, first = runs.step(run, _step_body(1))
    run, second = runs.step(run, _step_body(2, last_result={"ok": True}))

    assert first["action"]["kind"] == "navigate"
    assert second["action"]["expect"] == "Compose"
    assert calls["n"] == 0          # never consulted while replaying
    assert first["skill"] == {"name": "Send a Gmail", "status": "replaying"}


def test_replay_hands_back_to_the_model_when_it_runs_out(monkeypatch):
    _with_skill(monkeypatch, steps=[{"kind": "navigate", "url": "https://x.com/"}])
    _stub_brain(monkeypatch, actions.Action(kind="done", summary="finished", why="all good"))

    run = runs.create_run(USER, "send a hello email", "act", None)
    run, _ = runs.step(run, _step_body(1))                          # the one saved step
    run, resp = runs.step(run, _step_body(2, last_result={"ok": True}))
    assert resp["action"]["kind"] == "done"                          # model took over
    assert run["skill"]["status"] == "finished"


def test_a_remembered_step_that_fails_abandons_the_whole_recording(monkeypatch):
    """A route that no longer fits is worse than no route — stop trusting it."""
    _with_skill(monkeypatch)
    _stub_brain(monkeypatch, actions.Action(kind="scroll", why="look properly"))

    run = runs.create_run(USER, "send a hello email", "act", None)
    run, _ = runs.step(run, _step_body(1))
    run, resp = runs.step(
        run, _step_body(2, last_result={"ok": False, "error": 'nothing called "Compose"'})
    )
    assert run["skill"]["status"] == "abandoned"
    assert "Compose" in run["skill"]["abandoned_because"]
    assert resp["action"]["kind"] == "scroll"                        # thinking again


def test_monitor_mode_never_replays(monkeypatch):
    _with_skill(monkeypatch)
    _stub_brain(monkeypatch, actions.Action(kind="extract", why="read it"))
    run = runs.create_run(USER, "watch my email", "monitor", None)
    assert run["skill"] is None


def test_a_broken_skill_store_does_not_block_a_run(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(skills, "find_match", boom)
    _stub_brain(monkeypatch, actions.Action(kind="wait", why="settle"))
    run = runs.create_run(USER, "do a thing", "act", None)
    assert run["skill"] is None and run["status"] == "running"


# --------------------------------------------------------------------------- #
# Takeover — ask the person rather than give up
# --------------------------------------------------------------------------- #

def test_being_stuck_asks_the_user_to_step_in(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="click", index=0, expect="To", why="focus"))
    run = runs.create_run(USER, "send an email", "act", None)
    resp = None
    for seq in range(1, runs._STUCK_AFTER + 1):
        run, resp = runs.step(run, _page(seq, last_result={"ok": True}))
    assert resp["action"]["kind"] == "ask_user"
    assert "carry on" in resp["action"]["text"]
    assert run["status"] == "awaiting_user"


def test_a_canvas_page_fails_instead_of_asking(monkeypatch):
    """No amount of human clicking makes canvas cells readable to the agent."""
    _stub_brain(monkeypatch, actions.Action(kind="key", text="Escape", why="close overlays"))
    run = runs.create_run(USER, "type into the sheet", "act", None)
    resp = None
    for seq in range(1, runs._STUCK_AFTER + 1):
        body = _page(seq, last_result={"ok": True})
        body["dom"]["canvas_app"] = True
        run, resp = runs.step(run, body)
    assert resp["action"]["kind"] == "fail"
    assert "canvas" in resp["action"]["reason"]


def test_it_does_not_ask_twice_about_the_same_screen(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="click", index=0, expect="To", why="focus"))
    run = runs.create_run(USER, "send an email", "act", None)
    seq = 1
    for _ in range(runs._STUCK_AFTER):
        run, _ = runs.step(run, _page(seq, last_result={"ok": True}))
        seq += 1
    run["status"] = "running"  # the user replied and nothing changed
    resp = None
    for _ in range(runs._STUCK_AFTER):
        run, resp = runs.step(run, _page(seq, last_result={"ok": True}))
        seq += 1
        if resp["done"]:
            break
    assert resp["action"]["kind"] == "fail"


def test_extracted_result_becomes_a_finding(monkeypatch):
    _stub_brain(monkeypatch, actions.Action(kind="scroll", why="more"))
    run = runs.create_run(USER, "research", "act", None)
    run, _ = runs.step(run, _step_body(1))
    run, _ = runs.step(run, _step_body(2, last_result={"ok": True, "extracted": "a headline"}))
    assert "a headline" in run["findings"]
