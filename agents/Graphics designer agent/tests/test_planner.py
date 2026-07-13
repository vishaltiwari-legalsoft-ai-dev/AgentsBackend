"""Auto-mode planner: strict pack-validated JSON, retry-with-errors, and an
honest PlanError on failure — never a fabricated plan."""
import json
from types import SimpleNamespace

import pytest

from graphics_designer_agent import planner, registry
from graphics_designer_agent.runs import create_run

LOGOS = ["combined-solid", "combined-gradient"]


def _good_plan():
    return json.dumps({
        "concept": "Festive trust-forward offer.",
        "gradient": {"cid": "a", "reason": "Bright, optimistic sweep fits the offer."},
        "element": {"cid": "A", "reason": "People-first fits the audience."},
        "text": {"headline": "Diwali Offer For Your Firm", "highlight": "Diwali",
                 "subline": "20% off contract review for new clients", "cta": "Book Now",
                 "reason": "Direct offer framing."},
        "logo": {"logo_id": "combined-solid", "reason": "Best contrast on light."},
    })


def _fake_llm(replies):
    it = iter(replies)
    return SimpleNamespace(invoke=lambda _q: SimpleNamespace(content=next(it)))


def test_valid_plan_first_try(monkeypatch):
    monkeypatch.setattr(planner, "_get_planner_llm", lambda: _fake_llm([_good_plan()]))
    run = create_run("plan-user")
    plan = planner.build_plan(run, registry.get_pack(None), "Diwali offer 20% off", LOGOS)
    assert plan["version"] == 1
    assert plan["gradient"]["cid"] == "A"      # normalized to upper-case
    assert plan["logo"]["logo_id"] == "combined-solid"
    assert plan["brief"] == "Diwali offer 20% off"


def test_unknown_cid_is_retried_with_errors(monkeypatch):
    bad = _good_plan().replace('"cid": "a"', '"cid": "ZZ"', 1)
    asks: list[str] = []
    llm = SimpleNamespace()
    replies = iter([bad, _good_plan()])
    def invoke(q):
        asks.append(q)
        return SimpleNamespace(content=next(replies))
    llm.invoke = invoke
    monkeypatch.setattr(planner, "_get_planner_llm", lambda: llm)
    plan = planner.build_plan(create_run("plan-user-2"), registry.get_pack(None), "brief text", LOGOS)
    assert plan["gradient"]["cid"] == "A"
    assert "REJECTED" in asks[1] and "ZZ" in asks[1]


def test_persistent_failure_raises_plan_error(monkeypatch):
    monkeypatch.setattr(planner, "_get_planner_llm", lambda: _fake_llm(["nonsense"] * 3))
    with pytest.raises(planner.PlanError):
        planner.build_plan(create_run("plan-user-3"), registry.get_pack(None), "brief text", LOGOS)


def test_empty_brief_rejected():
    with pytest.raises(planner.PlanError):
        planner.build_plan(create_run("plan-user-4"), registry.get_pack(None), "   ", LOGOS)


def test_logo_must_be_null_without_library(monkeypatch):
    monkeypatch.setattr(planner, "_get_planner_llm", lambda: _fake_llm([_good_plan()] * 3))
    with pytest.raises(planner.PlanError):
        planner.build_plan(create_run("plan-user-5"), registry.get_pack(None), "brief text", [])


def test_ask_contains_brief_and_inventory():
    pack = registry.get_pack(None)
    ask = planner._plan_ask(pack, "Diwali offer 20% off", LOGOS)
    assert "Diwali offer 20% off" in ask and "#1 hard rule" in ask
    assert pack.stage1_variants[0]["id"] in ask
    assert "combined-solid" in ask


def test_non_dict_section_is_retried_not_crashed(monkeypatch):
    bad = '{"concept": "x", "gradient": "A", "element": {"cid": "A", "reason": "r"}, "text": {"headline": "h", "cta": "c"}, "logo": {"logo_id": null, "reason": "r"}}'
    asks: list[str] = []
    replies = iter([bad, _good_plan()])
    llm = SimpleNamespace()
    def invoke(q):
        asks.append(q)
        return SimpleNamespace(content=next(replies))
    llm.invoke = invoke
    monkeypatch.setattr(planner, "_get_planner_llm", lambda: llm)
    plan = planner.build_plan(create_run("plan-user-6"), registry.get_pack(None), "brief text", LOGOS)
    assert plan["gradient"]["cid"] == "A"
    assert "must be a JSON object" in asks[1]


def test_persistent_non_dict_sections_raise_plan_error(monkeypatch):
    bad = '{"concept": "x", "gradient": "A", "element": "B", "text": "words", "logo": "L"}'
    monkeypatch.setattr(planner, "_get_planner_llm", lambda: _fake_llm([bad] * 3))
    with pytest.raises(planner.PlanError):
        planner.build_plan(create_run("plan-user-7"), registry.get_pack(None), "brief text", LOGOS)
