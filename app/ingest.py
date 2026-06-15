"""Brand Kits ingestion.

Walks a brand-asset directory, treats each top-level folder as a brand (or
detects a nested `Brand Kit` subfolder), uploads every file to GCS, and records
metadata in Firestore.

Run with:
    python -m app.ingest              # ingest / update from BRAND_KITS_DIR
    python -m app.ingest --reset      # wipe brands + creatives + GCS kit files, then re-ingest
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from app.config import settings
from app.services import firestore_repo, storage

MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".mp4": "video/mp4",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ai": "application/postscript",
    ".eps": "application/postscript",
}


def _mime_for(path: Path) -> str:
    return MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")


def _brand_name(folder_name: str) -> str:
    cleaned = re.sub(r"\s*Brand Kit\s*$", "", folder_name, flags=re.IGNORECASE).strip()
    return cleaned or folder_name


def _discover_brands(base: Path) -> list[tuple[str, Path]]:
    """Return (brand_name, kit_folder) pairs from a data directory.

    Handles three layouts seen under `Data/`:
    - `{Brand}/Brand Kit/...`          e.g. MedVirtual/Brand Kit/
    - `{Name} brand kit/...`           e.g. Legal soft brand kit/ (kit at top level)
    - `{Brand}/...`                    fallback: entire folder is the kit
    """
    results: list[tuple[str, Path]] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue

        if "brand kit" in entry.name.lower():
            results.append((_brand_name(entry.name), entry))
            continue

        kit_sub = entry / "Brand Kit"
        if kit_sub.is_dir():
            results.append((_brand_name(entry.name), kit_sub))
            continue

        results.append((_brand_name(entry.name), entry))

    return results


def _ingest_brand(brand_name: str, brand_folder: Path) -> None:
    brand = firestore_repo.upsert_brand(
        brand_name,
        {"source_folder": brand_folder.name, "source_path": str(brand_folder)},
    )
    print(f"\n[ingest] Brand: {brand_name} ({brand['id']})")

    pruned = firestore_repo.delete_ingested_creatives(brand["id"])
    if pruned:
        print(f"[ingest]   pruned {pruned} previously-ingested records")

    uploaded = failed = 0
    for file_path in brand_folder.rglob("*"):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(brand_folder).as_posix()
        try:
            data = file_path.read_bytes()
            flat_name = relative.replace("/", "__")
            gs_uri, _ = storage.upload_creative(
                brand["id"], flat_name, data, _mime_for(file_path)
            )
            firestore_repo.create_creative(
                brand["id"],
                relative,
                _mime_for(file_path),
                gs_uri,
                {"relative_path": relative, "author": "Marketing Team"},
            )
            uploaded += 1
            if uploaded % 25 == 0:
                print(f"[ingest]   ...{uploaded} files uploaded")
        except Exception as exc:  # noqa: BLE001 - skip a bad file, keep going
            failed += 1
            print(f"[ingest]   skipped '{relative}': {exc}")
    print(f"[ingest] Done: {uploaded} uploaded, {failed} skipped.")


def _reset_all() -> None:
    """Remove all brand-kit data from Firestore and GCS (keeps users/chats)."""
    print("[reset] Deleting GCS brand-kit blobs...")
    gcs_deleted = storage.delete_all_brand_kit_blobs()
    print(f"[reset]   removed {gcs_deleted} GCS objects")

    print("[reset] Deleting Firestore creatives...")
    creatives_deleted = firestore_repo.delete_all_creatives()
    print(f"[reset]   removed {creatives_deleted} creative records")

    print("[reset] Deleting Firestore brands...")
    brands_deleted = firestore_repo.delete_all_brands()
    print(f"[reset]   removed {brands_deleted} brand records")
    print("[reset] Done.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest brand kits into GCS + Firestore")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe all brands/creatives/GCS kit files before re-ingesting",
    )
    args = parser.parse_args()

    source = settings.brand_kits_dir or os.path.join(os.getcwd(), "Brand Kits")
    base = Path(source)
    print(f"[ingest] Source: {base}")
    if not base.is_dir():
        raise SystemExit(f"Brand Kits directory not found: {base}")

    if args.reset:
        _reset_all()

    brands = _discover_brands(base)
    if not brands:
        print("[ingest] No brand folders found.")
        return

    for brand_name, kit_folder in brands:
        print(f"[ingest] Kit folder: {kit_folder}")
        _ingest_brand(brand_name, kit_folder)

    print(f"\n[ingest] Ingested {len(brands)} brands.")


if __name__ == "__main__":
    main()
