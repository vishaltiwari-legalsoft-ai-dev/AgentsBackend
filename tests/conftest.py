# backend/tests/conftest.py
"""Shared fixtures for the backend test suite."""
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


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


@pytest.fixture()
def valid_dyn_spec(kit_pdf: Path, tmp_path: Path) -> dict:
    """A spec dict that satisfies the `build_templated_pack` contract, built
    the same way real ingestion builds one (`gd_spec_builder.build_gd_spec`)
    from the shared `kit_pdf` fixture (4 hexes -> non-empty palette) and a
    `BrandFolder` with no font_files (-> Be Vietnam fallback font set).
    Function-scoped because it depends on `tmp_path`.
    """
    from app.services.brand_folder_scanner import BrandFolder
    from app.services.brand_kit_extractor import KitSources, build_profile
    from app.services.gd_spec_builder import build_gd_spec

    profile = build_profile("Dyn Brand", KitSources(kit_pdf=kit_pdf), llm=None)
    folder = BrandFolder(brand_name="Dyn Brand", root=tmp_path, kit_pdf=kit_pdf)
    spec = build_gd_spec(profile, folder, brand_id="dyn-firestore-id")
    assert spec is not None
    return spec
