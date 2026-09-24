"""Google Cloud Storage access (the file "Vault").

Files live in GCS; Firestore stores only their URLs. The client is created
lazily, and `is_configured` lets callers fall back to inline data URLs when GCS
is not yet set up.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Optional

from google.auth import compute_engine, default as google_auth_default
from google.auth.transport import requests as google_auth_requests
from google.cloud import storage

from app.config import settings

_client: Optional[storage.Client] = None
_signing_credentials = None

# --- Deadlines -------------------------------------------------------------
# These run in sync handlers on anyio's 40-slot worker threadpool, so a stalled
# object call costs the whole service a slot. google-cloud-storage does default
# to 60s per call (with a 120s retry deadline), so this is not an unbounded
# hang — but the numbers are inherited and invisible. Stated here they can be
# reviewed: metadata calls are small and should be quick, payload transfers get
# the room they actually need.
_METADATA_TIMEOUT_SECONDS = 15   # exists / delete / list — small round trips
_TRANSFER_TIMEOUT_SECONDS = 60   # upload / download — real bytes on the wire
# Token refresh for IAM-based URL signing on Cloud Run. google-auth's own
# default is 120s for what is a metadata-server call.
_AUTH_TIMEOUT_SECONDS = 10


class _TimedRequest(google_auth_requests.Request):
    """google-auth transport with our deadline instead of its 120s default."""

    def __call__(  # type: ignore[override]
        self, url, method="GET", body=None, headers=None,
        timeout=_AUTH_TIMEOUT_SECONDS, **kwargs,
    ):
        return super().__call__(
            url, method=method, body=body, headers=headers, timeout=timeout, **kwargs
        )


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
            creds.refresh(_TimedRequest())
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
    return (
        _storage()
        .bucket(bucket_name)
        .blob(object_path)
        .download_as_bytes(timeout=_TRANSFER_TIMEOUT_SECONDS)
    )


def _safe_name(file_name: str) -> str:
    return re.sub(r"[^\w.\-() ]", "_", file_name)


def _upload(object_path: str, data: bytes, content_type: str) -> tuple[str, str]:
    """Upload bytes and return (gs_uri, signed_url)."""
    bucket_name = settings.require("gcs_bucket_name")
    try:
        blob = _storage().bucket(bucket_name).blob(object_path)
        blob.upload_from_string(
            data, content_type=content_type, timeout=_TRANSFER_TIMEOUT_SECONDS
        )
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
    # No cap on the iteration on purpose: this must delete *everything* matching
    # or its return count is a lie. The per-page/per-delete deadlines bound each
    # round trip; the total is bounded by how much there is to delete.
    for blob in bucket.list_blobs(timeout=_METADATA_TIMEOUT_SECONDS):
        parts = blob.name.split("/", 2)
        if len(parts) >= 2 and parts[1] == "creatives" and parts[0] not in (
            "generated",
            "references",
        ):
            blob.delete(timeout=_METADATA_TIMEOUT_SECONDS)
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


def upload_brand_asset(
    brand_id: str,
    kind: str,
    filename: str,
    data: bytes,
    content_type: str | None = None,
) -> str:
    """Store a brand-kit asset at `brands/<brand_id>/<kind>/<filename>` (kind
    is "fonts" or "logos") and return its `gs://` URI.

    Unlike `_upload`, no signed view URL is generated — brand enrichment
    persists durable `gs://` URIs in Firestore, not time-limited links.
    """
    bucket_name = settings.require("gcs_bucket_name")
    object_path = f"brands/{brand_id}/{kind}/{_safe_name(filename)}"
    try:
        blob = _storage().bucket(bucket_name).blob(object_path)
        blob.upload_from_string(
            data,
            content_type=content_type or "application/octet-stream",
            timeout=_TRANSFER_TIMEOUT_SECONDS,
        )
        return f"gs://{bucket_name}/{object_path}"
    except Exception as exc:  # noqa: BLE001 - surface storage errors with context
        raise RuntimeError(f'GCS upload failed for "{object_path}": {exc}') from exc


# --- Self-serve brand kit + reference uploads --------------------------------
# Object names are the first 16 hex chars of the content's SHA-256, never the
# uploaded file name: a user-supplied name is untrusted input (path tricks,
# collisions between two people's "logo.png"), and a content hash makes a
# re-upload of the same bytes land on the same object — idempotent by
# construction, so a retried request can never mint a second copy.

#: Allowed extensions per asset kind. Anything else is refused before any
#: bytes move — the upload router surfaces the ValueError as a 4xx.
BRAND_ASSET_EXTS: dict[str, frozenset[str]] = {
    "logo": frozenset({"png", "svg", "jpg", "jpeg", "webp"}),
    "font": frozenset({"ttf", "otf", "woff", "woff2"}),
    "guidelines": frozenset({"pdf", "png", "jpg", "jpeg", "webp"}),
}
REFERENCE_EXTS = frozenset({"png", "jpg", "jpeg", "webp", "gif", "pdf"})

_CONTENT_TYPE_BY_EXT = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "webp": "image/webp", "gif": "image/gif", "svg": "image/svg+xml",
    "pdf": "application/pdf", "ttf": "font/ttf", "otf": "font/otf",
    "woff": "font/woff", "woff2": "font/woff2",
}


def content_hash(data: bytes) -> str:
    """The 16-hex-char content key used in every self-serve object name."""
    return hashlib.sha256(data).hexdigest()[:16]


def _clean_ext(ext: str, allowed: frozenset[str]) -> str:
    cleaned = (ext or "").strip().lower().lstrip(".")
    if cleaned not in allowed:
        raise ValueError(
            f"file type {ext!r} is not allowed here (allowed: {', '.join(sorted(allowed))})")
    return cleaned


def content_type_for_ext(ext: str) -> str:
    return _CONTENT_TYPE_BY_EXT.get((ext or "").lower().lstrip("."), "application/octet-stream")


def _put_object(object_path: str, data: bytes, content_type: str) -> str:
    """Upload bytes and return the durable ``gs://`` URI (no signed URL —
    Firestore stores durable URIs, never time-limited links). Raises on a
    missing bucket or a failed upload: a self-serve upload that did not land
    must never be recorded as if it had."""
    if not data:
        raise ValueError("refusing to store an empty file")
    if not is_configured():
        # Also what keeps these writers behind the suite's GCS guard.
        raise RuntimeError("Cloud Storage is not configured — cannot store the upload")
    bucket_name = settings.require("gcs_bucket_name")
    try:
        blob = _storage().bucket(bucket_name).blob(object_path)
        blob.upload_from_string(
            data, content_type=content_type, timeout=_TRANSFER_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - surface storage errors with context
        raise RuntimeError(f'GCS upload failed for "{object_path}": {exc}') from exc
    return f"gs://{bucket_name}/{object_path}"


def brand_asset_object_path(brand_id: str, kind: str, data: bytes, ext: str) -> str:
    """``brands/<brand_id>/<kind>s/<sha256[:16]>.<ext>`` — pure, no I/O."""
    if kind not in BRAND_ASSET_EXTS:
        raise ValueError(f"unknown brand asset kind {kind!r}")
    clean = _clean_ext(ext, BRAND_ASSET_EXTS[kind])
    return f"brands/{brand_id}/{kind}s/{content_hash(data)}.{clean}"


def put_brand_asset(brand_id: str, kind: str, data: bytes, ext: str) -> str:
    """Store one brand-kit file (``kind`` = logo | font | guidelines) at its
    content-hash path and return its ``gs://`` URI. Storage only — recording it
    on the brand doc is ``firestore_repo.add_brand_asset``'s job."""
    object_path = brand_asset_object_path(brand_id, kind, data, ext)
    return _put_object(object_path, data, content_type_for_ext(ext))


def reference_object_path(brand_id: str, data: bytes, ext: str) -> str:
    """``reference_library/<brand_id>/<sha256[:16]>.<ext>`` — pure, no I/O.

    Sits beside the Drive-synced ``reference_library/<brand>/<type>/…`` tree
    but never inside a ``<type>/`` folder, so the Drive sync (which rebuilds
    the legacy index from its own folders) cannot collide with these."""
    clean = _clean_ext(ext, REFERENCE_EXTS)
    return f"{REFERENCE_LIBRARY_PREFIX}/{brand_id}/{content_hash(data)}.{clean}"


def put_reference_file(brand_id: str, data: bytes, ext: str) -> str:
    """Store one uploaded reference at its content-hash path; ``gs://`` URI."""
    object_path = reference_object_path(brand_id, data, ext)
    return _put_object(object_path, data, content_type_for_ext(ext))


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
    if not blob.exists(timeout=_METADATA_TIMEOUT_SECONDS):
        return None
    return blob.download_as_bytes(timeout=_TRANSFER_TIMEOUT_SECONDS)


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
