"""Deterministic Stage-3 text overlay — exact size/position/colour, base preserved,
variable (1–5) sub-headings. Renders with the real Causten fonts, no network."""

from io import BytesIO

from PIL import Image

from graphics_designer_agent import pipeline, text_overlay
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
