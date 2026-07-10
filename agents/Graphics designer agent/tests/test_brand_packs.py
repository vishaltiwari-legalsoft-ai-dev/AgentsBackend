"""Templated brand packs (Phase C) — build, render with the brand's own font,
run the full pipeline per brand, and carry the palette + AR anchor in gradients."""

import copy
import logging
from io import BytesIO

import pytest
from PIL import Image

from graphics_designer_agent import pipeline, registry, suggestions, templated_brands
from graphics_designer_agent.stage3_text import text_overlay
from graphics_designer_agent.runs import create_run

TEMPLATED = ["medvirtual", "remote_attorneys"]

_EXTRA_GRADIENT_1 = {
    "id": "G1", "title": "Test Grad One", "desc": "Custom preset one.",
    "css_gradient": "linear-gradient(90deg, #ABCDEF 0%, #123456 100%)",
    "prompt": "16:9 aspect ratio custom preset gradient one, calm and premium, no noise, no text.",
}
_EXTRA_GRADIENT_2 = {
    "id": "G2", "title": "Test Grad Two", "desc": "Custom preset two.",
    "css_gradient": "radial-gradient(circle, #654321 0%, #FEDCBA 100%)",
    "prompt": "16:9 aspect ratio custom preset gradient two, calm and premium, no noise, no text.",
}


def _logo_png() -> bytes:
    buf = BytesIO()
    Image.new("RGBA", (140, 140), (3, 4, 94, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _base_png(w=480, h=600) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (w, h), (200, 210, 230)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize("bid", TEMPLATED)
def test_create_run_seeds_brand_defaults(bid):
    pack = registry.get_pack(bid)
    run = create_run("user-x", bid)
    assert run["brand_id"] == bid
    assert run["config"]["font"] == pack.default_font
    assert run["config"]["tokens"]["headline"] == pack.default_headline


@pytest.mark.parametrize("bid", TEMPLATED)
def test_full_pipeline_per_brand_reaches_done(bid):
    run = create_run("user-x", bid)
    pipeline.generate(run, 1, variant="A")
    pipeline.approve(run, 1)
    pipeline.generate(run, 2, variant="B")
    pipeline.approve(run, 2)
    for t in run["config"]["tokens_approved"]:
        run["config"]["tokens_approved"][t] = True
    for s in run["config"]["subheadings"]:
        s["approved"] = True
    pipeline.generate(run, 3)
    pipeline.approve(run, 3)
    pipeline.generate_stage4(run, _logo_png(), use_ai=False)
    pipeline.approve(run, 4)
    assert run["state"] == "DONE"


@pytest.mark.parametrize("bid", TEMPLATED)
def test_gradients_carry_ar_anchor_and_deep_palette_hex(bid):
    pack = registry.get_pack(bid)
    deep = pack.locked_colors["gradient"][3]  # every gradient template uses the deep tone
    assert pack.stage1_variants
    for v in pack.stage1_variants:
        text = pack.load_prompt(v["prompt_file"])
        assert "16:9 aspect ratio" in text
        assert deep.lower() in text.lower()


def test_render_overlay_uses_brand_font_dir():
    pack = registry.get_pack("medvirtual")
    spec = {
        "headline": {"text": "Hire Vetted Medical VAs", "highlight": "Medical VAs",
                     "font": pack.default_font, "size_pct": 8.0, "color": "dark",
                     "highlight_color": "gradient", "placement": "left", "offset": (0, 0)},
        "subheadings": [],
        "cta": {"text": "Book a Free Consultation", "font": pack.default_font,
                "size_pct": 3.4, "placement": "bottom", "offset": (0, 0)},
    }
    out = text_overlay.render_overlay(_base_png(), spec, 480, 600, pack=pack)
    img = Image.open(BytesIO(out))
    assert img.size == (480, 600) and img.format == "PNG"
    # the brand's font really resolves on disk (Be Vietnam, not Causten)
    assert (pack.fonts_dir / pack.font_file(pack.default_font)).exists()


@pytest.mark.parametrize("bid", TEMPLATED)
def test_suggestions_are_brand_scoped(bid):
    pack = registry.get_pack(bid)
    # offline (no OpenRouter in the test env) → curated fallbacks, deterministic
    assert suggestions.suggest_gradient({}, pack=pack)["gradient"]["title"]
    assert suggestions.suggest_element({}, pack=pack)["element"]["subject"]
    assert suggestions.generate_hooks("A", pack=pack)["headlines"]
    assert suggestions.recommend_font("A", pack=pack)["family"] == pack.font_family


# --------------------------------------------------------------------------- #
# Unit P1 — extra_gradients spec key
# --------------------------------------------------------------------------- #

def test_spec_without_extra_gradients_key_is_byte_identical():
    """Golden: absent key -> current counts, unchanged (byte-identical guarantee)."""
    spec = copy.deepcopy(templated_brands._MEDVIRTUAL)
    assert "extra_gradients" not in spec
    pack = templated_brands.build_templated_pack(spec)
    assert len(pack.stage1_variants) == 5
    assert len(pack.curated_gradients) == 5
    assert pack.verify_integrity() == []


def test_extra_gradients_append_to_stage1_and_curated():
    spec = copy.deepcopy(templated_brands._MEDVIRTUAL)
    spec["extra_gradients"] = [_EXTRA_GRADIENT_1, _EXTRA_GRADIENT_2]
    pack = templated_brands.build_templated_pack(spec)

    assert len(pack.stage1_variants) == 7
    assert len(pack.curated_gradients) == 7
    ids = [v["id"] for v in pack.stage1_variants]
    assert ids[-2:] == ["G1", "G2"]

    cids = [c["cid"] for c in pack.curated_gradients]
    assert "grad-g1" in cids
    assert "grad-g2" in cids

    # prompt_file naming convention
    g1_variant = next(v for v in pack.stage1_variants if v["id"] == "G1")
    assert g1_variant["prompt_file"] == "stage1_gradient_G1.txt"

    # prompts load (from inline_prompts, no disk .txt shipped for templated brands)
    for v in pack.stage1_variants[-2:]:
        assert pack.load_prompt(v["prompt_file"])

    # integrity clean — canonical hashes match inline content even though no
    # file with this exact name exists on disk (inline-served prompt)
    assert pack.verify_integrity() == []


def test_extra_gradient_id_collision_with_shared_template_is_skipped_and_logged(caplog):
    spec = copy.deepcopy(templated_brands._MEDVIRTUAL)
    colliding = dict(_EXTRA_GRADIENT_1, id="A")  # "A" collides with a shared template id
    spec["extra_gradients"] = [colliding, _EXTRA_GRADIENT_2]

    with caplog.at_level(logging.WARNING):
        pack = templated_brands.build_templated_pack(spec)

    # only G2 appended; the colliding "A" is skipped, not fatal
    assert len(pack.stage1_variants) == 6
    ids = [v["id"] for v in pack.stage1_variants]
    assert ids.count("A") == 1  # the shared template's own "A", not duplicated
    assert "G2" in ids
    assert any("collis" in r.getMessage().lower() or "skip" in r.getMessage().lower()
               for r in caplog.records)
    assert pack.verify_integrity() == []


def test_extra_gradient_hexes_unioned_into_brand_gradient_hexes():
    spec = copy.deepcopy(templated_brands._MEDVIRTUAL)
    spec["extra_gradients"] = [_EXTRA_GRADIENT_1]
    pack = templated_brands.build_templated_pack(spec)
    assert "#ABCDEF" in pack.brand_gradient_hexes
    assert "#123456" in pack.brand_gradient_hexes
