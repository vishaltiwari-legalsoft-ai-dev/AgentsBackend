"""Graphics Designer direct-to-GCS uploads — sign, then finalize.

The browser cannot send a large file through the API: the Vercel relay
refuses bodies over 4.5 MB (and breaks over 1 MiB) and Cloud Run HTTP/1 caps
a request at 32 MiB, while members upload ~50 MB originals. So:

1. **sign** — the API hands out a V4 signed ``PUT`` for ONE server-named
   object (``uploads/pending/<surface>/<target>/<user>/<uuid>``), write-once,
   size-ranged, 10 minutes, plus a stateless HMAC *ticket* binding that object
   to the caller, the surface and the target;
2. the browser PUTs the bytes straight to GCS;
3. **finalize** — the API verifies the ticket, re-checks the caller's rights,
   and validates the ACTUAL bytes (``upload_intake``): GCS's own size and MD5,
   a sniffed type, a decode budget, then derives a bounded working copy. The
   original is copied server-side to its permanent home, the working copy is
   stored where GD already reads, the store records it, and the pending
   object is deleted — on success and on every rejection.

The routers own WHO may write WHAT (``_editable_brand``, ``_owned_run``, the
per-brand caps) and how a result is recorded; this module owns the pipeline
both of them share. Kill switch ``GD_DIRECT_UPLOADS`` (default off): off, the
sign AND finalize routes answer 503 ``direct_uploads_disabled`` and the
frontend falls back to the multipart routes.

Idempotency: every permanent name derives from the original's GCS MD5 and the
target, and a small receipt object written beside the pending one before it is
deleted lets a retried finalize (say, after the first response was lost)
answer the same result instead of "not found".
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from fastapi import HTTPException

from app.config import settings
from app.services import storage, upload_intake as intake
from app.services.upload_intake import IntakeRejected, Ticket, TicketError, WorkingCopy

logger = logging.getLogger("agentos.gd_uploads")

PENDING_PREFIX = "uploads/pending"
_RECEIPT_SUFFIX = ".receipt.json"
_RECEIPT_MAX_BYTES = 64 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


# --------------------------------------------------------------------------- #
# Switch, key, errors
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    """``GD_DIRECT_UPLOADS`` — read on every call, so a deployment flips it
    without a code change. Off unless explicitly ``1/true/yes/on``."""
    return os.environ.get("GD_DIRECT_UPLOADS", "").strip().lower() in ("1", "true", "yes", "on")


def require_enabled() -> None:
    if not enabled():
        raise HTTPException(503, {
            "code": "direct_uploads_disabled",
            "message": "Direct uploads are switched off on this deployment — use the regular upload.",
        })


def _ticket_key() -> bytes:
    """Derived from ``JWT_SECRET`` (never used raw): a ticket can never be
    replayed as a session token or vice versa, and staging — which has its own
    JWT secret — cannot mint a ticket production accepts."""
    secret = settings.require("jwt_secret").encode("utf-8")
    return hmac.new(secret, b"agentos/gd-direct-upload-ticket/v1", hashlib.sha256).digest()


def _http(exc: IntakeRejected) -> HTTPException:
    return HTTPException(exc.status, exc.body)


def _storage_unavailable(what: str) -> HTTPException:
    return HTTPException(503, {
        "code": "upload_storage_unavailable",
        "message": f"File storage did not answer while {what}. Try again in a moment.",
    })


def _safe_segment(value: str) -> str:
    """An id as an object-path segment: as-is when already safe, else a hash
    (a user id is a token claim, not something to splice into a path)."""
    return value if _SAFE_ID.match(value or "") else hashlib.sha256(value.encode()).hexdigest()[:24]


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Sign
# --------------------------------------------------------------------------- #
def sign(*, surface: str, target: str, user_id: str, content_type: str,
         size: int | None, file_name: str | None) -> dict[str, Any]:
    """Mint the signed PUT + ticket for one file. The CALLER has already
    checked that ``user_id`` may write to ``target`` on ``surface``."""
    require_enabled()
    rule = intake.SURFACES[surface]
    try:
        ctype = intake.check_declared_content_type(surface, content_type, file_name=file_name)
        cap = rule.cap_for_content_type(ctype)
        if size is not None:
            if size <= 0:
                raise IntakeRejected(422, "empty_file", "The file is empty.", file=file_name)
            if size > cap:
                raise intake.too_large(size, cap, file_name)
    except IntakeRejected as exc:
        raise _http(exc) from exc
    if not storage.is_configured():
        raise HTTPException(503, {"code": "upload_storage_unavailable",
                                  "message": "File storage is not configured on this deployment."})

    object_name = (f"{PENDING_PREFIX}/{surface}/{_safe_segment(target)}/"
                   f"{_safe_segment(user_id)}/{uuid.uuid4().hex}")
    try:
        url, headers = storage.signed_put_url(object_name, content_type=ctype, max_bytes=cap)
    except Exception as exc:  # noqa: BLE001 - signing goes through IAM signBlob on Cloud Run
        logger.exception("could not sign a direct upload for %s/%s", surface, target)
        raise _storage_unavailable("preparing the upload") from exc
    now = datetime.now(timezone.utc).timestamp()
    ticket, exp = intake.mint_ticket(_ticket_key(), sub=user_id, surface=surface, target=target,
                                     object_name=object_name, cap=cap, now=now)
    return {
        "surface": surface,
        "upload_url": url,
        "method": "PUT",
        "headers": headers,
        "max_bytes": cap,
        "expires_at": _iso(now + storage.SIGNED_PUT_TTL.total_seconds()),
        "ticket": ticket,
        "ticket_expires_at": _iso(exp),
    }


# --------------------------------------------------------------------------- #
# Finalize
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Inspected:
    """Cheap facts about the pending object, before any decode."""

    size: int
    generation: int
    content_id: str       # GCS MD5, hex
    kind: str             # sniffed


@dataclass
class Placed:
    """Where a finalized upload now lives. JSON round-trips through the receipt."""

    surface: str
    target: str
    file: str | None
    kind: str
    content_id: str
    original_uri: str
    original_bytes: int
    original_width: int | None = None
    original_height: int | None = None
    working_uri: str | None = None      # brand surfaces (gs://)
    working_ref: str | None = None      # run surfaces (artifact ref)
    working_object_path: str | None = None
    working_content_type: str | None = None
    working_width: int | None = None
    working_height: int | None = None
    flags: list[str] = field(default_factory=list)
    pages_used: str | None = None
    pages: int | None = None

    def stored_upload(self):
        """The store's view of a brand-surface upload."""
        from app.services.firestore_repo import StoredUpload

        return StoredUpload(
            content_id=self.content_id, working_uri=self.working_uri or self.original_uri,
            working_object_path=self.working_object_path or "",
            working_content_type=self.working_content_type or "",
            original_uri=self.original_uri, original_bytes=self.original_bytes,
            working_width=self.working_width, working_height=self.working_height,
            original_width=self.original_width, original_height=self.original_height,
            flags=tuple(self.flags), pages_used=self.pages_used, pages=self.pages)


def upload_summary(placed: Placed, *, already_finalized: bool) -> dict[str, Any]:
    """What the client is told about one finalized file. The original is
    offered only as an attachment download, never as something to render."""
    download = None
    try:
        name = f"{_display_stem(placed.file)}.{intake.KIND_EXT.get(placed.kind, 'bin')}"
        download = storage.signed_download_url(placed.original_uri, file_name=name)
    except Exception:  # noqa: BLE001 - the link is a convenience; the upload stands
        logger.warning("could not sign the original's download link %s", placed.original_uri,
                       exc_info=True)
    working = None
    if placed.working_width is not None:
        working = {"width": placed.working_width, "height": placed.working_height,
                   "format": "png" if (placed.working_content_type or "").endswith("png") else "jpeg"}
    return {
        "surface": placed.surface,
        "file": placed.file,
        "status": "stored",
        "already_finalized": already_finalized,
        "kind": placed.kind,
        "content_id": placed.content_id,
        "original": {"bytes": placed.original_bytes, "width": placed.original_width,
                     "height": placed.original_height, "pages": placed.pages,
                     "download_url": download},
        "working": working,
        "pages_used": placed.pages_used,
        "flags": list(placed.flags),
    }


def _display_stem(file_name: str | None) -> str:
    stem = (file_name or "original").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].rsplit(".", 1)[0]
    return re.sub(r"[^\w.\-() ]", "_", stem)[:100] or "original"


class Finalizing:
    """One finalize request, from a verified ticket to the receipt.

    Use::

        up = begin(token, user=user, target=brand_id, scope="brand", file_name=...)
        if up.replay is not None: ...answer the recorded result...
        with up.rejecting():
            ...rights...; ins = up.inspect(); ...caps...; placed = up.derive_and_place(ins)
            result = ...record through the store...
        up.complete(placed, result)
    """

    def __init__(self, ticket: Ticket, file_name: str | None):
        self.ticket = ticket
        self.surface = ticket.surface
        self.rule = intake.SURFACES[ticket.surface]
        self.file_name = (file_name or "").strip()[:255] or None
        self.replay: dict[str, Any] | None = None

    # -- receipts ---------------------------------------------------------
    @property
    def _receipt_path(self) -> str:
        return self.ticket.object + _RECEIPT_SUFFIX

    def _load_receipt(self) -> dict[str, Any] | None:
        try:
            raw = storage.read_object(self._receipt_path, max_bytes=_RECEIPT_MAX_BYTES)
        except Exception as exc:  # noqa: BLE001
            logger.exception("could not read upload receipt %s", self._receipt_path)
            raise _storage_unavailable("checking this upload") from exc
        if raw is None:
            return None
        receipt = json.loads(raw)
        receipt["placed"] = Placed(**receipt["placed"])
        return receipt

    # -- rejection --------------------------------------------------------
    def discard_pending(self) -> None:
        """Delete the pending object; a failure is logged, never raised —
        the bucket's lifecycle rule is the backstop for what this misses."""
        try:
            storage.delete_object(self.ticket.object)
        except Exception:  # noqa: BLE001
            logger.warning("could not delete pending upload %s", self.ticket.object, exc_info=True)

    @contextlib.contextmanager
    def rejecting(self) -> Iterator[None]:
        """Every refusal inside deletes the pending object and answers with
        its own status and reason. Infrastructure faults do NOT delete it, so
        the same ticket can be retried: a busy decode slot (503 +
        ``Retry-After``), a missing SVG renderer (503), storage errors (503)."""
        try:
            yield
        except IntakeRejected as exc:
            self.discard_pending()
            raise _http(exc) from exc
        except HTTPException as exc:
            if 400 <= exc.status_code < 500:
                self.discard_pending()
                if isinstance(exc.detail, str):
                    raise HTTPException(exc.status_code, {"code": _code(exc.detail),
                                                          "message": exc.detail,
                                                          "file": self.file_name},
                                        headers=exc.headers) from exc
            raise
        except intake.DecodeBusy as exc:
            raise HTTPException(
                503, {"code": "upload_busy",
                      "message": "Another large file is being processed — try again shortly.",
                      "retry_after": exc.retry_after},
                headers={"Retry-After": str(exc.retry_after)}) from exc
        except intake.SvgRendererUnavailable as exc:
            logger.error("SVG logo finalize on a host without cairosvg/libcairo")
            raise HTTPException(503, {"code": "svg_renderer_unavailable",
                                      "message": "SVG logos cannot be processed on this server "
                                                 "right now — upload a PNG instead."}) from exc
        except (OSError, RuntimeError) as exc:
            logger.exception("direct upload storage fault: %s", self.ticket.object)
            raise _storage_unavailable("processing the upload") from exc
        except Exception as exc:
            from google.api_core.exceptions import GoogleAPIError

            if isinstance(exc, GoogleAPIError):
                logger.exception("direct upload storage fault: %s", self.ticket.object)
                raise _storage_unavailable("processing the upload") from exc
            raise

    # -- the pipeline -----------------------------------------------------
    def inspect(self) -> Inspected:
        """GCS's size + MD5, then the sniffed type and its cap. Cheap."""
        info = storage.object_info(self.ticket.object)
        if info is None:
            raise HTTPException(404, {
                "code": "upload_not_found",
                "message": "The uploaded file was not found — the upload did not finish, or it "
                           "expired. Upload it again.", "file": self.file_name})
        if info.size > self.ticket.cap:   # GCS enforces the signed range; belt and braces
            raise intake.too_large(info.size, self.ticket.cap, self.file_name)
        if info.size <= 0:
            raise IntakeRejected(422, "empty_file", "The uploaded file is empty.",
                                 file=self.file_name)
        if not info.md5_hex:
            raise RuntimeError(f"GCS reported no MD5 for {self.ticket.object}")
        head = storage.read_object_range(self.ticket.object, 0,
                                         min(info.size, intake.SNIFF_BYTES) - 1,
                                         generation=info.generation)
        kind = intake.check_kind(self.surface, head, info.size, file_name=self.file_name)
        return Inspected(size=info.size, generation=info.generation,
                         content_id=info.md5_hex, kind=kind)

    def planned_run_ref(self, ins: Inspected) -> str:
        """The artifact ref a run upload will get (for cap/dedupe checks)."""
        return f"{ins.content_id}-w{intake.WORK_SIDE_PX}.{'png' if self.rule.alpha else 'jpg'}"

    def derive_and_place(self, ins: Inspected) -> Placed:
        """Validate the bytes (decode work inside the instance's single
        decode slot), copy the original server-side, store the working copy."""
        working: WorkingCopy | None = None
        pages: int | None = None
        obj, gen = self.ticket.object, ins.generation
        if ins.kind in intake.RASTER_KINDS:
            with intake.decode_slot(), storage.open_object_reader(obj, generation=gen) as fp:
                working = intake.derive_working_copy(fp, ins.kind, alpha=self.rule.alpha,
                                                     file_name=self.file_name)
        elif ins.kind == "svg":
            data = storage.read_object(obj, max_bytes=self.rule.cap_for_kind("svg"))
            if data is None:
                raise RuntimeError(f"pending upload {obj} vanished mid-finalize")
            info = intake.check_svg(data, file_name=self.file_name)
            with intake.decode_slot():
                png = intake.rasterize_svg(data, info)
            working = intake.svg_working_copy(data, info, png)
        elif ins.kind == "pdf":
            tail_start = max(0, ins.size - intake.PDF_TAIL_BYTES)
            intake.check_pdf_tail(storage.read_object_range(obj, tail_start, ins.size - 1,
                                                            generation=gen),
                                  file_name=self.file_name)
            with intake.decode_slot(), storage.open_object_reader(obj, generation=gen) as fp:
                pages = intake.count_pdf_pages(fp, file_name=self.file_name)
        elif ins.kind in ("ttf", "otf"):
            data = storage.read_object(obj, max_bytes=self.rule.cap_for_kind(ins.kind))
            if data is None:
                raise RuntimeError(f"pending upload {obj} vanished mid-finalize")
            intake.check_font(data, file_name=self.file_name)
        else:  # check_kind only admits the kinds above
            raise intake.unsupported_type(self.surface, ins.kind, self.file_name)

        ext = intake.KIND_EXT[ins.kind]
        original_path = f"{self._originals_prefix()}{ins.content_id}.{ext}"
        original_uri = storage.copy_object(
            obj, original_path, src_generation=gen,
            content_type=intake.KIND_CONTENT_TYPE[ins.kind],
            content_disposition="attachment")
        placed = Placed(surface=self.surface, target=self.ticket.target, file=self.file_name,
                        kind=ins.kind, content_id=ins.content_id, original_uri=original_uri,
                        original_bytes=ins.size, pages=pages)
        if working is None:   # fonts and PDFs are used as uploaded
            placed.working_uri = original_uri
            placed.working_object_path = original_path
            placed.working_content_type = intake.KIND_CONTENT_TYPE[ins.kind]
            return placed
        placed.original_width, placed.original_height = working.original_width, working.original_height
        placed.working_width, placed.working_height = working.width, working.height
        placed.working_content_type = working.content_type
        placed.flags = list(working.flags)
        placed.pages_used = working.pages_used
        self._store_working(placed, ins, working)
        return placed

    def _originals_prefix(self) -> str:
        target = self.ticket.target
        if self.rule.scope == "brand":
            return f"brands/{target}/originals/"
        from graphics_designer_agent.runs import originals_prefix

        return originals_prefix(target)

    def _store_working(self, placed: Placed, ins: Inspected, working: WorkingCopy) -> None:
        target, cid = self.ticket.target, ins.content_id
        if self.surface == "logo":
            side = intake.SVG_RASTER_PX if ins.kind == "svg" else intake.WORK_SIDE_PX
            path = f"brands/{target}/logos/{cid}-w{side}.{working.ext}"
        elif self.surface == "reference":
            path = f"{storage.REFERENCE_LIBRARY_PREFIX}/{target}/{cid}-w{intake.WORK_SIDE_PX}.{working.ext}"
        else:
            from graphics_designer_agent.runs import save_upload_artifact

            placed.working_ref = save_upload_artifact(
                target, f"{cid}-w{intake.WORK_SIDE_PX}.{working.ext}", working.data,
                working.content_type)
            return
        placed.working_uri = storage.put_object(path, working.data, working.content_type)
        placed.working_object_path = path

    def complete(self, placed: Placed, result: dict[str, Any]) -> None:
        """Write the receipt (so a retry answers the same), then delete the
        pending object. Neither failing un-does the recorded upload."""
        receipt = {"placed": asdict(placed), "result": result}
        try:
            storage.put_object(self._receipt_path, json.dumps(receipt).encode("utf-8"),
                               "application/json")
        except Exception:  # noqa: BLE001 - a retry would redo idempotent work, nothing worse
            logger.warning("could not write upload receipt %s", self._receipt_path, exc_info=True)
        self.discard_pending()


def _code(detail: str) -> str:
    """A stable code for a router's plain-string refusal ("Run not found")."""
    return re.sub(r"[^a-z0-9]+", "_", detail.lower()).strip("_") or "refused"


@contextlib.contextmanager
def coded_refusals() -> Iterator[None]:
    """Give a router's plain-string 4xx (``brand_not_editable``, ``Run not
    found``) the same ``{"code", "message"}`` body every upload error has, so
    the client reads ``detail.code`` on every upload route."""
    try:
        yield
    except HTTPException as exc:
        if 400 <= exc.status_code < 500 and isinstance(exc.detail, str):
            raise HTTPException(exc.status_code, {"code": _code(exc.detail), "message": exc.detail},
                                headers=exc.headers) from exc
        raise


def begin(token: str, *, user: dict, target: str, scope: str,
          file_name: str | None) -> Finalizing:
    """Verify the ticket against THIS request: signature and expiry, then
    ``sub`` == caller, ``target`` == the path's id, and a surface this route
    family finalizes. Nothing is touched on a ticket refusal — a forged or
    foreign ticket must not be able to delete the object it names. A ticket
    with a receipt comes back with ``replay`` set."""
    require_enabled()
    try:
        ticket = intake.read_ticket(_ticket_key(), token)
    except TicketError as exc:
        raise HTTPException(403, {"code": exc.code, "message": str(exc)}) from exc
    if ticket.sub != str(user["id"]):
        raise HTTPException(403, {"code": "upload_ticket_wrong_user",
                                  "message": "This upload ticket was issued to someone else."})
    if ticket.target != target:
        raise HTTPException(403, {"code": "upload_ticket_wrong_target",
                                  "message": "This upload ticket is for a different "
                                             f"{'brand' if scope == 'brand' else 'run'}."})
    if intake.SURFACES[ticket.surface].scope != scope:
        raise HTTPException(403, {"code": "upload_ticket_wrong_surface",
                                  "message": "This upload ticket is not for this kind of upload."})
    up = Finalizing(ticket, file_name)
    up.replay = up._load_receipt()
    return up
