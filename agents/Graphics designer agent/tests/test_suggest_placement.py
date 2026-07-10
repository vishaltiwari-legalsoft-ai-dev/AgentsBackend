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


def test_without_judgment_response_is_deterministic_and_style_free():
    run = create_run("u-det")
    run["config"]["tokens"] = {"headline": "H", "cta": "Go"}
    out = suggestions.suggest_placement(run)
    assert out["source"] == "deterministic"
    assert "element_styles" not in out and "text_color" not in out


def _vision_run(name: str):
    run = create_run(name)
    run["config"]["tokens"] = {"headline": "H", "cta": "Go"}
    run["config"]["subheadings"] = [{"text": "Sub one"}]
    return run


def test_vision_judgment_zone_wins_over_metadata():
    # Metadata says subject on the LEFT (copy would go right), but the vision
    # brain SAW clean space on the left — the judgment must win.
    run = _vision_run("u-vis-zone")
    run["config"]["element_placement"] = "middle-left"
    judgment = {"zone": "left", "text_color": "dark", "density": "clean", "reason": "r"}
    out = suggestions.suggest_placement(run, judgment=judgment)
    assert out["source"] == "vision"
    assert out["layout"]["headline"]["x"] < 0.5


def test_vision_judgment_writes_colour_and_reason():
    run = _vision_run("u-vis-color")
    judgment = {"zone": "right", "text_color": "white", "density": "clean",
                "reason": "dark gradient on the right"}
    out = suggestions.suggest_placement(run, judgment=judgment)
    assert out["element_styles"]["headline"]["color"] == "white"
    assert out["text_color"] == "white"
    assert out["reason"] == "dark gradient on the right"
    # clean image: no size step-down
    assert "size_pct" not in out["element_styles"]["headline"]


def test_busy_image_steps_headline_down_and_narrows_column():
    run = _vision_run("u-vis-busy")
    clean = suggestions.suggest_placement(
        run, judgment={"zone": "right", "text_color": "dark", "density": "clean", "reason": ""})
    busy = suggestions.suggest_placement(
        run, judgment={"zone": "right", "text_color": "dark", "density": "busy", "reason": ""})
    assert busy["element_styles"]["headline"]["size_pct"] == 6.5
    assert busy["layout"]["headline"]["w"] < clean["layout"]["headline"]["w"]


def test_center_zone_is_vertically_centred():
    # "center" must not render as top-center: its copy stack starts well below
    # the top margin used by the "top" zone.
    run = _vision_run("u-vis-center-y")
    j = {"text_color": "dark", "density": "clean", "reason": ""}
    top = suggestions.suggest_placement(run, judgment={**j, "zone": "top"})["layout"]
    center = suggestions.suggest_placement(run, judgment={**j, "zone": "center"})["layout"]
    assert center["headline"]["y"] > top["headline"]["y"]


def test_vision_layout_stays_inside_the_canvas():
    for zone in ("left", "right", "center", "top", "bottom"):
        run = _vision_run(f"u-vis-{zone}")
        out = suggestions.suggest_placement(
            run, judgment={"zone": zone, "text_color": "dark", "density": "busy", "reason": ""})
        for eid, e in out["layout"].items():
            assert 0 <= e["x"] <= 1 and 0 <= e["y"] <= 1, (zone, eid, e)
            if e["anchor"] == "tl":
                assert e["x"] + e["w"] <= 1.0001, (zone, eid, e)
