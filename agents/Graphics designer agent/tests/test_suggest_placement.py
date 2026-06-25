"""AI Suggest Placement — heuristic arranger returns a polished layout for the
elements that are present, all coords in [0,1]. Offline + deterministic."""

from graphics_designer_agent import suggestions
from graphics_designer_agent.runs import create_run


def test_arranges_present_elements_only():
    run = create_run("u-arrange")
    run["config"]["tokens"] = {"headline": "Big News", "cta": "Book now", "venue": "", "website": ""}
    run["config"]["subheadings"] = [{"text": "Point one"}, {"text": "Point two"}]
    out = suggestions.suggest_placement(run)["layout"]
    assert set(out) == {"headline", "subheading-0", "subheading-1", "cta"}
    for entry in out.values():
        assert 0 <= entry["x"] <= 1 and 0 <= entry["y"] <= 1
        assert 0 < entry["w"] <= 1 and entry["anchor"] in (
            "tl", "tc", "tr", "ml", "mc", "mr", "bl", "bc", "br")


def test_includes_optional_fields_when_filled():
    run = create_run("u-arrange2")
    run["config"]["tokens"] = {"headline": "H", "cta": "Go", "venue": "Hall A", "website": "x.com"}
    run["config"]["subheadings"] = []
    out = suggestions.suggest_placement(run)["layout"]
    assert "venue" in out and "website" in out
    # headline above the CTA (smaller y = higher on the canvas)
    assert out["headline"]["y"] < out["cta"]["y"]
