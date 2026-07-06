"""§9.2 — each whitelisted token changes ONLY its own occurrences (isolation)."""

from graphics_designer_agent.prompts import load_prompt
from graphics_designer_agent.stage2_element import STAGE2_BLEND_PROMPT, substitute_stage2
from graphics_designer_agent.stage3_text.prompting import (
    DEFAULT_CTA,
    DEFAULT_HEADLINE,
    DEFAULT_HIGHLIGHT,
    DEFAULT_SUBTEXT_1,
    DEFAULT_SUBTEXT_2,
    STAGE3_STYLE_ANCHORS,
    substitute_stage3,
)
from graphics_designer_agent.tokens import default_element_styles

S3 = load_prompt("stage3_text_overlay.txt")
S2 = load_prompt(STAGE2_BLEND_PROMPT)


def test_per_element_style_substitution_and_isolation():
    styles = {
        "headline": {"font": "Causten ExtraBold", "color": "solid white #FFFFFF",
                     "placement": "the RIGHT side"},
        "cta": {"font": "Causten Black", "placement": "centered below"},
    }
    out = substitute_stage3(S3, styles=styles)
    # The targeted element markers are gone…
    for marker in ("[HEADLINE_FONT]", "[HEADLINE_COLOR]", "[HEADLINE_PLACEMENT]",
                   "[CTA_FONT]", "[CTA_PLACEMENT]"):
        assert marker not in out.text
    # …while untouched elements keep their markers (isolation).
    assert "[SUBTEXT1_FONT]" in out.text and "[HIGHLIGHT_COLOR]" in out.text
    expected = (
        S3.replace("[HEADLINE_FONT]", "Causten ExtraBold")
        .replace("[HEADLINE_COLOR]", "solid white #FFFFFF")
        .replace("[HEADLINE_PLACEMENT]", "the RIGHT side")
        .replace("[CTA_FONT]", "Causten Black")
        .replace("[CTA_PLACEMENT]", "centered below")
    )
    assert out.text == expected


def test_highlight_has_no_placement_marker():
    # The highlight is inline in the headline — it carries font + colour only.
    assert "placement" not in STAGE3_STYLE_ANCHORS["highlight"]
    assert "[HIGHLIGHT_PLACEMENT]" not in S3


def test_no_box_instruction_present():
    # The "AI keeps drawing a box behind the text" fix lives in the prompt.
    assert "NO BOX OR PANEL BEHIND THE TEXT" in S3


def test_default_element_styles_cover_every_styleable_element():
    styles = default_element_styles()
    assert set(styles) == set(STAGE3_STYLE_ANCHORS)
    # Highlight defaults to the locked brand gradient; CTA carries no colour.
    assert styles["highlight"]["color"] == "gradient"
    assert "color" not in styles["cta"]


def test_stage3_prompt_preserves_underlying_image():
    # The regeneration/AR-drift fix lives in the prompt itself.
    assert "PRESERVE THE PROVIDED IMAGE" in S3
    assert "change its aspect ratio" in S3


def test_headline_isolation():
    new = "Hire Top Legal Talent Fast"
    out = substitute_stage3(S3, headline=new)
    assert out.text == S3.replace(DEFAULT_HEADLINE, new)


def test_highlight_isolation_hits_all_occurrences():
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
