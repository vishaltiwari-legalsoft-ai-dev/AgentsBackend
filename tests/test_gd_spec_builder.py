# backend/tests/test_gd_spec_builder.py
"""Unit D: profile+folder -> templated-brand spec contract.

The real gate is `templated_brands.build_templated_pack(spec)` constructing
without error — everything else here pins the specific field-derivation
rules from the Unit D brief (resolution 2).
"""
import json

from app.services.brand_folder_scanner import BrandFolder
from app.services.brand_kit_extractor import KitSources, build_profile
from app.services.gd_spec_builder import GENERIC_STAGE2_VARIANTS, _slug, build_gd_spec


def _profile(kit_pdf, brand_name="Acme Health"):
    return build_profile(brand_name, KitSources(kit_pdf=kit_pdf), llm=None)


# --------------------------------------------------------------------------- #
# Contract gate
# --------------------------------------------------------------------------- #

def test_spec_satisfies_build_templated_pack_contract(kit_pdf, tmp_path):
    from graphics_designer_agent import templated_brands
    folder = BrandFolder(brand_name="Acme Health", root=tmp_path, kit_pdf=kit_pdf)
    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1")
    pack = templated_brands.build_templated_pack(spec)   # must not raise
    assert pack.id == "acme-health"
    assert pack.firestore_brand_id == "b1"
    assert pack.locked_colors                              # palette flowed through
    assert pack.font_names()                                # non-empty


def test_spec_none_when_too_few_colors(tmp_path):
    # a PDF with <3 colors -> not generatable
    from reportlab.pdfgen import canvas
    pdf = tmp_path / "thin.pdf"
    c = canvas.Canvas(str(pdf)); c.drawString(72, 700, "Primary #112233"); c.save()
    folder = BrandFolder(brand_name="Thin", root=tmp_path, kit_pdf=pdf)
    profile = build_profile("Thin", KitSources(kit_pdf=pdf), llm=None)
    assert build_gd_spec(profile, folder, brand_id=None) is None


# --------------------------------------------------------------------------- #
# id / slug rules
# --------------------------------------------------------------------------- #

def test_slug_lowercases_and_dashes_spaces():
    assert _slug("Acme Health") == "acme-health"


def test_slug_strips_invalid_chars_and_dashes_underscores():
    assert _slug("Acme_Health!!") == "acme-health"


def test_slug_collapses_repeated_dashes_and_trims_edges():
    assert _slug("  Multi   Space--Brand__Name  ") == "multi-space-brand-name"


# --------------------------------------------------------------------------- #
# palette passthrough
# --------------------------------------------------------------------------- #

def test_palette_is_full_nine_key_dict(kit_pdf, tmp_path):
    folder = BrandFolder(brand_name="Acme Health", root=tmp_path, kit_pdf=kit_pdf)
    profile = _profile(kit_pdf)
    spec = build_gd_spec(profile, folder, brand_id="b1")
    assert spec["palette"] == dict(profile.palette)
    assert set(spec["palette"]) == {
        "light", "mid", "deep", "accent", "ink", "hl_from", "hl_to", "cta_from", "cta_to",
    }


# --------------------------------------------------------------------------- #
# Fonts: derived from folder.font_files via fonts_from_files
# --------------------------------------------------------------------------- #

def test_font_fallback_marked_when_no_files(kit_pdf, tmp_path):
    folder = BrandFolder(brand_name="Acme", root=tmp_path, kit_pdf=kit_pdf)  # no font_files
    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1")
    assert spec["font_fallback"] is True
    assert spec["font_variants"]                           # Be Vietnam set substituted


def test_font_fallback_uses_be_vietnam_full_set_verbatim(kit_pdf, tmp_path):
    from graphics_designer_agent.templated_brands import _BEVIETNAM_FULL
    folder = BrandFolder(brand_name="Acme", root=tmp_path, kit_pdf=kit_pdf)
    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1")
    assert spec["font_family"] == "Be Vietnam"
    assert spec["default_font"] == "Be Vietnam Bold"
    assert spec["font_variants"] == _BEVIETNAM_FULL


def test_font_variants_derived_via_fonts_from_files_parsing_rules(kit_pdf, tmp_path):
    # "Inter_18pt-Bold.ttf" -> family "Inter" (size token dropped), style "Bold" —
    # this is fonts_from_files' own parsing rule; we must not reimplement it.
    f = tmp_path / "Inter_18pt-Bold.ttf"
    f.write_bytes(b"")
    folder = BrandFolder(brand_name="Acme", root=tmp_path, kit_pdf=kit_pdf, font_files=[f])
    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1")
    assert spec["font_fallback"] is False
    assert {"name": "Inter Bold", "file": "Inter_18pt-Bold.ttf"} in spec["font_variants"]


def test_font_family_is_most_common_family(kit_pdf, tmp_path):
    f1 = tmp_path / "Inter-Regular.ttf"; f1.write_bytes(b"")
    f2 = tmp_path / "Inter-Bold.ttf"; f2.write_bytes(b"")
    f3 = tmp_path / "OtherFont-Regular.ttf"; f3.write_bytes(b"")
    folder = BrandFolder(brand_name="Acme", root=tmp_path, kit_pdf=kit_pdf,
                         font_files=[f1, f2, f3])
    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1")
    assert spec["font_family"] == "Inter"


def test_default_font_picks_first_bold_variant(kit_pdf, tmp_path):
    f1 = tmp_path / "Inter-Regular.ttf"; f1.write_bytes(b"")
    f2 = tmp_path / "Inter-Bold.ttf"; f2.write_bytes(b"")
    f3 = tmp_path / "Inter-BoldItalic.ttf"; f3.write_bytes(b"")
    folder = BrandFolder(brand_name="Acme", root=tmp_path, kit_pdf=kit_pdf,
                         font_files=[f1, f2, f3])
    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1")
    assert spec["default_font"] == "Inter Bold"


def test_default_font_falls_back_to_first_variant_when_no_bold(kit_pdf, tmp_path):
    f1 = tmp_path / "Inter-Regular.ttf"; f1.write_bytes(b"")
    f2 = tmp_path / "Inter-Italic.ttf"; f2.write_bytes(b"")
    folder = BrandFolder(brand_name="Acme", root=tmp_path, kit_pdf=kit_pdf,
                         font_files=[f1, f2])
    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1")
    assert spec["default_font"] == "Inter Regular"


# --------------------------------------------------------------------------- #
# Generic content fields
# --------------------------------------------------------------------------- #

def test_generic_content_fields(kit_pdf, tmp_path):
    folder = BrandFolder(brand_name="Acme Health", root=tmp_path, kit_pdf=kit_pdf)
    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1")
    name = "Acme Health"
    assert spec["name"] == name
    assert spec["default_headline"] == f"Grow Faster With {name}"
    assert spec["default_highlight"] == name
    assert spec["default_subtext_1"] == "Expert support, ready when you are."
    assert spec["default_subtext_2"] == "Onboard trusted talent in days, not months."
    assert spec["default_cta"] == "Book a Free Consultation"
    assert spec["ctas"] == ["Book a Free Consultation", "Get Started Today", "Talk to Our Team"]
    assert spec["hooks"] == {}


def test_firestore_brand_id_none_when_not_yet_known(kit_pdf, tmp_path):
    folder = BrandFolder(brand_name="Acme Health", root=tmp_path, kit_pdf=kit_pdf)
    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id=None)
    assert spec["firestore_brand_id"] is None


# --------------------------------------------------------------------------- #
# GENERIC_STAGE2_VARIANTS
# --------------------------------------------------------------------------- #

def test_generic_stage2_variants_shape():
    assert [v["id"] for v in GENERIC_STAGE2_VARIANTS] == list("ABCDEF")
    assert [v["category"] for v in GENERIC_STAGE2_VARIANTS] == [
        "people", "people", "object", "flatlay", "architecture", "scene",
    ]
    for v in GENERIC_STAGE2_VARIANTS:
        assert set(v) == {"id", "title", "angle", "category", "desc", "subject"}
        assert v["title"].strip()
        assert v["angle"].strip()
        assert v["desc"].strip()
        assert len(v["subject"].strip()) > 40                # real prose, not a stub


def test_spec_uses_generic_stage2_variants(kit_pdf, tmp_path):
    folder = BrandFolder(brand_name="Acme Health", root=tmp_path, kit_pdf=kit_pdf)
    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1")
    assert spec["stage2_variants"] == GENERIC_STAGE2_VARIANTS


# --------------------------------------------------------------------------- #
# Optional LLM content synthesis (JSON guard, same pattern as Unit A)
# --------------------------------------------------------------------------- #

def test_llm_synthesizes_brand_tuned_content_when_valid(kit_pdf, tmp_path):
    folder = BrandFolder(brand_name="Acme Health", root=tmp_path, kit_pdf=kit_pdf)

    def good_llm(prompt: str) -> str:
        return json.dumps({
            "default_headline": "Scale Faster With Acme Health Virtual Staff",
            "default_highlight": "Acme Health Virtual Staff",
            "default_subtext_1": "Vetted talent, ready in days.",
            "default_subtext_2": "Onboard confidently, backed by our team.",
            "ctas": ["Book a Demo", "See Pricing", "Talk to Sales"],
        })

    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1", llm=good_llm)
    assert spec["default_headline"] == "Scale Faster With Acme Health Virtual Staff"
    assert spec["default_highlight"] == "Acme Health Virtual Staff"
    assert spec["default_subtext_1"] == "Vetted talent, ready in days."
    assert spec["default_subtext_2"] == "Onboard confidently, backed by our team."
    assert spec["ctas"] == ["Book a Demo", "See Pricing", "Talk to Sales"]
    assert spec["default_cta"] == "Book a Free Consultation"    # never LLM-synthesized


def test_llm_malformed_json_keeps_all_generics(kit_pdf, tmp_path):
    folder = BrandFolder(brand_name="Acme Health", root=tmp_path, kit_pdf=kit_pdf)
    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1", llm=lambda p: "not json at all")
    assert spec["default_headline"] == "Grow Faster With Acme Health"
    assert spec["default_highlight"] == "Acme Health"
    assert spec["default_subtext_1"] == "Expert support, ready when you are."
    assert spec["ctas"] == ["Book a Free Consultation", "Get Started Today", "Talk to Our Team"]


def test_llm_missing_required_key_keeps_all_generics(kit_pdf, tmp_path):
    folder = BrandFolder(brand_name="Acme Health", root=tmp_path, kit_pdf=kit_pdf)

    def missing_key_llm(prompt: str) -> str:
        return json.dumps({
            "default_headline": "Scale Faster",
            "default_highlight": "Scale",
            "default_subtext_1": "",                       # empty -> invalid
            "default_subtext_2": "Onboard confidently.",
            "ctas": ["Book a Demo"],
        })

    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1", llm=missing_key_llm)
    assert spec["default_headline"] == "Grow Faster With Acme Health"
    assert spec["default_subtext_1"] == "Expert support, ready when you are."


def test_llm_ctas_must_be_nonempty_list_of_strings(kit_pdf, tmp_path):
    folder = BrandFolder(brand_name="Acme Health", root=tmp_path, kit_pdf=kit_pdf)

    def bad_ctas_llm(prompt: str) -> str:
        return json.dumps({
            "default_headline": "Scale Faster With Acme Health",
            "default_highlight": "Acme Health",
            "default_subtext_1": "Vetted talent, ready in days.",
            "default_subtext_2": "Onboard confidently, backed by our team.",
            "ctas": [],
        })

    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1", llm=bad_ctas_llm)
    assert spec["ctas"] == ["Book a Free Consultation", "Get Started Today", "Talk to Our Team"]
    assert spec["default_headline"] == "Grow Faster With Acme Health"


def test_llm_highlight_not_substring_of_headline_reverts_both_to_generic(kit_pdf, tmp_path):
    folder = BrandFolder(brand_name="Acme Health", root=tmp_path, kit_pdf=kit_pdf)

    def bad_llm(prompt: str) -> str:
        return json.dumps({
            "default_headline": "Scale Your Team Today",
            "default_highlight": "Totally Different Phrase",
            "default_subtext_1": "Vetted talent, ready in days.",
            "default_subtext_2": "Onboard confidently, backed by our team.",
            "ctas": ["Book a Demo"],
        })

    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1", llm=bad_llm)
    assert spec["default_headline"] == "Grow Faster With Acme Health"   # reverted to generic
    assert spec["default_highlight"] == "Acme Health"                   # reverted to generic
    # non-headline fields from the same call still applied (they independently pass shape checks)
    assert spec["default_subtext_1"] == "Vetted talent, ready in days."
    assert spec["ctas"] == ["Book a Demo"]


def test_llm_none_uses_generics(kit_pdf, tmp_path):
    folder = BrandFolder(brand_name="Acme Health", root=tmp_path, kit_pdf=kit_pdf)
    spec = build_gd_spec(_profile(kit_pdf), folder, brand_id="b1", llm=None)
    assert spec["default_headline"] == "Grow Faster With Acme Health"
