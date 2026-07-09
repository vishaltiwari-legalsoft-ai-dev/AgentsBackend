# backend/app/services/brand_kit_extractor.py
"""Extract exact brand colors + fonts from a brand-kit PDF.

Pure module: filesystem in, dataclasses out. No Firestore/GCS/HTTP imports —
the enrichment orchestrator (brand_enrichment.py) owns all I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF — existing backend dependency

_HEX_RE = re.compile(r"#([0-9A-Fa-f]{6})\b")
_BARE_HEX_RE = re.compile(r"\b([0-9A-Fa-f]{6})\b")
_HEX_WORD_RE = re.compile(r"\bhex\b", re.I)
_RGB_RE = re.compile(
    r"\bR\s*:?\s*(\d{1,3})\s*[,/ ]\s*G\s*:?\s*(\d{1,3})\s*[,/ ]\s*B\s*:?\s*(\d{1,3})", re.I
)


@dataclass
class ColorHit:
    hex: str      # "#1A2B3C" — normalized uppercase
    page: int     # 1-based
    context: str  # the text line it appeared on (feeds role labeling)


def extract_colors(pdf_path: Path) -> list[ColorHit]:
    hits: list[ColorHit] = []
    seen: set[str] = set()
    with fitz.open(pdf_path) as doc:
        for page_no, page in enumerate(doc, start=1):
            for line in page.get_text("text").splitlines():
                for m in _HEX_RE.finditer(line):
                    _add(hits, seen, m.group(1), page_no, line)
                if _HEX_WORD_RE.search(line):
                    for m in _BARE_HEX_RE.finditer(line):
                        _add(hits, seen, m.group(1), page_no, line)
                for m in _RGB_RE.finditer(line):
                    r, g, b = (min(int(v), 255) for v in m.groups())
                    _add(hits, seen, f"{r:02X}{g:02X}{b:02X}", page_no, line)
    return hits


def _add(hits: list[ColorHit], seen: set[str], raw: str, page_no: int, line: str) -> None:
    hx = f"#{raw.upper()}"
    if hx not in seen:
        seen.add(hx)
        hits.append(ColorHit(hex=hx, page=page_no, context=line.strip()))
