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
