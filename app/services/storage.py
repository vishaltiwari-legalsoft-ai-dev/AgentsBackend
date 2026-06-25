"""Google Cloud Storage access (the file "Vault").

Files live in GCS; Firestore stores only their URLs. The client is created
lazily, and `is_configured` lets callers fall back to inline data URLs when GCS
is not yet set up.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Optional

from google.auth import compute_engine, default as google_auth_default
from google.auth.transport import requests as google_auth_requests
from google.cloud import storage

from app.config import settings

_client: Optional[storage.Client] = None
_signing_credentials = None


def _storage() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client(project=settings.require("gcp_project_id"))
    return _client


def _signing_kwargs() -> dict:
    """Extra args so `generate_signed_url` works on Cloud Run.

    Cloud Run's attached service account authenticates with a token but has no
    local private key, so v4 signing must go through the IAM signBlob API. We
    detect that case and pass `service_account_email` + `access_token`. With a
    local JSON key (dev), no extra args are needed and signing happens locally.
    Requires the service account to have `roles/iam.serviceAccountTokenCreator`.
    """
    global _signing_credentials
    if _signing_credentials is None:
        _signing_credentials, _ = google_auth_default()
    creds = _signing_credentials
    if isinstance(creds, compute_engine.Credentials):
        if not creds.valid:
            creds.refresh(google_auth_requests.Request())
        return {
            "service_account_email": creds.service_account_email,
            "access_token": creds.token,
        }
    return {}


def is_configured() -> bool:
    return bool(settings.gcp_project_id and settings.gcs_bucket_name)


def download_bytes(gs_uri: str) -> bytes:
    """Download an object's bytes given its `gs://bucket/object` URI."""
    if not gs_uri.startswith("gs://"):
        raise ValueError(f"Not a gs:// URI: {gs_uri}")
    bucket_name, _, object_path = gs_uri[len("gs://"):].partition("/")
    if not bucket_name or not object_path:
        raise ValueError(f"Malformed gs:// URI: {gs_uri}")
    return _storage().bucket(bucket_name).blob(object_path).download_as_bytes()


def _safe_name(file_name: str) -> str:
    return re.sub(r"[^\w.\-() ]", "_", file_name)


def _upload(object_path: str, data: bytes, content_type: str) -> tuple[str, str]:
    """Upload bytes and return (gs_uri, signed_url)."""
    bucket_name = settings.require("gcs_bucket_name")
    try:
        blob = _storage().bucket(bucket_name).blob(object_path)
        blob.upload_from_string(data, content_type=content_type)
        signed_url = blob.generate_signed_url(
            version="v4", expiration=timedelta(hours=1), method="GET", **_signing_kwargs()
        )
        return f"gs://{bucket_name}/{object_path}", signed_url
    except Exception as exc:  # noqa: BLE001 - surface storage errors with context
        raise RuntimeError(f'GCS upload failed for "{object_path}": {exc}') from exc


def delete_all_brand_kit_blobs() -> int:
    """Delete every ingested brand-kit object (`<brand_id>/creatives/...`).

    Leaves `generated/` and `references/` untouched.
    """
    bucket_name = settings.require("gcs_bucket_name")
    bucket = _storage().bucket(bucket_name)
    deleted = 0
    for blob in bucket.list_blobs():
        parts = blob.name.split("/", 2)
        if len(parts) >= 2 and parts[1] == "creatives" and parts[0] not in (
            "generated",
            "references",
        ):
            blob.delete()
            deleted += 1
    return deleted


def upload_creative(
    brand_id: str, file_name: str, data: bytes, content_type: str
) -> tuple[str, str]:
    """Store a brand creative at `<brand_id>/creatives/<file_name>`."""
    return _upload(f"{brand_id}/creatives/{_safe_name(file_name)}", data, content_type)


def upload_reference(
    user_id: str, file_name: str, data: bytes, content_type: str
) -> tuple[str, str]:
    """Store a user reference file at `references/<user_id>/<file_name>`."""
    return _upload(f"references/{user_id}/{_safe_name(file_name)}", data, content_type)


def upload_generated(
    partition: str, file_name: str, data: bytes, content_type: str
) -> tuple[str, str]:
    """Store an AI-generated asset at `generated/<partition>/<file_name>`.

    Intentionally kept OUTSIDE the brand-kit GCS namespace and never written
    to Firestore so the agent's retrieval pipeline can never pull its own
    prior outputs as "brand samples" (which would cause model drift).
    """
    return _upload(
        f"generated/{partition}/{_safe_name(file_name)}", data, content_type
    )


# Cloud Storage namespace for the Brand Reference Library (Drive-synced
# precedent + its index). Kept separate from brand kits, references and
# generated output so it is never confused with — or pruned alongside — them.
REFERENCE_LIBRARY_PREFIX = "reference_library"
REFERENCE_INDEX_OBJECT = f"{REFERENCE_LIBRARY_PREFIX}/reference_index.json"


def upload_reference_library_asset(
    brand_id: str, creative_type: str, file_name: str, data: bytes, content_type: str
) -> tuple[str, str]:
    """Mirror a reference-library asset to
    ``reference_library/<brand_id>/<creative_type>/<file>`` and return
    ``(gs_uri, signed_url)``."""
    object_path = (
        f"{REFERENCE_LIBRARY_PREFIX}/{brand_id}/{creative_type}/{_safe_name(file_name)}"
    )
    return _upload(object_path, data, content_type)


def write_reference_index(data: bytes) -> str:
    """Persist the reference index JSON to GCS; returns its ``gs://`` URI."""
    gs_uri, _ = _upload(REFERENCE_INDEX_OBJECT, data, "application/json")
    return gs_uri


def read_reference_index() -> Optional[bytes]:
    """Read the reference index JSON from GCS, or ``None`` if it does not exist."""
    bucket_name = settings.require("gcs_bucket_name")
    blob = _storage().bucket(bucket_name).blob(REFERENCE_INDEX_OBJECT)
    if not blob.exists():
        return None
    return blob.download_as_bytes()


def signed_url_for_gs_uri(gs_uri: str, expires_in_hours: int = 1) -> str:
    """Convert a `gs://bucket/object` URI into a time-limited HTTPS view URL."""
    if not gs_uri.startswith("gs://"):
        raise ValueError(f"Not a gs:// URI: {gs_uri}")
    without_scheme = gs_uri[len("gs://"):]
    bucket_name, _, object_path = without_scheme.partition("/")
    if not bucket_name or not object_path:
        raise ValueError(f"Malformed gs:// URI: {gs_uri}")
    blob = _storage().bucket(bucket_name).blob(object_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(hours=expires_in_hours),
        method="GET",
        **_signing_kwargs(),
    )


# Browser-renderable image formats (the only ones safe to put in <img>).
_RENDERABLE_IMAGE_MIMES = frozenset({
    "image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml",
})
_RENDERABLE_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
})


def _is_browser_renderable(file_name: str, file_type: str) -> bool:
    """True only if a browser can put this asset inside an <img> tag.

    Checks BOTH MIME and extension so files ingested before we added a MIME
    mapping (and thus saved as `application/octet-stream`) are still detected
    by their extension. Anything not confirmed renderable (EXR, PSD, PDF,
    fonts, video, etc.) is shown as a typed card on the frontend instead.
    """
    if file_type in _RENDERABLE_IMAGE_MIMES:
        return True
    lower = file_name.lower()
    return any(lower.endswith(ext) for ext in _RENDERABLE_IMAGE_EXTS)


def to_gallery(creatives: list[dict], limit: int) -> list[dict]:
    """Build a UI-ready gallery from raw creative records.

    Each returned item carries an `is_image` flag so the frontend can choose
    between a thumbnail and a typed-asset card. Renderable images are
    surfaced first so the gallery is visually rich at a glance.
    """
    items: list[dict] = []
    sorted_creatives = sorted(
        creatives,
        key=lambda c: 0 if _is_browser_renderable(
            c.get("file_name", ""), c.get("file_type", "")
        ) else 1,
    )
    for c in sorted_creatives:
        if len(items) >= limit:
            break
        gs_uri = c.get("file_url", "")
        if not isinstance(gs_uri, str) or not gs_uri.startswith("gs://"):
            continue
        try:
            view_url = signed_url_for_gs_uri(gs_uri)
        except Exception:  # noqa: BLE001 - skip the bad asset, keep going
            continue
        file_name = c.get("file_name", "")
        file_type = c.get("file_type", "application/octet-stream")
        items.append({
            "file_name": file_name,
            "file_type": file_type,
            "view_url": view_url,
            "gs_uri": gs_uri,  # source path, for re-signing from chat history
            "is_image": _is_browser_renderable(file_name, file_type),
        })
    return items


def rehydrate_result(result: dict) -> dict:
    """Re-sign any `gs_uri` fields in a stored agent result so a resumed chat
    renders even after the original signed URLs have expired.
    """
    if not isinstance(result, dict) or not is_configured():
        return result

    def _sign(gs_uri: str | None) -> str | None:
        if not gs_uri:
            return None
        try:
            return signed_url_for_gs_uri(gs_uri)
        except Exception:  # noqa: BLE001 - leave stale url if signing fails
            return None

    assets = result.get("assets")
    if isinstance(assets, dict):
        for variation in assets.values():
            fresh = _sign(variation.get("gs_uri")) if isinstance(variation, dict) else None
            if fresh:
                variation["url"] = fresh

    logo = result.get("logo")
    if isinstance(logo, dict):
        fresh = _sign(logo.get("gs_uri"))
        if fresh:
            logo["view_url"] = fresh

    gallery = result.get("gallery")
    if isinstance(gallery, list):
        for item in gallery:
            fresh = _sign(item.get("gs_uri")) if isinstance(item, dict) else None
            if fresh:
                item["view_url"] = fresh

    return result
