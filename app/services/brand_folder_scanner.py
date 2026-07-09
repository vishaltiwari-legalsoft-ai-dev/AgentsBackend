# backend/app/services/brand_folder_scanner.py
"""Scan a per-brand content folder tree. Pure: filesystem in, dataclasses out.

Layout contract (validated in Stage 0 against the real Downloads folder):
    <root>/<Brand Name>/...(anywhere)/<Brand Kit dir>/<kit>.pdf
Name hints below are the single tuning point if real folder naming differs.

Kit-PDF rule (Stage 0 incident fix): a PDF can NEVER qualify as the brand's
kit PDF by filename alone. It must sit under a directory (anywhere between
the brand root and the file) whose lowercase name contains one of
KIT_DIR_HINTS as a substring. Without that, no amount of "brand"/"kit"/
"guide" in the filename matters — this is what stopped a third-party
`MGMA-Media-Kit-2026.pdf` sitting under `Potential Partners/` from being
picked up as a brand's own kit. Among directory-qualified candidates, rank
by count of KIT_PDF_HINTS name hits, then tie-break by file size (larger
wins).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

KIT_DIR_HINTS = ("brand kit", "brandkit", "brand-kit", "brand_kit")
KIT_PDF_HINTS = ("brand", "kit", "guide", "identity", "style")
FONT_EXTS = {".ttf", ".otf"}
LOGO_HINTS = ("logo",)
ASSET_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".pdf", ".pptx"}
SVG_EXT = ".svg"


@dataclass
class BrandFolder:
    brand_name: str
    root: Path
    kit_pdf: Path | None
    font_files: list[Path] = field(default_factory=list)
    logo_candidates: list[Path] = field(default_factory=list)
    asset_files: list[Path] = field(default_factory=list)
    svg_files: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def scan_root(root: Path) -> list[BrandFolder]:
    return [_scan_brand(d) for d in sorted(root.iterdir()) if d.is_dir()]


def _scan_brand(brand_dir: Path) -> BrandFolder:
    files = [p for p in brand_dir.rglob("*") if p.is_file()]
    kit_pdf = _find_kit_pdf(files, brand_dir)
    folder = BrandFolder(
        brand_name=brand_dir.name,
        root=brand_dir,
        kit_pdf=kit_pdf,
        font_files=[p for p in files if p.suffix.lower() in FONT_EXTS],
        logo_candidates=[p for p in files if p.suffix.lower() in ASSET_EXTS
                         and any(h in p.name.lower() for h in LOGO_HINTS)],
        asset_files=[p for p in files if p.suffix.lower() in ASSET_EXTS and p != kit_pdf],
        svg_files=[p for p in files if p.suffix.lower() == SVG_EXT],
    )
    if kit_pdf is None:
        folder.notes.append("no brand-kit pdf found")
    return folder


def _find_kit_pdf(files: list[Path], brand_dir: Path) -> Path | None:
    candidates = [
        p for p in files
        if p.suffix.lower() == ".pdf" and _under_kit_dir(p, brand_dir)
    ]
    if not candidates:
        return None

    def score(p: Path) -> tuple[int, int]:
        name_hits = sum(h in p.name.lower() for h in KIT_PDF_HINTS)
        return (name_hits, p.stat().st_size)

    return max(candidates, key=score)


def _under_kit_dir(pdf_path: Path, brand_dir: Path) -> bool:
    """True if any directory between brand_dir and pdf_path's parent has a
    lowercase name containing one of KIT_DIR_HINTS as a substring."""
    rel_dir_parts = pdf_path.relative_to(brand_dir).parts[:-1]
    return any(
        any(hint in part.lower() for hint in KIT_DIR_HINTS)
        for part in rel_dir_parts
    )
