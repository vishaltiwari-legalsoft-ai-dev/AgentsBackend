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
