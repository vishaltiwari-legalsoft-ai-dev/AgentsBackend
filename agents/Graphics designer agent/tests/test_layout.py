"""Stage-3 absolute-coordinate layout model (free-drag canvas foundation).

Coordinate convention: x/y ∈ [0,1] are the fractional position of an element's
ANCHOR point on the canvas; w ∈ (0,1] is max width as a fraction of canvas
width; anchor ∈ ANCHORS. Elements with NO explicit coords stay "auto" (rendered
by the legacy zone+stack path) so existing runs are byte-identical.
"""

from graphics_designer_agent import layout
from graphics_designer_agent.runs import create_run


# ── anchor math ───────────────────────────────────────────────────────────────
def test_anchor_center_centers_box():
    left, top = layout.anchor_to_xy(0.5, 0.5, 100, 40, "mc", 1000, 800)
    assert (left, top) == (450, 380)


def test_anchor_bottom_right():
    left, top = layout.anchor_to_xy(1.0, 1.0, 100, 40, "br", 1000, 800)
    assert (left, top) == (900, 760)


def test_anchor_top_left_is_identity():
    left, top = layout.anchor_to_xy(0.0, 0.0, 100, 40, "tl", 1000, 800)
    assert (left, top) == (0, 0)


def test_unknown_anchor_falls_back_to_center():
    assert layout.anchor_to_xy(0.5, 0.5, 100, 40, "??", 1000, 800) == (450, 380)


# ── color resolution (named tokens + hex) ─────────────────────────────────────
_THEME = {"dark": (15, 15, 15), "white": (255, 255, 255),
          "grad": ((134, 175, 254), (38, 83, 171)),
          "cta": ((255, 138, 61), (242, 106, 26))}


def test_resolve_color_named():
    assert layout.resolve_color("dark", _THEME, "dark") == ("solid", (15, 15, 15))
    assert layout.resolve_color("white", _THEME, "dark") == ("solid", (255, 255, 255))
    assert layout.resolve_color("gradient", _THEME, "dark") == ("grad", _THEME["grad"])


def test_resolve_color_hex():
    assert layout.resolve_color("#FF0000", _THEME, "dark") == ("solid", (255, 0, 0))


def test_resolve_color_invalid_falls_back_to_default():
    assert layout.resolve_color("#zzz", _THEME, "dark") == ("solid", (15, 15, 15))
    assert layout.resolve_color(None, _THEME, "white") == ("solid", (255, 255, 255))


# ── legacy placement-key → coordinates ────────────────────────────────────────
def test_default_coords_match_legacy_zones():
    left = layout.default_coords("left", "text")
    assert left["anchor"] == "ml" and abs(left["x"] - 0.06) < 1e-6 and abs(left["w"] - 0.42) < 1e-6
    center = layout.default_coords("center", "text")
    assert center["anchor"] == "mc" and abs(center["x"] - 0.5) < 1e-6
    top = layout.default_coords("top", "text")
    assert top["anchor"] == "tc" and abs(top["y"] - 0.06) < 1e-6
    bottom = layout.default_coords("bottom", "text")
    assert bottom["anchor"] == "bc" and abs(bottom["y"] - 0.94) < 1e-6
    right = layout.default_coords("right", "text")
    assert right["anchor"] == "mr" and abs(right["x"] - 0.94) < 1e-6


# ── resolve_layers ────────────────────────────────────────────────────────────
def test_resolve_layers_marks_explicit_coords_pinned():
    run = create_run("u-layers")
    run["config"]["tokens"] = {"headline": "Hello World", "cta": "Go"}
    run["config"]["layout"] = {"headline": {"x": 0.2, "y": 0.3, "w": 0.5, "anchor": "tl"}}
    layers = layout.resolve_layers(run)
    head = next(l for l in layers if l["id"] == "headline")
    assert head["pinned"] is True
    assert (head["x"], head["y"], head["anchor"]) == (0.2, 0.3, "tl")


def test_resolve_layers_auto_when_no_coords():
    run = create_run("u-layers2")
    run["config"]["tokens"] = {"headline": "Hi"}
    layers = layout.resolve_layers(run)
    head = next(l for l in layers if l["id"] == "headline")
    assert head["pinned"] is False


def test_is_valid_color():
    assert layout.is_valid_color("dark")
    assert layout.is_valid_color("cta")
    assert layout.is_valid_color("#FF8A3D")
    assert layout.is_valid_color("#abcdef")
    assert not layout.is_valid_color("#fff")        # 3-digit not allowed
    assert not layout.is_valid_color("reddish")
    assert not layout.is_valid_color(None)


def test_venue_website_layers_only_when_filled():
    run = create_run("u-fields")
    run["config"]["tokens"] = {"headline": "H", "venue": "", "website": ""}
    ids = {l["id"] for l in layout.resolve_layers(run)}
    assert "venue" not in ids and "website" not in ids
    run["config"]["tokens"] = {"headline": "H", "venue": "Hall A", "website": "x.com"}
    layers = {l["id"]: l for l in layout.resolve_layers(run)}
    assert "venue" in layers and "website" in layers
    assert layers["venue"]["anchor"] == "bl" and layers["website"]["anchor"] == "br"


def test_sanitize_shapes_filters_and_clamps():
    out = layout.sanitize_shapes([
        {"kind": "circle", "x": 2, "y": -1, "w": 0, "fill": "bad", "stroke": "#000000", "stroke_w": 9999},
        {"kind": "icon", "icon": "nope"},
    ])
    assert len(out) == 2
    a = out[0]
    assert a["kind"] == "circle" and a["x"] == 1.0 and a["y"] == 0.0 and a["w"] == 0.3
    assert a["fill"] == "#FFFFFF" and a["stroke"] == "#000000" and a["stroke_w"] <= 200
    assert out[1]["icon"] == "dot"  # invalid icon → safe default


def test_sanitize_shapes_rejects_bad_kind():
    import pytest
    with pytest.raises(ValueError):
        layout.sanitize_shapes([{"kind": "hexagon"}])


def test_sanitize_shapes_caps_count():
    assert len(layout.sanitize_shapes([{"kind": "rect"}] * 50, max_n=30)) == 30


def test_resolve_layers_includes_shapes():
    run = create_run("u-shapes")
    run["config"]["tokens"] = {"headline": "H"}
    run["config"]["shapes"] = [{"id": "shape-0", "kind": "rect", "x": 0.5, "y": 0.5,
                                "w": 0.4, "h": 0.2, "anchor": "mc", "fill": "#ffffff"}]
    shp = [l for l in layout.resolve_layers(run) if l["type"] == "shape"]
    assert len(shp) == 1 and shp[0]["kind"] == "rect" and shp[0]["pinned"] is True


def test_clamp_entry_bounds_and_anchor():
    bad = layout.clamp_entry({"x": 1.4, "y": -0.2, "w": 3, "anchor": "zz"})
    assert bad == {"x": 1.0, "y": 0.0, "w": 0.42, "anchor": "mc"}
    ok = layout.clamp_entry({"x": 0.3, "y": 0.7, "w": 0.5, "anchor": "tl"})
    assert ok == {"x": 0.3, "y": 0.7, "w": 0.5, "anchor": "tl"}
    assert layout.clamp_entry({"x": "nan-ish", "w": 0}) == {"x": 0.5, "y": 0.5, "w": 0.42, "anchor": "mc"}


def test_resolve_layers_includes_cta_and_subheadings():
    run = create_run("u-layers3")
    run["config"]["tokens"] = {"headline": "H", "cta": "Book now"}
    run["config"]["subheadings"] = [{"text": "one"}, {"text": "two"}]
    ids = {l["id"] for l in layout.resolve_layers(run)}
    assert {"headline", "cta", "subheading-0", "subheading-1"} <= ids


def _run_with(cfg_extra):
    cfg = {"tokens": {"headline": "Hi"}, "aspect_ratio": "1:1"}
    cfg.update(cfg_extra)
    return {"brand_id": None, "config": cfg}


def test_resolve_layers_appends_elements():
    run = _run_with({"elements": [
        {"id": "e1", "kind": "emoji", "ref": "😀", "x": 0.5, "y": 0.5,
         "w": 0.2, "h": 0.2, "anchor": "mc", "z": 7, "rotation": 0.0,
         "opacity": 1.0, "fill": "#1746A2"},
    ]})
    layers = layout.resolve_layers(run)
    els = [l for l in layers if l.get("type") == "element"]
    assert len(els) == 1
    assert els[0]["kind"] == "emoji" and els[0]["ref"] == "😀"
    assert els[0]["pinned"] is True


def test_resolve_layers_no_elements_unchanged():
    run = _run_with({})
    layers = layout.resolve_layers(run)
    assert all(l.get("type") != "element" for l in layers)
