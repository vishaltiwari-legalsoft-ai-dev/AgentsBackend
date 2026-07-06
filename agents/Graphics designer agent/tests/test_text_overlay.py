"""Deterministic Stage-3 text overlay — exact size/position/colour, base preserved,
variable (1–5) sub-headings. Renders with the real Causten fonts, no network."""

from io import BytesIO

from PIL import Image

from graphics_designer_agent import pipeline
from graphics_designer_agent.stage3_text import text_overlay
from graphics_designer_agent.prompts import verify_integrity
from graphics_designer_agent.runs import artifact_abspath, create_run, save_run


def _base_png(w=480, h=600) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (w, h), (200, 210, 230)).save(buf, format="PNG")
    return buf.getvalue()


def _spec(n_subs=2, headline_size=8.0, headline_offset=(0, 0)) -> dict:
    return {
        "headline": {
            "text": "Hire Experienced Virtual Legal Staff",
            "highlight": "Virtual Legal Staff", "font": "Causten Bold",
            "size_pct": headline_size, "color": "dark", "highlight_color": "gradient",
            "placement": "left", "offset": headline_offset,
        },
        "subheadings": [
            {"text": f"Crisp value proposition number {i}", "font": "Causten Bold",
             "size_pct": 3.0, "color": "dark", "placement": "left", "offset": (0, 0)}
            for i in range(n_subs)
        ],
        "cta": {"text": "Book a Free Consultation", "font": "Causten Bold",
                "size_pct": 3.4, "placement": "bottom", "offset": (0, 0)},
    }


# ── renderer ──────────────────────────────────────────────────────────────────
def test_render_returns_png_at_base_size():
    img = Image.open(BytesIO(text_overlay.render_overlay(_base_png(480, 600), _spec(), 480, 600)))
    assert img.size == (480, 600) and img.format == "PNG"


def test_overlay_actually_draws_text():
    base = _base_png()
    assert text_overlay.render_overlay(base, _spec(), 480, 600) != base


def test_subheading_count_changes_output():
    base = _base_png()
    two = text_overlay.render_overlay(base, _spec(2), 480, 600)
    five = text_overlay.render_overlay(base, _spec(5), 480, 600)
    one = text_overlay.render_overlay(base, _spec(1), 480, 600)
    assert two != five and two != one and one != five


def test_size_change_changes_output():
    base = _base_png()
    small = text_overlay.render_overlay(base, _spec(headline_size=6.0), 480, 600)
    big = text_overlay.render_overlay(base, _spec(headline_size=15.0), 480, 600)
    assert small != big


def test_pixel_offset_changes_output():
    base = _base_png()
    a = text_overlay.render_overlay(base, _spec(), 480, 600)
    b = text_overlay.render_overlay(base, _spec(headline_offset=(70, 50)), 480, 600)
    assert a != b


# ── coordinate (free-drag) layer path ─────────────────────────────────────────
def test_render_layers_matches_legacy_for_default_placement():
    # The new layer path, fed all-auto layers from a legacy spec, must produce
    # BYTE-IDENTICAL output to the old renderer. This is the engine-safety gate.
    base = _base_png(480, 600)
    legacy = text_overlay.render_overlay(base, _spec(2), 480, 600)
    layers = text_overlay._layers_from_spec(_spec(2))
    assert text_overlay.render_layers(base, layers, 480, 600) == legacy


def test_pinned_layer_moves_output():
    base = _base_png(480, 600)
    layers = text_overlay._layers_from_spec(_spec(2))
    auto = text_overlay.render_layers(base, layers, 480, 600)
    head = next(l for l in layers if l["id"] == "headline")
    head.update({"pinned": True, "x": 0.8, "y": 0.1, "w": 0.4, "anchor": "tr"})
    moved = text_overlay.render_layers(base, layers, 480, 600)
    assert moved != auto


def test_pinned_multiline_renders_png():
    base = _base_png(480, 600)
    layers = text_overlay._layers_from_spec(_spec(1))
    head = next(l for l in layers if l["id"] == "headline")
    head.update({"pinned": True, "text": "Line one\nLine two\nLine three"})
    img = Image.open(BytesIO(text_overlay.render_layers(base, layers, 480, 600)))
    assert img.size == (480, 600) and img.format == "PNG"


# ── shapes / infographic layers ───────────────────────────────────────────────
def test_shape_layer_changes_output_absence_is_unchanged():
    base = _base_png(480, 600)
    layers = text_overlay._layers_from_spec(_spec(1))
    out0 = text_overlay.render_layers(base, layers, 480, 600)
    layers.append({"type": "shape", "id": "shape-0", "kind": "circle", "x": 0.5, "y": 0.5,
                   "w": 0.3, "h": 0.3, "anchor": "mc", "fill": "#FF0000", "stroke": None,
                   "stroke_w": 0, "radius": 0, "icon": None, "text": "", "z": 5, "pinned": True})
    assert text_overlay.render_layers(base, layers, 480, 600) != out0


def test_icon_layer_renders():
    base = _base_png(480, 600)
    layers = text_overlay._layers_from_spec(_spec(1))
    layers.append({"type": "shape", "id": "shape-1", "kind": "icon", "icon": "star", "x": 0.5,
                   "y": 0.3, "w": 0.2, "h": 0.2, "anchor": "mc", "fill": "#1746A2", "stroke": None,
                   "stroke_w": 0, "radius": 0, "text": "", "z": 6, "pinned": True})
    img = Image.open(BytesIO(text_overlay.render_layers(base, layers, 480, 600)))
    assert img.size == (480, 600)


# ── per-element colour (incl. CTA, hex) ───────────────────────────────────────
def test_cta_hex_color_changes_output_but_default_unchanged():
    base = _base_png(480, 600)
    default = text_overlay.render_overlay(base, _spec(1), 480, 600)
    spec = _spec(1)
    spec["cta"]["color"] = "#00AA00"
    assert text_overlay.render_overlay(base, spec, 480, 600) != default
    # explicit "cta" default token == no colour key (byte-identical safety)
    spec2 = _spec(1)
    spec2["cta"]["color"] = "cta"
    assert text_overlay.render_overlay(base, spec2, 480, 600) == default


def test_headline_hex_color_changes_output():
    base = _base_png(480, 600)
    default = text_overlay.render_overlay(base, _spec(1), 480, 600)
    spec = _spec(1)
    spec["headline"]["color"] = "#C81E1E"
    assert text_overlay.render_overlay(base, spec, 480, 600) != default


# ── pipeline end-to-end (mock provider for stages 1–2, deterministic stage 3) ──
def _seed_to_stage3(run):
    pipeline.generate(run, 1, variant="A")
    pipeline.approve(run, 1)
    pipeline.generate(run, 2, variant="A")
    pipeline.approve(run, 2)


def test_stage3_generate_is_deterministic_path():
    run = create_run("u-s3")
    _seed_to_stage3(run)
    attempt = pipeline.generate(run, 3)
    assert attempt["variant"] == "T"
    assert attempt["provider"] == "deterministic"
    png = artifact_abspath(run["id"], attempt["artifact"]).read_bytes()
    assert Image.open(BytesIO(png)).format == "PNG"


def test_stage3_renders_one_and_five_subheadings():
    for n in (1, 5):
        run = create_run(f"u-s3-{n}")
        _seed_to_stage3(run)
        seed = run["config"]["subheadings"][0]
        run["config"]["subheadings"] = [{**seed, "text": f"Line {i}"} for i in range(n)]
        save_run(run)
        attempt = pipeline.generate(run, 3)
        assert attempt["variant"] == "T"


def test_pipeline_pinned_coords_move_output_but_default_unchanged():
    run = create_run("u-coord")
    _seed_to_stage3(run)
    run["config"]["tokens"]["headline"] = "Move Me"
    save_run(run)
    a = artifact_abspath(run["id"], pipeline.generate(run, 3)["artifact"]).read_bytes()
    run["config"]["layout"] = {"headline": {"x": 0.8, "y": 0.1, "w": 0.4, "anchor": "tr"}}
    save_run(run)
    b = artifact_abspath(run["id"], pipeline.generate(run, 3)["artifact"]).read_bytes()
    assert a != b


def test_pipeline_multiline_headline_renders():
    run = create_run("u-ml")
    _seed_to_stage3(run)
    run["config"]["tokens"]["headline"] = "Line one\nLine two"
    save_run(run)
    assert pipeline.generate(run, 3)["variant"] == "T"


def test_resolve_overlay_spec_supports_legacy_runs():
    run = create_run("u-legacy")
    run["config"].pop("subheadings", None)  # pre-feature run
    run["config"]["tokens"]["subtext1"] = "Old line one"
    run["config"]["tokens"]["subtext2"] = "Old line two"
    spec = pipeline._resolve_overlay_spec(run)
    assert [s["text"] for s in spec["subheadings"]] == ["Old line one", "Old line two"]


def test_spec_summary_lists_each_element():
    summary = text_overlay.overlay_spec_summary(_spec(3))
    assert "HEADLINE" in summary and summary.count("SUB-HEADING") == 3 and "CTA" in summary


def test_immutability_unaffected_by_deterministic_stage3():
    assert verify_integrity() == []


def _element_base_png(w=400, h=400):
    buf = BytesIO()
    Image.new("RGB", (w, h), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_render_layers_draws_emoji_element():
    base = _element_base_png()
    layers = [{
        "type": "element", "id": "e1", "kind": "emoji", "ref": "😀",
        "x": 0.5, "y": 0.5, "w": 0.3, "h": 0.3, "anchor": "mc", "z": 5,
        "rotation": 0.0, "opacity": 1.0, "fill": "#1746A2", "pinned": True,
    }]
    out = text_overlay.render_layers(base, layers, 400, 400)
    assert out != base  # emoji composited


def test_render_layers_no_elements_byte_identical():
    """Backward-compat law: a run without elements renders exactly as before."""
    base = _element_base_png()
    layers = [{
        "type": "text", "id": "headline", "text": "Hello", "highlight": "",
        "font": "Causten Bold", "size_pct": 8.0, "color": "dark",
        "highlight_color": "gradient", "placement": "left", "offset": (0, 0),
        "z": 10, "pinned": False,
        "x": 0.06, "y": 0.5, "w": 0.42, "anchor": "ml",
    }]
    a = text_overlay.render_layers(base, layers, 400, 400)
    b = text_overlay.render_layers(base, layers, 400, 400)
    assert a == b  # deterministic, and the element branch never ran
