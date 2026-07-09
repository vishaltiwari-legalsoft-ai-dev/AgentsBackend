# backend/app/services/brand_enrichment.py
"""Orchestrates: scan folder -> extract profile -> merge-write Firestore
(+ upload fonts/logos to GCS).

The ONLY module in this feature allowed to touch Firestore/GCS. Extraction
and scanning stay pure so they test offline; this module wraps its calls
with a fake Firestore / fake `_upload_file` in tests, never the real thing.

Also implements the Amendment-A rung-5 static backfill (`backfill_static`)
for brands whose exact palette/fonts are already hardcoded in
`graphics_designer_agent.templated_brands` rather than re-extracted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.services import firestore_repo, storage
from app.services.brand_folder_scanner import BrandFolder, scan_root
from app.services.brand_kit_extractor import BrandKitProfile, KitSources, build_profile

# Images considered for the pixel-frequency rung (mirrors the folder scanner's
# ASSET_EXTS, minus the non-image formats it also sweeps up).
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

OWNED_KEYS = ("primary_colors", "secondary_colors", "accent_colors", "fonts",
              "tone_of_voice", "brand_kit_source", "enrichment")

# Content-type map for the font/logo files this module uploads to GCS.
_UPLOAD_MIME_BY_EXT = {
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


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


def _upload_file(local: Path, dest: str) -> str | None:
    """Upload `local` to GCS at object path `dest` (e.g.
    "brands/<id>/fonts/<file>"), delegating to storage's core `_upload`
    primitive — the same one every public storage.upload_* helper wraps; none
    of the existing public helpers match this feature's destination layout
    (brands/<brand_id>/fonts|logos/<file>), so this seam calls the shared
    primitive directly rather than duplicating its GCS/signing logic.
    Returns the `gs://` URI, or None when no bucket is configured — callers
    log that as a report note rather than raising."""
    if not storage.is_configured():
        return None
    content_type = _UPLOAD_MIME_BY_EXT.get(local.suffix.lower(), "application/octet-stream")
    gs_uri, _ = storage._upload(dest, local.read_bytes(), content_type)
    return gs_uri


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
            "font_fallback": not folder.font_files, "notes": []}

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

    # R2d live-write order: allocate the doc first (find-or-create), THEN
    # upload fonts/logos (R3), THEN a single update_brand_metadata carrying
    # everything — including the freshly-uploaded URIs.
    if existing:
        brand_id = existing["id"]
    else:
        created = firestore_repo.upsert_brand(folder.brand_name, {})
        brand_id = created["id"]

    font_uris: list[str] = []
    for f in folder.font_files:
        uri = _upload_file(f, f"brands/{brand_id}/fonts/{f.name}")
        if uri:
            font_uris.append(uri)
        else:
            base["notes"].append(f"no GCS bucket configured — skipped upload of {f.name}")

    logo_uris: list[str] = []
    for f in folder.logo_candidates:
        uri = _upload_file(f, f"brands/{brand_id}/logos/{f.name}")
        if uri:
            logo_uris.append(uri)
        else:
            base["notes"].append(f"no GCS bucket configured — skipped upload of {f.name}")

    if font_uris:
        patch["enrichment"]["font_files"] = font_uris
    if logo_uris:
        patch["enrichment"]["logo_files"] = logo_uris

    firestore_repo.update_brand_metadata(brand_id, patch)
    base |= {"brand_id": brand_id, "wrote": True}
    return base


def backfill_static(pack_id: str, *, dry_run: bool, now_iso: str) -> dict:
    """Amendment-A rung 5: build a patch straight from a
    `graphics_designer_agent.templated_brands` static spec's exact
    palette/fonts (no re-extraction — that spec already wins). Same
    report-entry shape as `_enrich_one` so the CLI can print/serialize both
    uniformly."""
    from graphics_designer_agent.templated_brands import SPECS

    spec = next((s for s in SPECS if s["id"] == pack_id), None)
    if spec is None:
        known = ", ".join(s["id"] for s in SPECS)
        raise ValueError(f"Unknown static brand pack id {pack_id!r} (known: {known})")

    firestore_brand_id = spec.get("firestore_brand_id")
    if not firestore_brand_id:
        raise ValueError(
            f"Static pack {pack_id!r} has no firestore_brand_id — cannot backfill.")

    palette = spec["palette"]
    patch = {
        "primary_colors": [palette["mid"], palette["deep"]],
        "secondary_colors": [palette["light"]],
        "accent_colors": [palette["accent"]],
        "fonts": [v["name"] for v in spec["font_variants"]],
        "brand_kit_source": f"static-spec:templated_brands/{spec['id']}",
        "enrichment": {"confidence": "high", "extracted_at": now_iso,
                       "palette": dict(palette), "source": "static_spec"},
    }
    base = {"brand_name": spec["name"], "brand_id": firestore_brand_id,
            "matched_existing": True, "wrote": False, "skipped_reason": None,
            "patch": patch, "confidence": "high", "font_fallback": False, "notes": []}
    if dry_run:
        return base

    firestore_repo.update_brand_metadata(firestore_brand_id, patch)
    base["wrote"] = True
    return base
