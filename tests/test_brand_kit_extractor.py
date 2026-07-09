# backend/tests/test_brand_kit_extractor.py
import pytest

from app.services.brand_kit_extractor import extract_colors


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


# --- Amendment A: source-ladder additions -----------------------------------

from PIL import Image

from app.services.brand_kit_extractor import (
    extract_pixel_colors,
    extract_svg_colors,
    fonts_from_files,
)


def test_extract_svg_colors_finds_hex_fills_deduped(tmp_path):
    svg = tmp_path / "logo.svg"
    svg.write_text(
        '<svg><path fill="#0892D0"/><rect fill="#FF00AA"/><path fill="#0892D0"/></svg>',
        encoding="utf-8",
    )
    hits = extract_svg_colors(svg)
    assert [h.hex for h in hits] == ["#0892D0", "#FF00AA"]  # deduped, first-seen order
    assert hits[0].page == 0
    assert hits[0].context == "svg:logo.svg"


def test_extract_svg_colors_unreadable_file_returns_empty(tmp_path):
    missing = tmp_path / "missing.svg"
    assert extract_svg_colors(missing) == []


@pytest.mark.parametrize("filename,expected", [
    ("Magistral_Medium.otf", ("Magistral", "Medium")),
    ("Rubik-Regular.ttf", ("Rubik", "Regular")),
    ("Inter_18pt-Bold.ttf", ("Inter", "Bold")),
    ("Oxygen-BoldItalic.otf", ("Oxygen", "BoldItalic")),
])
def test_fonts_from_files_parses_filename_examples(tmp_path, filename, expected):
    path = tmp_path / filename
    path.write_bytes(b"")  # filename parsing only — file content is irrelevant
    [hit] = fonts_from_files([path])
    assert (hit.family, hit.style) == expected
    assert hit.embedded is True
    assert hit.raw_name == filename
    assert hit.pages == []


def test_fonts_from_files_dedupes_on_family_and_style(tmp_path):
    a = tmp_path / "Rubik-Regular.ttf"
    b = tmp_path / "Rubik-Regular.otf"
    a.write_bytes(b"")
    b.write_bytes(b"")
    assert len(fonts_from_files([a, b])) == 1


def test_extract_pixel_colors_excludes_white_finds_exact_blue(tmp_path):
    blue_path = tmp_path / "blue.png"
    white_path = tmp_path / "white.png"
    Image.new("RGB", (100, 100), (0x08, 0x92, 0xD0)).save(blue_path)
    Image.new("RGB", (100, 100), (0xFF, 0xFF, 0xFF)).save(white_path)
    hits = extract_pixel_colors([blue_path, white_path])
    assert [h.hex for h in hits] == ["#0892D0"]
    assert hits[0].page == 0
    assert "pixel-share=" in hits[0].context


def test_extract_pixel_colors_below_min_share_excluded(tmp_path):
    # a color covering < 2% of combined pixels must not surface
    big = tmp_path / "big.png"
    tiny = tmp_path / "tiny.png"
    Image.new("RGB", (100, 100), (0xFF, 0xFF, 0xFF)).save(big)   # white excluded anyway
    Image.new("RGB", (4, 4), (0x11, 0x22, 0x33)).save(tiny)      # 16 / 10016 px ~ 0.16%
    hits = extract_pixel_colors([big, tiny])
    assert hits == []


def test_extract_pixel_colors_skips_corrupt_images(tmp_path):
    bad = tmp_path / "not_an_image.png"
    bad.write_bytes(b"this is not a real png")
    good = tmp_path / "blue.png"
    Image.new("RGB", (10, 10), (0x08, 0x92, 0xD0)).save(good)
    hits = extract_pixel_colors([bad, good])
    assert [h.hex for h in hits] == ["#0892D0"]


def test_build_profile_source_ladder_merges_and_prioritizes(tmp_path, kit_pdf):
    svg_path = tmp_path / "brand.svg"
    svg_path.write_text(
        '<svg><path fill="#1A2B3C"/><path fill="#00FF00"/></svg>', encoding="utf-8"
    )
    blue_png = tmp_path / "creative.png"
    white_png = tmp_path / "bg.png"
    Image.new("RGB", (100, 100), (0x08, 0x92, 0xD0)).save(blue_png)
    Image.new("RGB", (100, 100), (0xFF, 0xFF, 0xFF)).save(white_png)
    font_path = tmp_path / "Helvetica-Regular.otf"
    font_path.write_bytes(b"")

    sources = KitSources(
        kit_pdf=kit_pdf,
        svg_files=[svg_path],
        font_files=[font_path],
        image_files=[blue_png, white_png],
    )
    profile = build_profile("Acme Health", sources, llm=None)

    hexes = {c.hex for c in profile.colors}
    assert {"#1A2B3C", "#00FF00", "#0892D0"} <= hexes
    assert "#FFFFFF" not in hexes

    pdf_version = next(c for c in profile.colors if c.hex == "#1A2B3C")
    assert "Primary" in pdf_version.context  # pdf context wins over svg's duplicate hex

    svg_only = next(c for c in profile.colors if c.hex == "#00FF00")
    assert svg_only.context == "svg:brand.svg"

    pixel_only = next(c for c in profile.colors if c.hex == "#0892D0")
    assert "pixel-share=" in pixel_only.context

    assert profile.confidence == "high"

    helv = next(f for f in profile.fonts if (f.family, f.style) == ("Helvetica", "Regular"))
    assert helv.raw_name == "Helvetica-Regular.otf"  # file-derived source wins over pdf

    assert profile.provenance["svg_files"] == ["brand.svg"]
    assert profile.provenance["image_files_sampled"] == 2
    assert profile.provenance["pages_scanned"] == 2  # kit_pdf fixture has 2 pages


def test_build_profile_confidence_medium_when_only_pixel_hits(tmp_path):
    blue_png = tmp_path / "creative.png"
    Image.new("RGB", (100, 100), (0x08, 0x92, 0xD0)).save(blue_png)
    profile = build_profile("Acme", KitSources(image_files=[blue_png]), llm=None)
    assert profile.confidence == "medium"
    assert [c.hex for c in profile.colors] == ["#0892D0"]


def test_build_profile_confidence_low_when_no_sources():
    profile = build_profile("Acme", KitSources(), llm=None)
    assert profile.confidence == "low"
    assert profile.colors == []
    assert profile.palette == {}
    assert profile.provenance == {
        "kit_pdf": None, "svg_files": [], "image_files_sampled": 0, "pages_scanned": 0,
    }


def test_llm_allowed_set_is_union_of_all_extracted_hexes(tmp_path):
    svg_path = tmp_path / "brand.svg"
    svg_path.write_text('<svg><path fill="#123456"/></svg>', encoding="utf-8")
    blue_png = tmp_path / "creative.png"
    Image.new("RGB", (100, 100), (0x08, 0x92, 0xD0)).save(blue_png)

    def spy_llm(prompt: str) -> str:
        return json.dumps({"primary": ["#123456"], "secondary": ["#0892D0"],
                           "accent": [], "tone_of_voice": "bold"})

    sources = KitSources(svg_files=[svg_path], image_files=[blue_png])
    profile = build_profile("Acme", sources, llm=spy_llm)
    assert "#123456" in profile.primary_colors
    assert "#0892D0" in profile.secondary_colors
