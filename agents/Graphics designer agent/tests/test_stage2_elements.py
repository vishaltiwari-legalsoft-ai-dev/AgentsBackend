"""Expanded Stage-2 element library + the agent element explorer (§7.1.1b)."""

from graphics_designer_agent import suggestions
from graphics_designer_agent.prompts import CANONICAL_SHA256, load_prompt
from graphics_designer_agent.tokens import substitute_stage2
from graphics_designer_agent.variants import (
    STAGE2_BLEND_PROMPT,
    STAGE2_CATEGORIES,
    STAGE2_VARIANTS,
)

_IDS = {v["id"] for v in STAGE2_VARIANTS}


def test_catalog_is_well_formed():
    # Grew well past the original five; one shared immutable blend prompt.
    assert len(STAGE2_VARIANTS) >= 19
    assert STAGE2_BLEND_PROMPT in CANONICAL_SHA256
    for v in STAGE2_VARIANTS:
        assert v["category"] in STAGE2_CATEGORIES, v
        assert v["subject"].strip(), v["id"]
        assert "prompt_file" not in v  # no per-variant prompt files anymore


def test_subjects_carry_no_background_or_palette():
    # The whole point: subjects describe only the element, never the background.
    for v in STAGE2_VARIANTS:
        s = v["subject"]
        assert "#" not in s, f"{v['id']} leaked a colour code"
        assert "gradient" not in s.lower(), f"{v['id']} describes a gradient/background"


def test_blend_prompt_merges_background_and_subject():
    blend = load_prompt(STAGE2_BLEND_PROMPT)
    assert "[SUBJECT]" in blend
    # Built prompt drops the token and instructs preserving the provided background.
    out = substitute_stage2(blend, "A", "4:5", subject=STAGE2_VARIANTS[0]["subject"])
    assert "[SUBJECT]" not in out.text
    assert "background" in out.text.lower()
    assert STAGE2_VARIANTS[0]["subject"] in out.text


def test_recommend_concept_covers_full_catalog():
    rec = suggestions.recommend_concept({"angle": "pain"})
    assert rec["recommended"] in _IDS
    assert {v["id"] for v in rec["variants"]} == _IDS


def test_explore_elements_returns_valid_catalog_ids():
    e = suggestions.explore_elements({"goal": "brand"})
    assert e["type"] == "explore"
    assert e["ai"] is False  # no LLM key in the test env → curated
    ids = [p["id"] for p in e["picks"]]
    assert 1 <= len(ids) == len(set(ids)) <= 3
    assert all(i in _IDS for i in ids)
    assert e["wildcard"]["id"] in _IDS


def test_explore_respects_exclude():
    e = suggestions.explore_elements({}, exclude=["G", "H"])
    chosen = {p["id"] for p in e["picks"]} | {e["wildcard"]["id"]}
    assert chosen.isdisjoint({"G", "H"})
