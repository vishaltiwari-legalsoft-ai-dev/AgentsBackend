# backend/app/services/brand_enrichment.py
"""Orchestrates: scan folder -> extract profile -> merge-write Firestore.

The ONLY module in this feature allowed to touch Firestore. Extraction and
scanning stay pure so they test offline; this module wraps their calls with
a fake Firestore in tests, never the real thing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.services import firestore_repo
from app.services.brand_folder_scanner import BrandFolder, scan_root
from app.services.brand_kit_extractor import BrandKitProfile, KitSources, build_profile

# Images considered for the pixel-frequency rung (mirrors the folder scanner's
# ASSET_EXTS, minus the non-image formats it also sweeps up).
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

OWNED_KEYS = ("primary_colors", "secondary_colors", "accent_colors", "fonts",
              "tone_of_voice", "brand_kit_source", "enrichment")


def _build_sources(folder: BrandFolder) -> KitSources:
    """KitSources per R2a: images are the union of logo_candidates + asset_files,
    filtered to real image extensions (duplicates between the two lists are
    fine — pixel counting is share-based)."""
    return KitSources(
        kit_pdf=folder.kit_pdf,
        svg_files=folder.svg_files,
        font_files=folder.font_files,
        image_files=[p for p in (folder.logo_candidates + folder.asset_files)
                     if p.suffix.lower() in IMAGE_EXTS],
    )


def profile_to_patch(profile: BrandKitProfile, folder: BrandFolder, now_iso: str) -> dict:
    """The exact brand_metadata patch (R2c patch hygiene): build every owned
    key, then drop top-level keys whose value is None or [] — except
    `brand_kit_source` (only ever added when folder.kit_pdf is set, so it is
    never empty when present) and `enrichment` (always written, even when its
    `palette` sub-value is {})."""
    sources = _build_sources(folder)
    patch: dict = {
        "primary_colors": profile.primary_colors,
        "secondary_colors": profile.secondary_colors,
        "accent_colors": profile.accent_colors,
        "fonts": [f"{f.family} {f.style}" for f in profile.fonts],
        "tone_of_voice": profile.tone_of_voice,
        "enrichment": {
            "confidence": profile.confidence,
            "extracted_at": now_iso,
            "palette": profile.palette,
            "pages_scanned": profile.provenance.get("pages_scanned", 0),
            "source_ladder": {
                "kit_pdf": sources.kit_pdf is not None,
                "svg": bool(sources.svg_files),
                "font_files": bool(sources.font_files),
                "pixel": bool(sources.image_files),
            },
        },
    }
    if folder.kit_pdf is not None:
        patch["brand_kit_source"] = str(folder.kit_pdf)

    return {
        k: v for k, v in patch.items()
        if k in ("enrichment", "brand_kit_source") or (v is not None and v != [])
    }


def enrich_root(root: Path, *, dry_run: bool = True,
                 llm: Callable[[str], str] | None = None,
                 now_iso: str) -> list[dict]:
    reports = []
    for folder in scan_root(root):
        reports.append(_enrich_one(folder, dry_run=dry_run, llm=llm, now_iso=now_iso))
    return reports


def _enrich_one(folder: BrandFolder, *, dry_run: bool, llm, now_iso: str) -> dict:
    base = {"brand_name": folder.brand_name, "brand_id": None,
            "matched_existing": False, "wrote": False, "skipped_reason": None,
            "patch": None, "confidence": None,
            "font_fallback": not folder.font_files}

    sources = _build_sources(folder)
    profile = build_profile(folder.brand_name, sources, llm=llm)
    # R2b skip rule: run build_profile whenever ANY source exists; skip only
    # when the built profile has no colors AND no fonts (this also covers the
    # "folder has no sources at all" case, since that trivially yields both
    # empty).
    if not profile.colors and not profile.fonts:
        return base | {"skipped_reason": "no extractable sources"}

    patch = profile_to_patch(profile, folder, now_iso)
    base |= {"patch": patch, "confidence": profile.confidence}

    existing = firestore_repo.find_brand_by_name(folder.brand_name)
    base["matched_existing"] = existing is not None
    if dry_run:
        return base

    # R2d live-write order: allocate the doc first (find-or-create), THEN a
    # single update_brand_metadata carrying everything.
    if existing:
        brand_id = existing["id"]
    else:
        created = firestore_repo.upsert_brand(folder.brand_name, {})
        brand_id = created["id"]

    firestore_repo.update_brand_metadata(brand_id, patch)
    base |= {"brand_id": brand_id, "wrote": True}
    return base
