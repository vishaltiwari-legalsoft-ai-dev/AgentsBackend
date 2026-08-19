"""Google Drive ingestion source for the Brand Reference Library.

Downloads the reference creatives the team has shared with the project service
account into a local directory laid out exactly the way
``graphics_designer_agent.reference_library`` expects::

    <dest>/<Brand>/<creative_type>/<file>

so the existing ingester can understand + index them unchanged.

Auth model
----------
Credentials come from Application Default Credentials with the read-only Drive
scope::

    google.auth.default(scopes=["https://www.googleapis.com/auth/drive.readonly"])

That resolves to the SAME service account both places we run:
- **Local dev** — the JSON key at ``GOOGLE_APPLICATION_CREDENTIALS``.
- **Cloud Run** — the attached service-account identity (no key file).

The only prerequisites are (1) the Google Drive API enabled on the project and
(2) the target folder shared with the service account (both one-time setup).

This module does Drive I/O ONLY. Understanding/indexing stays in
``reference_library`` so the two concerns can be tested in isolation.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("agentos.drive_source")

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

# Drive subfolder name (lowercased) -> the library type it maps to. This mirrors
# ``reference_library.FOLDER_ALIASES`` but is kept here too so the Drive layer is
# self-describing; ``reference_library.resolve_folder_type`` is the source of
# truth applied again at ingest time.
DRIVE_FOLDER_MAP: dict[str, str] = {
    "carousel": "carousel",
    "story": "social_story",
    "ls gradients": "brand_gradient",
    "newsletter graphics": "newsletter",
    "brochure and flyer": "brochure",
    "blog covers": "blog",
}

# Folders + files Google represents with these MIME types are native editor docs
# (Docs/Sheets/Slides) — they have no binary to download directly, so we skip
# them rather than fail. (We only want real image/PDF assets here.)
_FOLDER_MIME = "application/vnd.google-apps.folder"
_GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."

# Mirror the reference library's accepted asset extensions.
_REF_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".pdf", ".pptx"}

# --- Deadlines and caps ----------------------------------------------------
# A Drive sync runs in a sync handler, i.e. on one of anyio's 40 worker threads.
# googleapiclient applies an implicit 60s socket timeout of its own, but the
# *loops* below (paging, chunked download) had no ceiling at all, so total time
# was unbounded no matter what the socket did. Both ends are fixed here.
DRIVE_TIMEOUT_SECONDS = 30
# 1000 files per page; 50 pages is 50k files in one folder — far past anything
# this library holds. Hitting it means something is wrong, so it raises rather
# than quietly returning a partial folder (a partial sync that reports success
# is exactly the kind of lie this repo does not ship).
MAX_LIST_PAGES = 50
# Chunked download: 5MB keeps each request comfortably inside the socket
# deadline (googleapiclient's default chunk is 100MB, which is a lot to move
# before the transport is allowed to notice a stall).
DOWNLOAD_CHUNK_BYTES = 5 * 1024 * 1024
# Reference assets are images/PDFs. Anything past this is not a reference
# creative and must not be pulled into memory.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
# Drive folders cannot cycle, but shortcuts can point back up the tree. Bound
# the walk so a malformed folder cannot recurse forever.
MAX_FOLDER_DEPTH = 6


def build_drive_service():
    """Build an authenticated Drive v3 client via ADC + the read-only scope.

    Imports the Google API client lazily so importing this module never requires
    the dependency to be installed (keeps offline tests + partial envs working).

    The transport is built by hand rather than via ``credentials=`` because that
    is the only way to state a socket timeout; googleapiclient otherwise applies
    its own implicit default. A fresh ``httplib2.Http`` per client is deliberate
    — httplib2 is not thread-safe and this runs on worker threads. ``build``
    itself opens no socket (the discovery document ships with the library).
    """
    import google.auth
    import google_auth_httplib2
    import httplib2
    from googleapiclient.discovery import build

    creds, _project = google.auth.default(scopes=[DRIVE_READONLY_SCOPE])
    http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=DRIVE_TIMEOUT_SECONDS)
    )
    # cache_discovery=False avoids a noisy warning + a write to an unwritable dir
    # on Cloud Run; the discovery doc is tiny.
    return build("drive", "v3", http=http, cache_discovery=False)


def _safe_segment(name: str) -> str:
    """Make a Drive name safe to use as a single path segment."""
    cleaned = re.sub(r"[^\w.\-() ]", "_", name).strip()
    return cleaned or "untitled"


def _list_children(service, parent_id: str) -> list[dict[str, Any]]:
    """All non-trashed direct children of a Drive folder (handles paging)."""
    children: list[dict[str, Any]] = []
    page_token: Optional[str] = None
    query = f"'{parent_id}' in parents and trashed = false"
    for _page in range(MAX_LIST_PAGES):
        resp = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType)",
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageToken=page_token,
            )
            .execute()
        )
        children.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return children
    raise RuntimeError(
        f"Drive folder {parent_id} has more than {MAX_LIST_PAGES} pages of children "
        f"({len(children)} listed so far) — refusing to return a partial listing."
    )


def _download_file(service, file_id: str) -> bytes:
    """Download a binary Drive file's bytes.

    Bounded on both axes: each chunk is a request with its own socket deadline,
    and the total is capped so an unexpectedly huge file fails loudly instead of
    looping until the process runs out of memory.
    """
    from googleapiclient.http import MediaIoBaseDownload

    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=DOWNLOAD_CHUNK_BYTES)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
        if buf.tell() > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(
                f"Drive file {file_id} exceeds the {MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB "
                f"reference-asset limit — not downloading it."
            )
    return buf.getvalue()


def _download_folder_recursive(
    service,
    folder_id: str,
    dest: Path,
    *,
    resolve_type: Callable[[str], Optional[str]],
    depth: int = 0,
) -> int:
    """Walk one creative-type folder, downloading every asset file into ``dest``.

    Returns the number of files written. Nested subfolders are flattened into the
    same type folder (their names prefix the file) so an arbitrarily organised
    Drive folder still lands as ``<type>/<file>``."""
    if depth >= MAX_FOLDER_DEPTH:
        logger.warning(
            "not descending past depth %s in Drive folder %s", MAX_FOLDER_DEPTH, folder_id
        )
        return 0
    written = 0
    for child in _list_children(service, folder_id):
        name = child.get("name", "")
        mime = child.get("mimeType", "")
        if mime == _FOLDER_MIME:
            # Flatten one level deeper, prefixing to keep names unique.
            sub = dest  # same type dir
            nested = _download_folder_recursive(
                service, child["id"], sub, resolve_type=resolve_type, depth=depth + 1
            )
            written += nested
            continue
        if mime.startswith(_GOOGLE_NATIVE_PREFIX):
            logger.info("skipping Google-native file (no binary): %s", name)
            continue
        if Path(name).suffix.lower() not in _REF_EXTS:
            logger.info("skipping non-asset file: %s", name)
            continue
        try:
            data = _download_file(service, child["id"])
        except Exception as exc:  # noqa: BLE001 - skip a bad file, keep going
            logger.warning("could not download '%s': %s", name, exc)
            continue
        dest.mkdir(parents=True, exist_ok=True)
        (dest / _safe_segment(name)).write_bytes(data)
        written += 1
    return written


def download_folder(
    folder_id: str,
    dest_dir: Path,
    *,
    brand_name: str = "Legal Soft",
    service=None,
    resolve_type: Optional[Callable[[str], Optional[str]]] = None,
) -> dict[str, Any]:
    """Download a shared Drive folder into ``dest_dir/<brand_name>/<type>/...``.

    Each top-level subfolder of ``folder_id`` is resolved to a library creative
    type (via ``resolve_type``; defaults to ``reference_library.resolve_folder_type``).
    Unmapped subfolders and loose files at the root are skipped + logged.

    Returns a summary ``{brand, downloaded, skipped_folders, by_type}``. The
    Drive ``service`` can be injected for tests; otherwise it is built from ADC.
    """
    if resolve_type is None:
        from graphics_designer_agent import reference_library as rl

        resolve_type = rl.resolve_folder_type
    if service is None:
        service = build_drive_service()

    brand_dir = Path(dest_dir) / _safe_segment(brand_name)
    downloaded = 0
    skipped_folders: list[str] = []
    by_type: dict[str, int] = {}

    for child in _list_children(service, folder_id):
        if child.get("mimeType") != _FOLDER_MIME:
            # Loose files at the context root have no creative type — skip.
            logger.info("skipping loose root file: %s", child.get("name"))
            continue
        folder_name = child.get("name", "")
        creative_type = resolve_type(folder_name)
        if not creative_type:
            logger.warning("skipping unmapped Drive folder: %s", folder_name)
            skipped_folders.append(folder_name)
            continue
        type_dir = brand_dir / creative_type
        count = _download_folder_recursive(
            service, child["id"], type_dir, resolve_type=resolve_type
        )
        by_type[creative_type] = by_type.get(creative_type, 0) + count
        downloaded += count

    return {
        "brand": brand_name,
        "downloaded": downloaded,
        "skipped_folders": skipped_folders,
        "by_type": by_type,
    }
