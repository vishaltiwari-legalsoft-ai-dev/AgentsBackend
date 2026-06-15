"""§9.2 — each whitelisted token changes ONLY its own occurrences (isolation)."""

from graphics_designer_agent.prompts import load_prompt
from graphics_designer_agent.tokens import (
    DEFAULT_CTA,
    DEFAULT_HEADLINE,
    DEFAULT_HIGHLIGHT,
    DEFAULT_SUBTEXT_1,
    DEFAULT_SUBTEXT_2,
    FONT_ANCHOR,
    substitute_stage2,
    substitute_stage3,
)

from graphics_designer_agent.variants import STAGE2_BLEND_PROMPT

S3 = load_prompt("stage3_text_overlay.txt")
S2 = load_prompt(STAGE2_BLEND_PROMPT)


def test_font_isolation():
    # Font is locked to the Causten family; selecting a variant swaps the anchor.
    out = substitute_stage3(S3, font="Causten ExtraBold")
    assert out.text == S3.replace(FONT_ANCHOR, "Causten ExtraBold")
    assert out.text.replace("Causten ExtraBold", FONT_ANCHOR) == S3  # reversible


def test_font_is_locked_to_causten_family():
    from graphics_designer_agent.tokens import DEFAULT_FONT
    from graphics_designer_agent.variants import FONT_FAMILY, FONTS

    assert FONT_FAMILY == "Causten"
    assert DEFAULT_FONT in FONTS
    assert FONTS and all(f.startswith("Causten") for f in FONTS)
    # With the Causten default selected, the Artica anchor is fully replaced.
    out = substitute_stage3(S3, font=DEFAULT_FONT)
    assert FONT_ANCHOR not in out.text
    assert DEFAULT_FONT in out.text


def test_placement_substitution_and_isolation():
    # Default (no placement args): markers stay → byte-identical, no diffs.
    assert substitute_stage3(S3).text == S3
    # Resolved phrases replace only the placement markers.
    out = substitute_stage3(S3, text_placement="the RIGHT side", cta_placement="centered")
    assert "[TEXT_PLACEMENT]" not in out.text and "[CTA_PLACEMENT]" not in out.text
    assert "the RIGHT side" in out.text and "centered" in out.text
    expected = S3.replace("[TEXT_PLACEMENT]", "the RIGHT side").replace("[CTA_PLACEMENT]", "centered")
    assert out.text == expected


def test_stage3_prompt_preserves_underlying_image():
    # The regeneration/AR-drift fix lives in the prompt itself.
    assert "PRESERVE THE PROVIDED IMAGE" in S3
    assert "change its aspect ratio" in S3


def test_headline_isolation():
    new = "Hire Top Legal Talent Fast"
    out = substitute_stage3(S3, headline=new)
    assert out.text == S3.replace(DEFAULT_HEADLINE, new)


def test_highlight_isolation_hits_all_three_occurrences():
    out = substitute_stage3(S3, highlight="Legal Staff")
    assert out.text == S3.replace(DEFAULT_HIGHLIGHT, "Legal Staff")


def test_subtext_and_cta_isolation():
    assert substitute_stage3(S3, subtext1="A short value prop here.").text == \
        S3.replace(DEFAULT_SUBTEXT_1, "A short value prop here.")
    assert substitute_stage3(S3, subtext2="Another crisp value prop.").text == \
        S3.replace(DEFAULT_SUBTEXT_2, "Another crisp value prop.")
    assert substitute_stage3(S3, cta="Start Now").text == S3.replace(DEFAULT_CTA, "Start Now")


def test_stage2_subject_substitution():
    out = substitute_stage2(S2, "A", "4:5", subject="A lone red balloon")
    assert "[SUBJECT]" not in out.text
    assert "A lone red balloon" in out.text
    assert out.text == S2.replace("[SUBJECT]", "A lone red balloon")


def test_stage2_blend_has_no_ar_tokens_and_warns_on_non_default():
    out = substitute_stage2(S2, "A", "9:16", subject="A subject")
    # No AR text tokens — only the subject changed; dimensions go via the API.
    assert out.text == S2.replace("[SUBJECT]", "A subject")
    assert out.warnings  # warns that AR must be passed via API only
