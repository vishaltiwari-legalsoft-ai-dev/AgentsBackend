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
