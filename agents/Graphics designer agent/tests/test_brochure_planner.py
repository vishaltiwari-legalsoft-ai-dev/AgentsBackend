# tests/test_brochure_planner.py
from graphics_designer_agent.creative import planner


def test_brochure_plan_emits_pages_with_templates():
    p = planner.plan("brochure", "virtual legal staff for law firms",
                     brand_name="Legal Soft", use_llm=False)
    assert "pages" in p
    templates = [pg["template"] for pg in p["pages"]]
    assert "card_grid" in templates
    assert "cta_contact" in templates
    # the roles page carries real cards
    cards = next(pg for pg in p["pages"] if pg["template"] == "card_grid")["cards"]
    assert cards and "title" in cards[0] and "bullets" in cards[0]


def test_brochure_plan_still_has_cover_with_title():
    p = planner.plan("brochure", "x", brand_name="Legal Soft", use_llm=False)
    assert p["cover"]["title"]
