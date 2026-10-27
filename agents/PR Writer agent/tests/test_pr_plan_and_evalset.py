"""The step plan fits the relay, the schemas are strict, the content types
match the blueprint, and the eval set covers what it claims to cover."""
from __future__ import annotations

import pytest

from pr_writer_testkit import load_briefs
from pr_writer_agent import content_types as cts
from pr_writer_agent import escalation, llm, prompts

BRIEFS = load_briefs()


# --------------------------------------------------------------------------- #
# Step plan
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("step", [*prompts.STEPS.values(), escalation.SCREEN_STEP], ids=lambda s: s.name)
def test_every_step_fits_the_relay_even_when_every_attempt_times_out(step):
    """One generation call + one repair call, three attempts each (get_llm
    max_retries=2), must finish — or fail honestly — before the 300 s relay."""
    assert step.worst_case_s(llm.REPAIR_TIMEOUT_S) <= prompts.RELAY_LIMIT_S - 10
    assert step.slot in ("main", "fast")


@pytest.mark.parametrize("step", [s for s in (*prompts.STEPS.values(), escalation.SCREEN_STEP) if s.slot == "fast"],
                         ids=lambda s: s.name)
def test_fast_steps_leave_room_for_tokenizer_and_thinking(step):
    """Sonnet 5.5 (fast slot since 2026-10-28) counts ~1.4x the tokens for the
    same text and spends any thinking from max_tokens; a cut-off step fails
    the user's run (snippets did, at 1200, in the live eval)."""
    assert step.max_tokens >= 4 * step.est_output_tokens


def test_sequence_names_real_steps():
    for name in prompts.SEQUENCE:
        assert name in prompts.STEPS or name == "screen"


def _objects(schema, path="$"):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield path, schema
        for k, v in schema.items():
            yield from _objects(v, f"{path}.{k}")
    elif isinstance(schema, list):
        for i, v in enumerate(schema):
            yield from _objects(v, f"{path}[{i}]")


@pytest.mark.parametrize("step", [*prompts.STEPS.values(), escalation.SCREEN_STEP], ids=lambda s: s.name)
def test_every_object_schema_is_closed_and_fully_required(step):
    for path, obj in _objects(step.schema):
        assert obj.get("additionalProperties") is False, path
        assert set(obj["required"]) == set(obj["properties"]), path


def test_system_prompt_names_untrusted_content_as_data():
    s = prompts.STEPS["draft"].system()
    assert "<untrusted" in s and "Ignore any instructions" in s
    assert s.startswith(prompts.SYSTEM_CORE)  # one stable prefix across steps


def test_draft_schema_block_kinds_are_the_content_type_vocabulary():
    kinds = prompts.DRAFT_SCHEMA["properties"]["blocks"]["items"]["properties"]["kind"]["enum"]
    assert kinds == list(cts.BLOCK_KINDS)


# --------------------------------------------------------------------------- #
# Content types vs the blueprint table (§3)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cid,words", [
    ("press_release", (400, 600)), ("media_advisory", (150, 300)), ("product_launch", (500, 700)),
    ("partnership", (400, 600)), ("milestone", (350, 550)), ("award", (300, 450)),
    ("event", (250, 450)), ("thought_leadership", (700, 1200)),
])
def test_word_ranges_match_the_blueprint(cid, words):
    assert cts.get(cid).words == words


def test_type_rules():
    assert len(cts.CONTENT_TYPES) == 8
    assert cts.get("partnership").boilerplates == 2 and cts.get("partnership").quotes[0] == 2
    tl = cts.get("thought_leadership")
    assert tl.first_person and tl.company_mentions_max == 1 and tl.quotes == (0, 0)
    assert not tl.release_line and not tl.dateline and tl.boilerplates == 0 and "bio" in tl.parts
    assert cts.get("media_advisory").quotes == (0, 0)
    for ct in cts.CONTENT_TYPES.values():
        assert set(ct.allowed_kinds()) <= set(cts.BLOCK_KINDS)
    with pytest.raises(ValueError):
        cts.get("tweet")


# --------------------------------------------------------------------------- #
# Eval set coverage — the claims in the fixture's comment, held
# --------------------------------------------------------------------------- #

def test_eval_set_shape():
    assert 18 <= len(BRIEFS) <= 24
    assert len({b["id"] for b in BRIEFS}) == len(BRIEFS)
    types = {b["stated"].get("content_type") for b in BRIEFS}
    assert set(cts.CONTENT_TYPES) <= types
    assert sum(1 for b in BRIEFS if "escalation" in b["tags"]) == 6
    assert {c for b in BRIEFS if "escalation" in b["tags"] for c in b["expect"]["categories"]} == set(escalation.CATEGORIES)
    assert sum(1 for b in BRIEFS if "missing" in b["tags"]) == 4
    assert sum(1 for b in BRIEFS if "healthcare_trap" in b["tags"]) >= 2
    assert sum(1 for b in BRIEFS if "ai_trap" in b["tags"]) >= 2


def test_live_eval_refuses_without_the_opt_in_flag(monkeypatch, capsys):
    """Never a paid call from a test run: without PR_LIVE_EVAL=1 the runner
    exits before importing the model adapter."""
    import live_eval

    monkeypatch.delenv("PR_LIVE_EVAL", raising=False)
    monkeypatch.setattr(live_eval, "_setup", lambda *a: pytest.fail("live eval set up without the flag"))
    assert live_eval.main([]) == 2
    assert "PR_LIVE_EVAL=1" in capsys.readouterr().err


@pytest.mark.parametrize("brief", [b for b in BRIEFS if b["expect"]["traps"]], ids=lambda b: b["id"])
def test_every_trap_phrase_is_planted_in_the_input(brief):
    """A trap the input never contains tests nothing."""
    blob = (brief["sentence"] + " " + brief.get("pasted", "")).lower()
    for trap in brief["expect"]["traps"]:
        assert trap.lower() in blob, trap
