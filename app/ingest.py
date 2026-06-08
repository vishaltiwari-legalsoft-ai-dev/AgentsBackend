"""Brand Kits ingestion.

Walks the Brand Kits directory (the ONLY brand-asset source), treats each
top-level folder as a brand, uploads every file to GCS, and records metadata in
Firestore. The sibling "LS DESIGN PRODUCTIONS" folder is never touched.

Run with:  python -m app.ingest
"""

from __future__ import annotations

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
}


def _mime_for(path: Path) -> str:
    return MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")


def _brand_name(folder_name: str) -> str:
    cleaned = re.sub(r"\s*Brand Kit\s*$", "", folder_name, flags=re.IGNORECASE).strip()
    return cleaned or folder_name


def _ingest_brand(brand_folder: Path) -> None:
    brand_name = _brand_name(brand_folder.name)
    brand = firestore_repo.upsert_brand(brand_name, {"source_folder": brand_folder.name})
    print(f"\n[ingest] Brand: {brand_name} ({brand['id']})")

    # Make re-runs idempotent: wipe this brand's previously ingested creatives
    # before re-uploading. AI-generated assets (author=AgentOS) are preserved.
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
                brand["id"], relative, _mime_for(file_path), gs_uri,
                {"relative_path": relative, "author": "Marketing Team"},
            )
            uploaded += 1
            if uploaded % 25 == 0:
                print(f"[ingest]   ...{uploaded} files uploaded")
        except Exception as exc:  # noqa: BLE001 - skip a bad file, keep going
            failed += 1
            print(f"[ingest]   skipped '{relative}': {exc}")
    print(f"[ingest] Done: {uploaded} uploaded, {failed} skipped.")


def main() -> None:
    source = settings.brand_kits_dir or os.path.join(os.getcwd(), "Brand Kits")
    base = Path(source)
    print(f"[ingest] Source: {base}")
    if not base.is_dir():
        raise SystemExit(f"Brand Kits directory not found: {base}")

    brand_folders = sorted(p for p in base.iterdir() if p.is_dir())
    if not brand_folders:
        print("[ingest] No brand folders found.")
        return

    for folder in brand_folders:
        _ingest_brand(folder)
    print(f"\n[ingest] Ingested {len(brand_folders)} brands.")


if __name__ == "__main__":
    main()
