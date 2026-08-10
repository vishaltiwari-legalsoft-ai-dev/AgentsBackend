"""The a11 brain: prompt binding + strict parse, with a stubbed LLM."""
from __future__ import annotations

import app  # noqa: F401 - registers agent roots on sys.path
import pytest

from app.services import openrouter as openrouter_service
from browser_agent import brain


class _StubLLM:
    def __init__(self, reply: str):
        self._reply = reply
        self.prompts: list = []

    def invoke(self, messages):
        self.prompts.append(messages)

        class R:
            content = self._reply

        R.content = self._reply
        return R()


def _run():
    return {
        "goal": "Find the top AI story",
        "mode": "act",
        "step_cap": 40,
        "steps_used": 0,
        "steps": [],
        "findings": [],
    }


def _obs():
    return {
        "tab": {"id": 1, "title": "Hacker News", "url": "https://news.ycombinator.com"},
        "tabs": [{"id": 1, "title": "HN", "url": "https://news.ycombinator.com", "active": True}],
        "dom": {"elements": [{"i": 0, "tag": "a", "text": "Top story"}], "truncated": False},
        "last_result": None,
    }


def _bind(monkeypatch, reply: str) -> list:
    seen: list = []

    def fake_get_llm(*_a, **kw):
        seen.append(kw.get("agent_id"))
        return _StubLLM(reply)

    monkeypatch.setattr(openrouter_service, "get_llm", fake_get_llm)
    return seen


def test_decide_passes_agent_id_a11(monkeypatch):
    seen = _bind(monkeypatch, '{"kind":"click","index":0,"why":"open"}')
    action = brain.decide(_run(), _obs())
    assert seen == ["a11"]
    assert action.kind == "click" and action.index == 0


def test_decide_strips_markdown_fence(monkeypatch):
    _bind(monkeypatch, '```json\n{"kind":"done","summary":"ok"}\n```')
    action = brain.decide(_run(), _obs())
    assert action.kind == "done" and action.summary == "ok"


def test_decide_falls_back_to_honest_fail_on_garbage(monkeypatch):
    _bind(monkeypatch, "I think you should click the button.")
    action = brain.decide(_run(), _obs())
    assert action.kind == "fail"
    assert "unparseable" in (action.reason or "")


def test_prompt_includes_goal_and_dom_index(monkeypatch):
    captured: list = []

    def fake_get_llm(*_a, **kw):
        stub = _StubLLM('{"kind":"wait","why":"settle"}')
        captured.append(stub)
        return stub

    monkeypatch.setattr(openrouter_service, "get_llm", fake_get_llm)
    brain.decide(_run(), _obs())
    user_msg = captured[0].prompts[0][1][1]  # ("user", <text>)
    assert "Find the top AI story" in user_msg
    assert "[0]" in user_msg  # the distilled element index is rendered
