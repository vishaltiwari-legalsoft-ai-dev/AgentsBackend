"""llm.py — one call, local repair, at most one model repair, then an honest
raise. Never a canned payload."""
from __future__ import annotations

import pytest

from pr_writer_agent import llm as pr_llm
from pr_writer_agent.prompts import STEPS
from seo_geo_agent.sources import CredentialMissing

STEP = STEPS["angle"]
GOOD = {"why_news": "a", "why_now": "b", "who_cares": "c", "angle_paragraph": "d",
        "audience": "legal_buyers", "newsworthiness": "strong", "weak_news_reason": None,
        "primary_keyword": "virtual paralegal", "secondary_keywords": [], "injection_notes": []}


def test_valid_json_is_one_call(fake):
    invoke = fake(GOOD)
    result = pr_llm.call_json(STEP, "sys", "user", invoke=invoke)
    assert result.payload == GOOD and len(invoke.calls) == 1
    assert invoke.calls[0]["slot"] == "main" and invoke.calls[0]["timeout_s"] == STEP.timeout_s
    assert result.usage["calls"] == 1


def test_code_fenced_json_is_repaired_locally_without_a_second_call(fake):
    import json
    invoke = fake("```json\n" + json.dumps(GOOD) + "\n```")
    assert pr_llm.call_json(STEP, "s", "u", invoke=invoke).payload == GOOD
    assert len(invoke.calls) == 1


def test_schema_invalid_gets_exactly_one_repair_call_on_the_fast_slot(fake):
    bad = dict(GOOD, audience="everyone")
    invoke = fake(bad, GOOD)
    result = pr_llm.call_json(STEP, "s", "u", invoke=invoke)
    assert result.payload == GOOD and len(invoke.calls) == 2
    repair = invoke.calls[1]
    assert repair["slot"] == "fast" and repair["timeout_s"] == pr_llm.REPAIR_TIMEOUT_S
    assert "'everyone' is not one of" in repair["user"]


def test_still_invalid_after_repair_raises_with_the_reason(fake):
    with pytest.raises(pr_llm.LLMOutputInvalid) as exc:
        pr_llm.call_json(STEP, "s", "u", invoke=fake("nope", "still nope"))
    assert "angle" in str(exc.value) and "after one repair" in str(exc.value)


def test_truncated_output_is_not_retried(fake):
    invoke = fake(pr_llm.Reply(text='{"why_news": "a', finish_reason="length"))
    with pytest.raises(pr_llm.LLMTruncated):
        pr_llm.call_json(STEP, "s", "u", invoke=invoke)
    assert len(invoke.calls) == 1


def test_refusal_is_reported_as_refusal(fake):
    with pytest.raises(pr_llm.LLMRefused):
        pr_llm.call_json(STEP, "s", "u", invoke=fake(pr_llm.Reply(text="", finish_reason="content_filter")))


def test_offline_default_invoke_fails_honestly_before_any_network():
    """The repo-root conftest blanks the OpenRouter key; the real adapter must
    turn that into LLMUnavailable (a CredentialMissing, so the router's 424
    path applies) — not a fabricated payload."""
    with pytest.raises(pr_llm.LLMUnavailable) as exc:
        pr_llm.default_invoke("s", "u", slot="fast", timeout_s=5, max_tokens=10, temperature=0)
    assert isinstance(exc.value, CredentialMissing)
    assert "openrouter_api_key" in str(exc.value).lower()


@pytest.mark.parametrize("value,errors", [
    ({"a": "x"}, 0),
    ({}, 1),                       # missing required
    ({"a": "x", "b": 1}, 1),       # extra key
    ({"a": 3}, 1),                 # wrong type
])
def test_validator(value, errors):
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"],
              "additionalProperties": False}
    assert len(pr_llm.validate(value, schema)) == errors


# --------------------------------------------------------------------------- #
# Provider cost: OpenRouter's usage.cost, carried end to end, never guessed
# --------------------------------------------------------------------------- #

def _msg(cost="absent", text='{"a": "x"}'):
    from langchain_core.messages import AIMessage

    usage = {"prompt_tokens": 120, "completion_tokens": 30}
    if cost != "absent":
        usage["cost"] = cost
    return AIMessage(content=text, response_metadata={
        "finish_reason": "stop", "model_name": "anthropic/claude-sonnet-5.5", "token_usage": usage},
        usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150})


def test_default_invoke_asks_for_usage_accounting_and_reads_the_cost(monkeypatch):
    seen = {}

    class _LLM:
        model_name = "anthropic/claude-sonnet-5.5"

        def invoke(self, messages, **kw):
            seen.update(kw)
            return _msg(0.004321)

    import app.services.openrouter as openrouter
    monkeypatch.setattr(openrouter, "get_llm", lambda *a, **k: _LLM())
    reply = pr_llm.default_invoke("s", "u", slot="fast", timeout_s=5, max_tokens=10, temperature=0)
    assert seen["extra_body"] == {"usage": {"include": True}}
    assert reply.cost_usd == pytest.approx(0.004321)
    assert (reply.input_tokens, reply.output_tokens) == (120, 30)
    assert reply.model == "anthropic/claude-sonnet-5.5"


@pytest.mark.parametrize("cost,expected", [("absent", None), (None, None), (True, None), ("0.01", None),
                                           (0, 0.0), (0.0125, 0.0125)])
def test_reply_cost_is_only_a_number_the_provider_reported(cost, expected):
    assert pr_llm.reply_from_message(_msg(cost)).cost_usd == expected


def test_result_cost_sums_every_call_including_the_repair(fake):
    bad = dict(GOOD, audience="everyone")
    invoke = fake(pr_llm.Reply(text=__import__("json").dumps(bad), finish_reason="stop", cost_usd=0.01),
                  pr_llm.Reply(text=__import__("json").dumps(GOOD), finish_reason="stop", cost_usd=0.002))
    assert pr_llm.call_json(STEP, "s", "u", invoke=invoke).usage["cost_usd"] == pytest.approx(0.012)


def test_result_cost_is_unknown_when_any_call_did_not_report_one(fake):
    bad = dict(GOOD, audience="everyone")
    invoke = fake(pr_llm.Reply(text=__import__("json").dumps(bad), finish_reason="stop", cost_usd=0.01),
                  GOOD)  # the fake's default reply carries no cost
    assert pr_llm.call_json(STEP, "s", "u", invoke=invoke).usage["cost_usd"] is None


def test_meter_reports_provider_cost_and_never_a_partial_sum():
    from pr_writer_agent.pipeline import Meter

    m = Meter()
    assert m.as_dict()["cost_usd"] is None  # no calls, nothing billed to report
    m.add(pr_llm.Reply(text="", cost_usd=0.02, model="a"))
    m.add(pr_llm.Reply(text="", cost_usd=0.005, model="b"))
    assert m.as_dict()["cost_usd"] == pytest.approx(0.025)
    m.add(pr_llm.Reply(text="", model="c"))  # provider did not report this one
    assert m.as_dict()["cost_usd"] is None
