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


def test_no_text_element_overflows_the_right_edge():
    # Width must keep tl-anchored copy inside the canvas (x + w <= 1).
    for placement in ("auto", "middle-left", "middle-right", "top-center"):
        run = create_run(f"u-ovf-{placement}")
        run["config"]["tokens"] = {"headline": "A long headline that would wrap", "cta": "Book"}
        run["config"]["subheadings"] = [{"text": "A reasonably long supporting subheading line"}]
        run["config"]["element_placement"] = placement
        out = suggestions.suggest_placement(run)["layout"]
        for eid in ("headline", "subheading-0"):
            e = out[eid]
            assert e["x"] + e["w"] <= 1.0001, (placement, eid, e)


def test_copy_goes_opposite_the_subject():
    # Subject on the LEFT -> copy column starts on the right half.
    run = create_run("u-left")
    run["config"]["tokens"] = {"headline": "H", "cta": "Go"}
    run["config"]["element_placement"] = "middle-left"
    left = suggestions.suggest_placement(run)["layout"]
    assert left["headline"]["x"] >= 0.5
    # Subject on the RIGHT -> copy column on the left half.
    run["config"]["element_placement"] = "middle-right"
    right = suggestions.suggest_placement(run)["layout"]
    assert right["headline"]["x"] < 0.5
