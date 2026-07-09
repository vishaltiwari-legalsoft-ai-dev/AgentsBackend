# backend/app/services/brand_enrichment.py
"""Orchestrates: scan folder -> extract profile -> merge-write Firestore
(+ upload fonts/logos to GCS).

The ONLY module in this feature allowed to touch Firestore/GCS. Extraction
and scanning stay pure so they test offline; this module wraps its calls
with a fake Firestore / fake upload seam in tests, never the real thing.
Dry-run performs ZERO Firestore/GCS calls — not even reads.

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


def _source_ladder(profile: BrandKitProfile, kit_pdf_present: bool) -> dict:
    """Which rungs CONTRIBUTED to the merged profile — not merely which had
    material present. Uses Unit A's context conventions: a ColorHit whose
    context starts "svg:" came from the SVG rung, "pixel-share=" from the
    pixel rung, and any other context can only be a kit-PDF text line
    (guarded on the PDF actually being present); a FontHit whose raw_name is
    a .ttf/.otf file name came from the font-file rung (PDF-embedded fonts
    carry basefont names like "ABCDEF+Family-Style" instead). A rung whose
    every hit was deduped out by a higher-priority rung reads False."""
    contexts = [c.context for c in profile.colors]
    return {
        "kit_pdf": kit_pdf_present and any(
            not c.startswith("svg:") and not c.startswith("pixel-share=")
            for c in contexts),
        "svg": any(c.startswith("svg:") for c in contexts),
        "font_files": any(f.raw_name.lower().endswith((".ttf", ".otf"))
                          for f in profile.fonts),
        "pixel": any(c.startswith("pixel-share=") for c in contexts),
    }


def profile_to_patch(profile: BrandKitProfile, folder: BrandFolder, now_iso: str) -> dict:
    """The exact brand_metadata patch (R2c patch hygiene): build every owned
    key, then drop top-level keys whose value is None or [] — except
    `brand_kit_source` (only ever added when folder.kit_pdf is set, so it is
    never empty when present) and `enrichment` (always written, even when its
    `palette` sub-value is {})."""
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
            "source_ladder": _source_ladder(profile, folder.kit_pdf is not None),
        },
    }
    if folder.kit_pdf is not None:
        patch["brand_kit_source"] = str(folder.kit_pdf)

    return {
        k: v for k, v in patch.items()
        if k in ("enrichment", "brand_kit_source") or (v is not None and v != [])
    }


def _upload_file(local: Path, dest: str) -> str | None:
    """Upload `local` to GCS at object path `dest`
    ("brands/<brand_id>/<kind>/<file>"), delegating to the public
    `storage.upload_brand_asset` helper with a content type derived from the
    file extension. Returns the `gs://` URI, or None when no bucket is
    configured — callers log that as a report note rather than raising.
    Upload errors propagate; `_upload_batch` contains them per-file."""
    if not storage.is_configured():
        return None
    _, brand_id, kind, filename = dest.split("/", 3)
    content_type = _UPLOAD_MIME_BY_EXT.get(local.suffix.lower(), "application/octet-stream")
    return storage.upload_brand_asset(brand_id, kind, filename, local.read_bytes(),
                                      content_type)


def _upload_batch(files: list[Path], brand_id: str, kind: str, notes: list[str]) -> list[str]:
    """Upload a brand's font/logo files, containing failures per-file: one
    flaky upload appends a note and moves on — it never aborts the rest of
    the batch (or the brand's Firestore write)."""
    uris: list[str] = []
    for f in files:
        try:
            uri = _upload_file(f, f"brands/{brand_id}/{kind}/{f.name}")
        except Exception as exc:  # noqa: BLE001 - per-file containment by design
            notes.append(f"upload failed: {f.name}: {exc}")
            continue
        if uri:
            uris.append(uri)
        else:
            notes.append(f"no GCS bucket configured — skipped upload of {f.name}")
    return uris


def enrich_root(root: Path, *, dry_run: bool = True,
                 llm: Callable[[str], str] | None = None,
                 now_iso: str) -> list[dict]:
    reports = []
    for folder in scan_root(root):
        reports.append(_enrich_one(folder, dry_run=dry_run, llm=llm, now_iso=now_iso))
    return reports


def _enrich_one(folder: BrandFolder, *, dry_run: bool, llm, now_iso: str) -> dict:
    # matched_existing starts as None = "not checked": dry-run performs ZERO
    # Firestore calls, including the name lookup, so it can never know.
    base = {"brand_name": folder.brand_name, "brand_id": None,
            "matched_existing": None, "wrote": False, "skipped_reason": None,
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

    if dry_run:
        return base

    existing = firestore_repo.find_brand_by_name(folder.brand_name)
    base["matched_existing"] = existing is not None

    # R2d live-write order: allocate the doc first (find-or-create), THEN
    # upload fonts/logos (R3), THEN a single update_brand_metadata carrying
    # everything — including the freshly-uploaded URIs.
    if existing:
        brand_id = existing["id"]
    else:
        created = firestore_repo.upsert_brand(folder.brand_name, {})
        brand_id = created["id"]

    font_uris = _upload_batch(folder.font_files, brand_id, "fonts", base["notes"])
    logo_uris = _upload_batch(folder.logo_candidates, brand_id, "logos", base["notes"])
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
