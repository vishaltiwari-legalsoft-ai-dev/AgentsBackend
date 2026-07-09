# backend/tests/test_brand_kit_extractor.py
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.services.brand_kit_extractor import extract_colors


@pytest.fixture()
def kit_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "kit.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 14)
    c.drawString(72, 760, "Brand Colors")
    c.drawString(72, 730, "Primary  #1A2B3C")
    c.drawString(72, 700, "Secondary  #24B9CE")
    c.drawString(72, 670, "Accent HEX 19B1E3")
    c.drawString(72, 640, "Ink  R: 22, G: 21, B: 17")
    c.showPage()
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 760, "Typography: Be Vietnam Pro")
    c.save()
    return path


def test_extract_colors_finds_exact_hexes(kit_pdf):
    hexes = {h.hex for h in extract_colors(kit_pdf)}
    assert {"#1A2B3C", "#24B9CE", "#19B1E3", "#161511"} <= hexes


def test_extract_colors_keeps_context_and_page(kit_pdf):
    primary = next(h for h in extract_colors(kit_pdf) if h.hex == "#1A2B3C")
    assert "Primary" in primary.context
    assert primary.page == 1


def test_bare_hex_needs_hex_keyword_on_line(kit_pdf):
    # "760" in coordinates etc. must never be parsed; only lines saying HEX
    # may contribute bare 6-char hexes.
    hexes = {h.hex for h in extract_colors(kit_pdf)}
    assert "#000760" not in hexes


from app.services.brand_kit_extractor import extract_fonts


def test_extract_fonts_enumerates_families_and_styles(kit_pdf):
    fonts = extract_fonts(kit_pdf)
    got = {(f.family, f.style) for f in fonts}
    assert ("Helvetica", "Regular") in got
    assert ("Helvetica", "Bold") in got


def test_extract_fonts_strips_subset_prefix():
    from app.services.brand_kit_extractor import _clean_basefont
    assert _clean_basefont("ABCDEF+BeVietnamPro-Bold") == ("BeVietnamPro", "Bold")
    assert _clean_basefont("Helvetica") == ("Helvetica", "Regular")


from app.services.brand_kit_extractor import derive_palette


def test_derive_palette_maps_medvirtual_like_hexes():
    # real MedVirtual brand hexes — known-good shape
    palette = derive_palette(["#A1D7E2", "#24B9CE", "#137A9A", "#19B1E3"])
    assert set(palette) == {"light", "mid", "deep", "accent", "ink",
                            "hl_from", "hl_to", "cta_from", "cta_to"}
    assert palette["light"] == "#A1D7E2"   # lightest by luminance
    assert palette["deep"] == "#137A9A"    # darkest
    assert palette["accent"] in {"#24B9CE", "#19B1E3"}  # most saturated middle


def test_derive_palette_uses_darkest_as_ink_when_truly_dark():
    palette = derive_palette(["#A1D7E2", "#24B9CE", "#161511"])
    assert palette["ink"] == "#161511"


def test_derive_palette_requires_three_colors():
    import pytest
    with pytest.raises(ValueError):
        derive_palette(["#FFFFFF", "#000000"])


import json

from app.services.brand_kit_extractor import KitSources, build_profile


def test_build_profile_offline_labels_by_context(kit_pdf):
    profile = build_profile("Acme Health", KitSources(kit_pdf=kit_pdf), llm=None)
    assert "#1A2B3C" in profile.primary_colors      # line said "Primary"
    assert "#24B9CE" in profile.secondary_colors    # line said "Secondary"
    assert "#19B1E3" in profile.accent_colors       # line said "Accent"
    assert profile.confidence == "high"             # explicit role words found
    assert profile.palette["light"]                 # derive_palette ran
    assert profile.provenance["kit_pdf"].endswith("kit.pdf")


def test_llm_cannot_invent_colors(kit_pdf):
    def lying_llm(prompt: str) -> str:
        return json.dumps({"primary": ["#DEADBF"], "secondary": ["#24B9CE"],
                           "accent": [], "tone_of_voice": "confident, warm"})
    profile = build_profile("Acme Health", KitSources(kit_pdf=kit_pdf), llm=lying_llm)
    assert "#DEADBF" not in (profile.primary_colors + profile.secondary_colors
                             + profile.accent_colors)   # invented hex discarded
    assert "#24B9CE" in profile.secondary_colors        # real hex accepted
    assert profile.tone_of_voice == "confident, warm"


def test_build_profile_survives_broken_llm(kit_pdf):
    profile = build_profile("Acme", KitSources(kit_pdf=kit_pdf), llm=lambda p: "not json at all")
    assert profile.primary_colors                       # heuristic fallback used
