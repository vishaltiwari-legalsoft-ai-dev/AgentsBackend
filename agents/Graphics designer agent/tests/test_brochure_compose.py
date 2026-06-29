# tests/test_brochure_compose.py
from graphics_designer_agent.creative import brochure_layout as bl


def test_new_pages_shape_is_passed_through_with_cover_first():
    plan = {"cover": {"title": "Legal Soft", "subtitle": "Brochure"},
            "pages": [{"template": "card_grid", "heading": "Roles",
                       "cards": [{"title": "Intake", "bullets": ["call"]}]}]}
    pages = bl.compose_brochure(plan)
    assert pages[0]["template"] == "cover"
    assert pages[0]["heading"] == "Legal Soft"
    assert pages[1]["template"] == "card_grid"
    assert all("text_lines" in pg for pg in pages)


def test_legacy_sections_are_inferred_into_templates():
    plan = {"cover": {"title": "X", "subtitle": "Y"},
            "sections": [{"heading": "Our Roles",
                          "bullets": ["Intake", "Paralegal", "Case Manager"]},
                         {"heading": "Why us", "body": "Because we are great."}],
            "contact": {"line": "Call us 555"}}
    pages = bl.compose_brochure(plan)
    templates = [pg["template"] for pg in pages]
    assert templates[0] == "cover"
    assert "cta_contact" in templates           # contact became a CTA page
    assert "feature" in templates or "card_grid" in templates


def test_contact_section_infers_cta_contact():
    assert bl._infer_template({"heading": "Contact us", "body": "email a@b.com"}) == "cta_contact"


def test_quote_section_infers_testimonial():
    assert bl._infer_template({"heading": "What clients say", "quote": "Great"}) == "testimonial"
