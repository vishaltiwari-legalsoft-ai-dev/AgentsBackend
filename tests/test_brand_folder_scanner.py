# backend/tests/test_brand_folder_scanner.py
from pathlib import Path

from app.services.brand_folder_scanner import scan_root


def _touch(p: Path, size: int = 1) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


def test_scan_root_finds_kit_pdf_fonts_and_logos(tmp_path):
    # random-flyer.pdf sits in the SAME Brand Kit dir as the guidelines PDF —
    # the guidelines PDF must win on name hints, not merely by being a PDF
    # under a kit dir.
    _touch(tmp_path / "Acme Health" / "Brand Kit" / "Acme Brand Guidelines.pdf")
    _touch(tmp_path / "Acme Health" / "Brand Kit" / "random-flyer.pdf")
    _touch(tmp_path / "Acme Health" / "Fonts" / "Inter-Bold.ttf")
    _touch(tmp_path / "Acme Health" / "Logos" / "acme-logo-solid.png")
    _touch(tmp_path / "Acme Health" / "Social" / "post1.png")
    _touch(tmp_path / "NoKit Co" / "Social" / "post2.png")

    brands = {b.brand_name: b for b in scan_root(tmp_path)}
    acme = brands["Acme Health"]
    assert acme.kit_pdf and acme.kit_pdf.name == "Acme Brand Guidelines.pdf"
    assert [f.name for f in acme.font_files] == ["Inter-Bold.ttf"]
    assert [f.name for f in acme.logo_candidates] == ["acme-logo-solid.png"]
    assert any(f.name == "post1.png" for f in acme.asset_files)

    nokit = brands["NoKit Co"]
    assert nokit.kit_pdf is None
    assert any("no brand-kit pdf" in n.lower() for n in nokit.notes)


def test_scan_root_skips_files_at_root(tmp_path):
    _touch(tmp_path / "stray.png")
    _touch(tmp_path / "Brand A" / "Brand Kit" / "kit.pdf")
    assert [b.brand_name for b in scan_root(tmp_path)] == ["Brand A"]


def test_scan_root_excludes_third_party_media_kit_without_brand_kit_dir(tmp_path):
    # Regression test for the Stage-0 incident: a large, hint-laden
    # third-party PDF sitting OUTSIDE any "brand kit"-named directory must
    # never be selected as the brand's kit PDF. A PDF can never qualify by
    # filename alone.
    _touch(
        tmp_path / "Some Brand" / "Potential Partners" / "MGMA-Media-Kit-2026.pdf",
        size=50_000,
    )

    brands = {b.brand_name: b for b in scan_root(tmp_path)}
    brand = brands["Some Brand"]
    assert brand.kit_pdf is None
    assert any("no brand-kit pdf" in n.lower() for n in brand.notes)


def test_scan_root_collects_svgs_separately_from_assets(tmp_path):
    _touch(tmp_path / "Brandy" / "Brand Kit" / "Brandy Guidelines.pdf")
    _touch(tmp_path / "Brandy" / "Brand Kit" / "SVGs" / "logo_05.svg")

    brand = next(b for b in scan_root(tmp_path) if b.brand_name == "Brandy")
    assert [f.name for f in brand.svg_files] == ["logo_05.svg"]
    assert all(f.name != "logo_05.svg" for f in brand.asset_files)


def test_scan_root_finds_kit_pdf_in_prefixed_kit_dir(tmp_path):
    # "RCM Brand Kit" — the hint text is a substring, not the whole dir name.
    _touch(tmp_path / "RCM Co" / "RCM Brand Kit" / "Style Guide.pdf")

    brand = next(b for b in scan_root(tmp_path) if b.brand_name == "RCM Co")
    assert brand.kit_pdf and brand.kit_pdf.name == "Style Guide.pdf"


def test_scan_root_kit_pdf_tie_break_by_size(tmp_path):
    # Both candidates live under a brand-kit dir and score identically on
    # name hints ("kit" only) — the larger file must win the tie-break.
    _touch(tmp_path / "TieCo" / "Brand Kit" / "Kit One.pdf", size=1)
    _touch(tmp_path / "TieCo" / "Brand Kit" / "Kit Two.pdf", size=5_000)

    brand = next(b for b in scan_root(tmp_path) if b.brand_name == "TieCo")
    assert brand.kit_pdf and brand.kit_pdf.name == "Kit Two.pdf"
