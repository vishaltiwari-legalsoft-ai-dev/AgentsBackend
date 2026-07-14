"""Text Optimizer polish prompts — 3 style recipes sharing one preservation block."""

from graphics_designer_agent.stage3_text import polish_prompts as pp


def _layers():
    return [
        {"type": "text", "id": "headline", "text": "Hire Virtual Legal Staff",
         "x": 0.06, "y": 0.2, "anchor": "ml"},
        {"type": "text", "id": "subheading-0", "text": "Vetted paralegals", "x": 0.06, "y": 0.5},
        {"type": "cta", "id": "cta", "text": "Book a Call", "x": 0.5, "y": 0.94},
        {"type": "shape", "id": "s1", "kind": "rect", "x": 0.9, "y": 0.9},
        {"type": "text", "id": "venue", "text": "", "x": 0.06, "y": 0.965},
    ]


def test_three_recipes_with_expected_keys():
    assert pp.STYLE_KEYS == ["brand_strict", "highlighted", "sharp_minimal"]
    assert all(r["label"] and r["intent"] for r in pp.STYLE_RECIPES)


def test_describe_layout_names_zones_and_skips_empty():
    desc = pp.describe_layout(_layers())
    assert 'HEADLINE "Hire Virtual Legal Staff"' in desc
    assert "left" in desc and "CTA BUTTON" in desc and "RECT" in desc
    assert "VENUE" not in desc  # empty text layers are skipped


def test_prompt_contains_all_blocks():
    for key in pp.STYLE_KEYS:
        p = pp.build_polish_prompt(key, "HEADLINE — top left", notes="keep it airy")
        assert pp.PRESERVATION_BLOCK in p
        assert "HEADLINE — top left" in p
        assert "keep it airy" in p


def test_prompt_omits_notes_block_when_empty():
    p = pp.build_polish_prompt("brand_strict", "X")
    assert "DESIGNER NOTES" not in p


def test_preservation_forbids_added_text():
    # Live-run regression: the polish model hallucinated a vertical "NO GLYPH"
    # tag inside the CTA — adding NEW text must be explicitly forbidden.
    assert "Never ADD new text" in pp.PRESERVATION_BLOCK
    assert "watermark" in pp.PRESERVATION_BLOCK
