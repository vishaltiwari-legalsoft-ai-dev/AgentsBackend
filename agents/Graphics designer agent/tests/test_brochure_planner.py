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


def test_brochure_plan_pages_have_bg_prompts():
    plan = planner._brochure_plan("virtual legal staffing for law firms", "Legal Soft", 4)
    assert plan["cover"].get("bg")
    assert all(p.get("bg") for p in plan["pages"])


def test_compose_brochure_carries_bg_to_pages():
    from graphics_designer_agent.creative import brochure_layout
    plan = {
        "cover": {"title": "T", "subtitle": "S", "bg": "cover scene"},
        "pages": [{"template": "steps", "heading": "H", "steps": [], "bg": "steps scene"}],
    }
    pages = brochure_layout.compose_brochure(plan)
    assert pages[0]["bg"] == "cover scene"
    assert pages[1]["bg"] == "steps scene"
