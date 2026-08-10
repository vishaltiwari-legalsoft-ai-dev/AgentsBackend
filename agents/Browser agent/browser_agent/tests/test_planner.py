"""Planning: decompose a task before touching the browser, and never die trying."""
from __future__ import annotations

import app  # noqa: F401 - registers agent roots on sys.path
import pytest

from app.services import openrouter as openrouter_service
from browser_agent import planner

GOOD = """{"subtasks": [
  {"title": "Open compose", "goal": "Open a new compose window in Gmail",
   "steps": ["Click the Compose button"],
   "edge_cases": [{"risk": "the compose opens as a popout", "handle": "work inside the popout"}],
   "done_when": "an empty compose window is visible"},
  {"title": "Fill the recipient", "goal": "Put the address in the To field",
   "steps": ["Click the To field", "Type the address"],
   "edge_cases": [{"risk": "autocomplete swallows Enter", "handle": "press Escape first"}],
   "done_when": "the To field shows the address"}
], "notes": "Gmail is a single-page app"}"""


class _StubLLM:
    def __init__(self, reply):
        self._reply = reply
        self.seen: list = []

    def invoke(self, messages):
        self.seen.append(messages)
        if isinstance(self._reply, Exception):
            raise self._reply

        class R:
            content = self._reply

        R.content = self._reply
        return R()


def _bind(monkeypatch, reply) -> list[dict]:
    calls: list[dict] = []

    def fake_get_llm(*_a, **kw):
        calls.append(kw)
        return _StubLLM(reply)

    monkeypatch.setattr(openrouter_service, "get_llm", fake_get_llm)
    return calls


def test_plan_has_subtasks_steps_and_edge_cases(monkeypatch):
    _bind(monkeypatch, GOOD)
    plan = planner.build_plan("send an email in gmail")
    assert plan["planned"] is True
    assert [s["id"] for s in plan["subtasks"]] == ["s1", "s2"]
    assert plan["subtasks"][0]["edge_cases"][0]["handle"] == "work inside the popout"
    assert plan["subtasks"][1]["done_when"].startswith("the To field")
    assert plan["notes"]


def test_planner_uses_the_premium_planner_model(monkeypatch):
    calls = _bind(monkeypatch, GOOD)
    monkeypatch.setattr(
        planner.runtime_config, "get_for_agent",
        lambda agent_id, field: f"{agent_id}:{field}",
    )
    planner.build_plan("do a thing")
    assert calls[0]["model"] == "a11:browser_planner_model"
    assert calls[0]["agent_id"] == "a11"


def test_unparseable_plan_degrades_instead_of_killing_the_run(monkeypatch):
    _bind(monkeypatch, "Sure! First you should open Gmail.")
    plan = planner.build_plan("send an email")
    assert plan["planned"] is False
    assert plan["subtasks"][0]["goal"] == "send an email"
    assert plan["plan_error"]


def test_llm_failure_degrades_too(monkeypatch):
    _bind(monkeypatch, RuntimeError("no credit"))
    plan = planner.build_plan("send an email")
    assert plan["planned"] is False
    assert "no credit" in plan["plan_error"]


def test_empty_subtasks_is_treated_as_no_plan(monkeypatch):
    _bind(monkeypatch, '{"subtasks": [], "notes": ""}')
    assert planner.build_plan("x")["planned"] is False


def test_plan_is_capped(monkeypatch):
    many = ",".join(
        f'{{"title":"t{i}","goal":"g{i}","steps":[],"edge_cases":[],"done_when":"d"}}'
        for i in range(planner.MAX_SUBTASKS + 4)
    )
    _bind(monkeypatch, f'{{"subtasks":[{many}]}}')
    assert len(planner.build_plan("x")["subtasks"]) == planner.MAX_SUBTASKS


def test_markdown_fenced_plan_is_accepted(monkeypatch):
    _bind(monkeypatch, f"```json\n{GOOD}\n```")
    assert planner.build_plan("x")["planned"] is True


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #

@pytest.fixture()
def plan(monkeypatch):
    _bind(monkeypatch, GOOD)
    return planner.build_plan("send an email")


def test_current_starts_at_the_first_subtask(plan):
    assert planner.current(plan)["title"] == "Open compose"
    assert planner.progress(plan) == (0, 2)


def test_advance_walks_the_plan_then_finishes(plan):
    assert planner.advance(plan)["title"] == "Fill the recipient"
    assert planner.progress(plan) == (1, 2)
    assert planner.advance(plan) is None
    assert planner.progress(plan) == (2, 2)
    assert planner.current(plan) is None
