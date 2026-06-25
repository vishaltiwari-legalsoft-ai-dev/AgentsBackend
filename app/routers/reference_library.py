"""Brand Reference Library API — ingest + retrieval.

Exposes the reference-library rail (``graphics_designer_agent.reference_library``)
over HTTP so the team can build the index from real, type-segregated creatives
and so the agent can retrieve on-brand precedent for a job.

Endpoints (all under ``/api/ref-library``):
- ``GET  /ref-library/types``       creative-type taxonomy + format rules
- ``POST /ref-library/ingest``      (admin) understand the asset folder -> index
- ``POST /ref-library/sync-drive``  (admin) pull the shared Google Drive folder
                                    of references -> understand -> mirror to GCS
                                    -> index
- ``GET  /ref-library``             list indexed references (filter by brand/type)
- ``GET  /ref-library/retrieve``    rank references for a brand+type+brief job

The index is a local JSON file by default (no GCP needed), and is mirrored to
Cloud Storage when a bucket is configured so it survives Cloud Run restarts —
the record shape is storage-agnostic either way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse

from app.config import settings
from app.security import get_current_user, require_admin

# On sys.path via app.__init__ (agent root registered there).
from graphics_designer_agent import reference_library as rl

router = APIRouter()


def _base_dir() -> Path:
    """Where reference assets + the index live (shared with the pipeline).

    Delegates to ``reference_library.default_base_dir`` so the API and the
    generator always read/write the same index (``GD_REFERENCE_DIR`` override,
    else the repo's ``Data/_reference_mock``).
    """
    return rl.default_base_dir()


def _public(record: dict[str, Any]) -> dict[str, Any]:
    """Drop the server-local filesystem path from API responses."""
    return {k: v for k, v in record.items() if k != "abs_path"}


@router.get("/ref-library/types")
def list_types(_user: dict = Depends(get_current_user)) -> dict:
    """The creative-type taxonomy with each type's format rules."""
    return {
        "types": [
            {"key": key, **spec} for key, spec in rl.CREATIVE_TYPES.items()
        ]
    }


@router.post("/ref-library/ingest")
def ingest(
    use_llm: bool = Query(default=False, description="Refine tags/summary via the LLM if available"),
    _admin: dict = Depends(require_admin),
) -> dict:
    """Walk the asset folder, understand every creative, and (re)write the index."""
    base = _base_dir()
    if not base.is_dir():
        raise HTTPException(404, f"Reference directory not found: {base}")
    try:
        records = rl.ingest_all(base, use_llm=use_llm)
    except Exception as exc:  # noqa: BLE001 - surface ingest errors to the admin
        raise HTTPException(500, f"Ingestion failed: {exc}") from exc
    rl.write_index(base, records)

    by_brand: dict[str, dict[str, int]] = {}
    for r in records:
        by_brand.setdefault(r.brand_name, {}).setdefault(r.creative_type, 0)
        by_brand[r.brand_name][r.creative_type] += 1
    return {
        "ingested": len(records),
        "source": "agent+llm" if use_llm else "deterministic",
        "by_brand": by_brand,
    }


@router.post("/ref-library/sync-drive")
def sync_drive(
    use_llm: bool = Query(default=False, description="Refine tags/summary via the LLM if available"),
    folder_id: Optional[str] = Query(default=None, description="Override the configured Drive folder id"),
    _admin: dict = Depends(require_admin),
) -> dict:
    """Pull reference creatives from the shared Google Drive folder into the library.

    Downloads the folder (shared with the service account) to a temp dir laid out
    as ``<Brand>/<creative_type>/<file>``, understands + indexes every asset,
    mirrors the bytes to Cloud Storage (when configured) so the index survives
    Cloud Run restarts, and writes the index. Admin/creator only.
    """
    import shutil

    from app.services import drive_source

    fid = folder_id or settings.gd_drive_folder_id
    if not fid:
        raise HTTPException(400, "No Drive folder configured (set GD_DRIVE_FOLDER_ID).")

    # Download into the durable reference dir (NOT a temp dir) so local/no-GCS
    # deployments keep the files + a valid abs_path after the request ends. The
    # brand's prior subtree is cleared first so a re-sync fully replaces it.
    base = _base_dir()
    brand_dir = base / drive_source._safe_segment(settings.gd_drive_brand_name)
    if brand_dir.exists():
        shutil.rmtree(brand_dir, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)

    try:
        summary = drive_source.download_folder(
            fid, base, brand_name=settings.gd_drive_brand_name
        )
    except Exception as exc:  # noqa: BLE001 - surface Drive/auth errors to admin
        raise HTTPException(
            502,
            f"Google Drive sync failed: {exc}. Check that the Drive API is "
            f"enabled and the folder is shared with the service account.",
        ) from exc

    if summary["downloaded"] == 0:
        raise HTTPException(
            404,
            "No assets downloaded — is the folder shared with the service "
            f"account and laid out in mapped subfolders? Skipped: {summary['skipped_folders']}",
        )

    records = rl.ingest_all(base, use_llm=use_llm)
    mirrored = rl.mirror_to_gcs(records)  # stamps gs_uri when GCS is configured
    rl.write_index(base, records)  # local + GCS (handled inside write_index)

    by_type: dict[str, int] = {}
    for r in records:
        by_type[r.creative_type] = by_type.get(r.creative_type, 0) + 1
    return {
        "source": "google-drive",
        "folder_id": fid,
        "downloaded": summary["downloaded"],
        "ingested": len(records),
        "mirrored_to_gcs": mirrored,
        "by_type": by_type,
        "skipped_folders": summary["skipped_folders"],
    }


@router.get("/ref-library")
def list_references(
    brand: Optional[str] = Query(default=None),
    type: Optional[str] = Query(default=None, alias="type"),
    _user: dict = Depends(get_current_user),
) -> dict:
    """List indexed reference creatives, optionally filtered by brand and type."""
    records = rl.load_index(_base_dir())
    if brand:
        bslug = rl.brand_slug(brand)
        records = [r for r in records if r.get("brand_id") == bslug]
    if type:
        records = [r for r in records if r.get("creative_type") == type]
    return {"count": len(records), "references": [_public(r) for r in records]}


@router.get("/ref-library/asset/{record_id}")
def asset(record_id: str, _user: dict = Depends(get_current_user)):
    """Serve one reference image (for thumbnails in the UI).

    GCS-backed records (synced from Drive) redirect to a short-lived signed URL.
    Local records are served from disk, with the path validated to live *inside*
    the reference base dir so a tampered/legacy record can never read arbitrary
    files off disk.
    """
    base = _base_dir().resolve()
    for record in rl.load_index(_base_dir()):
        if record.get("id") != record_id:
            continue
        gs_uri = record.get("gs_uri")
        if gs_uri:
            from app.services import storage

            try:
                return RedirectResponse(storage.signed_url_for_gs_uri(gs_uri))
            except Exception as exc:  # noqa: BLE001 - fall through to local copy
                raise HTTPException(502, f"Could not sign asset URL: {exc}") from exc
        path = Path(record.get("abs_path", ""))
        try:
            path.resolve().relative_to(base)  # path-traversal guard
        except ValueError:
            raise HTTPException(403, "Asset is outside the reference directory")
        if not path.is_file():
            raise HTTPException(404, "Asset file is missing on disk")
        return FileResponse(path)
    raise HTTPException(404, "Unknown reference id")


@router.get("/ref-library/retrieve")
def retrieve(
    brief: str = Query(default="", description="Free-text creative brief"),
    brand: Optional[str] = Query(default=None),
    type: Optional[str] = Query(default=None, alias="type"),
    k: int = Query(default=3, ge=1, le=20),
    _user: dict = Depends(get_current_user),
) -> dict:
    """Rank reference creatives for a job and return them + a prompt-ready block."""
    if type and not rl.is_known_type(type):
        raise HTTPException(400, f"Unknown creative type: {type}")
    records = rl.load_index(_base_dir())
    if not records:
        raise HTTPException(409, "Reference index is empty — POST /ref-library/ingest first.")
    hits = rl.retrieve(records, creative_type=type, brief=brief, brand_id=brand, k=k)
    return {
        "count": len(hits),
        "results": [_public(r) for r in hits],
        "prompt_block": rl.summarize_for_prompt(hits),
    }
