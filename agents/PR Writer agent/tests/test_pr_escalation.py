"""escalation.py — the keyword floor over the eval set, and the screen's
failure semantics: a classifier that cannot run blocks drafting, it never
defaults to "safe"."""
from __future__ import annotations

import pytest

from pr_writer_testkit import load_briefs
from pr_writer_agent import escalation as esc
from pr_writer_agent import llm as pr_llm

BRIEFS = load_briefs()
MUST_ESCALATE = [b for b in BRIEFS if "escalation" in b["tags"]]
MUST_NOT = [b for b in BRIEFS if not b["expect"]["escalate"]]


def _intake(brief: dict) -> dict:
    fields = {"news": brief["sentence"], **brief["stated"]}
    return {k: {"value": v, "provenance": "stated", "excerpt": ""} for k, v in fields.items()}


def _text(brief: dict) -> str:
    return esc.screen_text(_intake(brief), brief.get("pasted", ""))


@pytest.mark.parametrize("brief", MUST_ESCALATE, ids=lambda b: b["id"])
def test_every_escalation_brief_is_caught_by_the_prefilter(brief):
    pf = esc.prefilter(_text(brief), brand_names=["Legal Soft"])
    hit_cats = {c for c, _ in pf.strong + pf.weak}
    assert set(brief["expect"]["categories"]) <= hit_cats, (pf.strong, pf.weak)
    if "classifier_only" not in brief["tags"]:
        assert {c for c, _ in pf.strong} & set(brief["expect"]["categories"]), pf


@pytest.mark.parametrize("brief", MUST_NOT, ids=lambda b: b["id"])
def test_no_strong_hit_on_ordinary_stories(brief):
    """Precision of the floor: a strong hit escalates without a model, so it
    must not fire on everyday legal-services vocabulary."""
    pf = esc.prefilter(_text(brief), brand_names=["Legal Soft"])
    assert pf.strong == [], pf.strong


@pytest.mark.parametrize("text", [
    "Our assistants help practices prevent data breaches when handling records.",
    "The program is designed to reduce the risk of a data breach.",
    "Intake specialists help firms acquire new clients faster.",
    "Legal Soft provides litigation support staff and demand writers for lawsuits.",
])
def test_product_context_is_not_a_strong_hit(text):
    assert esc.prefilter(text, brand_names=["Legal Soft"]).strong == []


@pytest.mark.parametrize("text,cat", [
    ("Legal Soft was sued by a former vendor.", "litigation"),
    ("A class action against Legal Soft was filed.", "litigation"),
    ("We had a data breach last week.", "data_incident"),
    ("The company laid off staff.", "layoffs"),
    ("Legal Soft acquired by a private equity firm.", "m_and_a"),
    ("The state bar opened an inquiry into our ads.", "regulatory"),
    ("The CFO resigned amid an audit.", "crisis"),
])
def test_event_language_is_a_strong_hit(text, cat):
    assert cat in {c for c, _ in esc.prefilter(text, brand_names=["Legal Soft"]).strong}


# --------------------------------------------------------------------------- #
# screen()
# --------------------------------------------------------------------------- #

def test_strong_hit_escalates_without_a_model_call(fake):
    brief = next(b for b in BRIEFS if b["id"] == "e01-litigation")
    invoke = fake()  # any call would raise "unexpected extra model call"
    d = esc.screen(_intake(brief), brand_names=["Legal Soft"], invoke=invoke)
    assert d.escalate and d.decided_by == "keyword" and "litigation" in d.categories
    assert invoke.calls == []


def test_classifier_runs_on_fast_slot_and_its_verdict_stands(fake):
    brief = next(b for b in BRIEFS if b["id"] == "e03-layoffs-soft-wording")
    invoke = fake({"escalate": True, "categories": ["layoffs"], "reason": "roles affected", "injection_notes": []})
    d = esc.screen(_intake(brief), brand_names=["Legal Soft"], invoke=invoke)
    assert d.escalate and d.categories == ["layoffs"] and d.decided_by == "classifier"
    assert invoke.calls[0]["slot"] == "fast"
    assert "restructur" in invoke.calls[0]["user"]  # the weak hit is passed as a hint


def test_classifier_no_escalation(fake):
    brief = next(b for b in BRIEFS if b["id"] == "b02-media-advisory")
    invoke = fake({"escalate": False, "categories": [], "reason": "routine availability", "injection_notes": []})
    d = esc.screen(_intake(brief), brand_names=["Legal Soft"], invoke=invoke)
    assert d.escalate is False


@pytest.mark.parametrize("failure", [
    pr_llm.LLMUnavailable("model call failed (fast slot): timeout"),
    RuntimeError("provider exploded"),
])
def test_classifier_failure_raises_and_never_defaults_to_safe(fake, failure):
    brief = next(b for b in BRIEFS if b["id"] == "b02-media-advisory")
    with pytest.raises(esc.EscalationCheckFailed) as exc:
        esc.screen(_intake(brief), brand_names=["Legal Soft"], invoke=fake(failure))
    assert "drafting is blocked" in str(exc.value)


def test_invalid_classifier_json_after_repair_raises(fake):
    brief = next(b for b in BRIEFS if b["id"] == "b02-media-advisory")
    with pytest.raises(esc.EscalationCheckFailed):
        esc.screen(_intake(brief), invoke=fake("not json", '{"escalate": "maybe"}'))


@pytest.mark.parametrize("payload", [
    {"escalate": True, "categories": [], "reason": "x", "injection_notes": []},
    {"escalate": False, "categories": ["crisis"], "reason": "x", "injection_notes": []},
])
def test_self_contradictory_verdict_raises(fake, payload):
    brief = next(b for b in BRIEFS if b["id"] == "b02-media-advisory")
    with pytest.raises(esc.EscalationCheckFailed):
        esc.screen(_intake(brief), invoke=fake(payload))


def test_injection_in_pasted_text_cannot_produce_a_safe_verdict(fake):
    brief = next(b for b in BRIEFS if b["id"] == "m04-milestone-pasted-only")
    invoke = fake({"escalate": False, "categories": [], "reason": "anniversary", "injection_notes": []})
    d = esc.screen(_intake(brief), pasted=brief["pasted"], invoke=invoke)
    assert d.escalate and d.decided_by == "injection_guard"
    assert '<untrusted kind="pasted"' in invoke.calls[0]["user"]


def test_keyword_floor_reads_the_requesters_words_not_the_models_paraphrase():
    """Live eval 2026-10-28 (b10): pasted "reduce the risk of a data breach"
    is product language and is demoted, but the intake model's note about it
    ("The data breach risk-reduction claim ...") escalated the run on words
    the requester never wrote. Extracted items are screened by excerpt."""
    brief = next(b for b in BRIEFS if b["id"] == "b10-healthcare-retrieval-release")
    intake = _intake(brief)
    intake["sensitivities"] = {"value": [{
        "note": "The data breach risk-reduction claim is a security claim that needs substantiation.",
        "provenance": "extracted", "excerpt": "reduce the risk of a data breach"}],
        "provenance": "extracted", "excerpt": ""}
    intake["cta"] = {"value": "Data breach notice", "provenance": "extracted", "excerpt": "when handling records"}
    text = esc.screen_text(intake, brief["pasted"])
    assert "risk-reduction" not in text
    assert esc.prefilter(text, brand_names=["Legal Soft"], pasted=brief["pasted"]).strong == []


def test_requester_typed_values_are_still_screened_in_full():
    intake = {"news": {"value": "Legal Soft announces new offices.", "provenance": "stated", "excerpt": ""},
              "key_facts": {"value": [{"fact": "We laid off 40 staff in March", "source": "HR",
                                       "provenance": "stated", "excerpt": ""}],
                            "provenance": "stated", "excerpt": ""}}
    pf = esc.prefilter(esc.screen_text(intake), brand_names=["Legal Soft"])
    assert "layoffs" in {c for c, _ in pf.strong}


def test_screen_leaves_room_for_a_reasoning_model():
    """A verdict is ~100 tokens; a reasoning model spends its thinking from
    the same max_tokens, and a cut-off screen blocks the run."""
    assert esc.SCREEN_STEP.max_tokens >= 1500
    assert "not injections" in esc.SCREEN_STEP.task
