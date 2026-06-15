"""§9.1 — canonical prompts are byte-frozen and default substitution is a no-op."""

from graphics_designer_agent.prompts import load_prompt, verify_integrity
from graphics_designer_agent.tokens import substitute_stage2, substitute_stage3
from graphics_designer_agent.variants import STAGE2_BLEND_PROMPT


def test_prompt_integrity_matches_frozen_hashes():
    assert verify_integrity() == []


def test_stage2_blend_prompt_untouched_without_subject_or_ar():
    # The common blend prompt carries no AR tokens; with no subject + default AR
    # it must be byte-identical (§9.1) and produce no diffs.
    tmpl = load_prompt(STAGE2_BLEND_PROMPT)
    out = substitute_stage2(tmpl, None, "4:5")
    assert out.text == tmpl
    assert out.diffs == []


def test_stage3_defaults_are_byte_identical():
    tmpl = load_prompt("stage3_text_overlay.txt")
    out = substitute_stage3(tmpl)
    assert out.text == tmpl
    assert out.diffs == []
