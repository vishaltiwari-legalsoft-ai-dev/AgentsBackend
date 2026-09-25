"""Self-serve Graphics Designer brands — ``/api/gd/brands``.

Any signed-in member may create a brand, edit its kit and upload its logo,
fonts, guidelines and reference creatives; archiving is admin-only and soft.
Brands are ONE company-wide set (owner decision, 2026-09-25): every route here
serves the same rows to every member, which is why the ledger classifies them
WORKSPACE_SHARED.

Layering: validation and status codes live here; every write goes through
``firestore_repo``'s self-serve store (transactions, the cap, the version
bump) and ``storage`` (content-hash object names). The GD pack a brand becomes
is derived by ``gd_spec_builder.build_self_serve_spec`` — the same
``derive_palette`` + ``build_gd_spec`` path CLI ingestion uses — from the
colours the user typed, which are used exactly as given.

Files reach the browser as short-lived signed GCS URLs, the way every other GD
route serves images (``_artifact_url``, ``_ingested_logo_url``).

Upload limits exist because Cloud Run caps a request at 32 MiB, not because
members are suspected of anything; types are sniffed from the bytes because
an extension is a claim, not a fact.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field, StringConstraints

from app.security import get_current_user, require_admin
from app.services import firestore_repo, imaging, storage
from app.services.firestore_repo import (
    BRAND_REFERENCE_CAP,
    BUILTIN_GD_PACK_IDS,
    BrandExists,
    BrandNotEditable,
    BrandNotFound,
    ReferenceCapReached,
)
from app.services.gd_brand_source import brand_logo_record
from app.services.gd_spec_builder import build_self_serve_spec, font_variant_for_upload
from graphics_designer_agent import registry
from graphics_designer_agent import reference_library as rl

router = APIRouter()
logger = logging.getLogger("agentos.gd_brands")

MB = 1024 * 1024

# --------------------------------------------------------------------------- #
# Upload rules — what the contract promises the frontend
# --------------------------------------------------------------------------- #
ASSET_RULES: dict[str, dict] = {
    # ``per_brand`` matches the sheet's ``maxFiles: 8``; rasters are further
    # capped at ``imaging.LOGO_MAX_SIDE_PX`` per side (a 21 KB PNG can be
    # 9000×9000) and SVGs may not embed images — see ``_check_logo``.
    "logo": {"exts": frozenset({"png", "svg", "webp", "jpg"}), "max_bytes": 5 * MB, "per_brand": 8},
    "font": {"exts": frozenset({"ttf", "otf"}), "max_bytes": 2 * MB, "per_brand": 16},
    "guidelines": {"exts": frozenset({"pdf"}), "max_bytes": 20 * MB, "per_request": 1},
}
REFERENCE_RULE = {"exts": frozenset({"png", "jpg", "webp"}), "max_bytes": 10 * MB,
                  "per_request": 10}

AssetKind = Literal["logo", "font", "guidelines"]
ReferenceKind = Literal["creative", "reference"]

_HEX_PATTERN = r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$"
Hex = Annotated[str, StringConstraints(pattern=_HEX_PATTERN)]


# --------------------------------------------------------------------------- #
# Request models (the boundary; the service layer sees clean types)
# --------------------------------------------------------------------------- #
class BrandCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    primary_colors: list[Hex] = Field(min_length=1, max_length=12)
    secondary_colors: list[Hex] = Field(default_factory=list, max_length=12)
    accent_colors: list[Hex] = Field(default_factory=list, max_length=12)
    fonts: list[Annotated[str, StringConstraints(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=32)
    tone_of_voice: str | None = Field(default=None, max_length=2000)
    website: str | None = Field(default=None, max_length=500)


class BrandPatch(BaseModel):
    """Every field optional; ``None`` means "leave it alone"."""
    name: str | None = Field(default=None, min_length=1, max_length=120)
    primary_colors: list[Hex] | None = Field(default=None, min_length=1, max_length=12)
    secondary_colors: list[Hex] | None = Field(default=None, max_length=12)
    accent_colors: list[Hex] | None = Field(default=None, max_length=12)
    fonts: list[Annotated[str, StringConstraints(min_length=1, max_length=80)]] | None = Field(
        default=None, max_length=32)
    tone_of_voice: str | None = Field(default=None, max_length=2000)
    website: str | None = Field(default=None, max_length=500)


# --------------------------------------------------------------------------- #
# Byte sniffing — the extension is never trusted
# --------------------------------------------------------------------------- #
def _looks_like_svg(data: bytes) -> bool:
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n")
    return (head.startswith(b"<?xml") or head.startswith(b"<svg")) and b"<svg" in data[:65536]


_SNIFFERS: tuple[tuple[str, object], ...] = (
    ("png", lambda d: d.startswith(b"\x89PNG\r\n\x1a\n")),
    ("jpg", lambda d: d.startswith(b"\xff\xd8\xff")),
    ("webp", lambda d: d[:4] == b"RIFF" and d[8:12] == b"WEBP"),
    ("pdf", lambda d: d.startswith(b"%PDF-")),
    ("otf", lambda d: d.startswith(b"OTTO")),
    ("ttf", lambda d: d[:4] in (b"\x00\x01\x00\x00", b"true")),
    ("svg", _looks_like_svg),
)


def sniff_ext(data: bytes) -> str | None:
    """The file type the BYTES say they are (``png``/``jpg``/``webp``/``pdf``/
    ``otf``/``ttf``/``svg``), or ``None`` when they are none of those."""
    for ext, test in _SNIFFERS:
        if test(data):  # type: ignore[operator]
            return ext
    return None


def _read_upload(upload: UploadFile, *, max_bytes: int, allowed: frozenset[str]) -> tuple[bytes, str]:
    """Read one upload within its limit and sniff its type; 413 / 415 / 422.

    Reads ``max_bytes + 1`` so a 30 MB file costs one extra byte of memory
    over the limit, not 30 MB. Sync on purpose — these handlers are plain
    ``def`` and run on the threadpool, so the blocking read is fine here and
    the blocking Firestore/GCS calls that follow are not on the event loop.
    """
    data = upload.file.read(max_bytes + 1)
    if not data:
        raise HTTPException(422, "empty_file")
    if len(data) > max_bytes:
        raise HTTPException(413, "file_too_large")
    ext = sniff_ext(data)
    if ext is None or ext not in allowed:
        raise HTTPException(415, "unsupported_file_type")
    return data, ext


def _check_logo(data: bytes, ext: str) -> None:
    """The decode-cost gate a logo passes AFTER the byte sniff: 415
    ``image_too_large`` over ``imaging.LOGO_MAX_SIDE_PX`` per side (header
    read only), 415 ``unsupported_file_type`` for an SVG carrying ``<image>``
    or a ``data:`` URI, or bytes Pillow cannot open at all."""
    try:
        imaging.validate_logo_upload(data, file_name=f"logo.{ext}")
    except imaging.LogoRejected as exc:
        raise HTTPException(415, exc.code) from exc


# --------------------------------------------------------------------------- #
# Response shaping
# --------------------------------------------------------------------------- #
def _view_url(gs_uri: str | None) -> str | None:
    """Signed browser URL for a ``gs://`` object; ``None`` when there is no
    object or it cannot be signed (logged — a picture is decorative, the
    brand data is not)."""
    if not gs_uri or not storage.is_configured():
        return None
    try:
        return storage.signed_url_for_gs_uri(gs_uri)
    except Exception:  # noqa: BLE001 - never fail the brand for a thumbnail
        logger.warning("could not sign %s", gs_uri, exc_info=True)
        return None


def _asset(gs_uri: str) -> dict:
    path = gs_uri[len("gs://"):].partition("/")[2] if gs_uri.startswith("gs://") else gs_uri
    return {"path": path, "url": _view_url(gs_uri), "name": path.rsplit("/", 1)[-1]}


def _reference(rec: dict) -> dict:
    """Contract ``Reference`` for an uploaded doc OR a legacy index record.
    ``url`` is always a string — empty when there is no object or it could
    not be signed — so the sheet never branches on ``null``."""
    return {
        "ref_id": rec.get("ref_id") or rec.get("id"),
        "url": _view_url(rec.get("gs_uri")) or "",
        "kind": rec.get("kind") or "reference",
        "creative_type": rec.get("creative_type"),
        "note": rec.get("note") or rec.get("summary") or "",
        "created_at": rec.get("created_at") or rec.get("ingested_at"),
    }


def _legacy_records() -> list[dict]:
    """The Drive-synced index (GCS copy when configured, else local) — read
    ONCE per request and handed to the store, never per brand."""
    try:
        return rl.load_index(rl.default_base_dir())
    except Exception:  # noqa: BLE001 - legacy refs are additive
        logger.warning("legacy reference index unreadable", exc_info=True)
        return []


def _legacy_count(records: list[dict], brand_id: str) -> int:
    key = firestore_repo._slug_key(brand_id)
    return sum(1 for r in records if firestore_repo._slug_key(str(r.get("brand_id", ""))) == key)


def _pack_id(doc: dict) -> str:
    """The id the registry serves this brand's pack under — the studio's
    ``brand_id``. Self-serve docs are keyed by it; CLI-ingested docs keep a
    uuid id and carry the pack id in ``gd_spec.id``."""
    return str(((doc.get("brand_metadata") or {}).get("gd_spec") or {}).get("id") or doc["id"])


def _is_user_brand(doc: dict) -> bool:
    return doc.get("source") == "user"


def _summary_from_doc(doc: dict, *, reference_count: int) -> dict:
    """Picker row for a brand doc. Only self-serve docs are editable; a
    CLI-ingested doc (any other ``source``) is a read-only pack to the sheet
    and reports ``source="builtin"``, the contract's read-only value."""
    meta = doc.get("brand_metadata") or {}
    enrichment = meta.get("enrichment") or {}
    has_kit = any(enrichment.get(k) for k in ("logo_files", "font_files", "guideline_files"))
    pack_id = _pack_id(doc)
    user_brand = _is_user_brand(doc)
    return {
        "brand_id": pack_id,
        "id": pack_id,  # deploy-skew: the pre-contract picker read ``id``
        "name": doc.get("name") or doc.get("brand_name"),
        "slug": doc.get("slug") or pack_id,
        "source": "user" if user_brand else "builtin",
        "editable": user_brand and not doc.get("archived_at"),
        "logo_url": _view_url(doc.get("logo_uri")),
        "primary_colors": list(meta.get("primary_colors") or []),
        "has_kit": bool(has_kit),
        "reference_count": reference_count,
    }


def _summary_from_pack(pack, *, reference_count: int) -> dict:
    rec = brand_logo_record(pack.firestore_brand_id)
    return {
        "brand_id": pack.id,
        "id": pack.id,
        "name": pack.name,
        "slug": pack.id,
        "source": "builtin",
        "editable": False,
        "logo_url": _view_url(rec["file_url"]) if rec else None,
        "primary_colors": [h for h in pack.locked_colors["gradient"] if h.upper() != "#FFFFFF"],
        "has_kit": True,
        "reference_count": reference_count,
    }


def _references_for(brand_id: str, legacy: list[dict]) -> list[dict]:
    return [_reference(r) for r in firestore_repo.references_for_brand(brand_id, legacy_records=legacy)]


def _detail_from_doc(doc: dict) -> dict:
    meta = doc.get("brand_metadata") or {}
    enrichment = meta.get("enrichment") or {}
    refs = _references_for(_pack_id(doc), _legacy_records())
    return {
        **_summary_from_doc(doc, reference_count=len(refs)),
        "archived_at": doc.get("archived_at"),
        "created_by": doc.get("created_by"),
        "tone_of_voice": meta.get("tone_of_voice"),
        "website": enrichment.get("website"),
        "fonts": list(meta.get("fonts") or []),
        "colors": {
            "primary": list(meta.get("primary_colors") or []),
            "secondary": list(meta.get("secondary_colors") or []),
            "accent": list(meta.get("accent_colors") or []),
        },
        "assets": {
            "logos": [_asset(u) for u in enrichment.get("logo_files") or []],
            "fonts": [_asset(u) for u in enrichment.get("font_files") or []],
            "guidelines": [_asset(u) for u in enrichment.get("guideline_files") or []],
        },
        "references": refs,
        "reference_cap": BRAND_REFERENCE_CAP,
    }


def _detail_from_pack(pack) -> dict:
    """A built-in pack, read-only. Its Firestore doc (when the kit was ingested)
    supplies the typed colours and files; the pack itself supplies the rest."""
    doc = None
    if pack.firestore_brand_id:
        try:
            doc = firestore_repo.get_brand(pack.firestore_brand_id)
        except Exception:  # noqa: BLE001 - the pack is authoritative; the doc decorates
            logger.warning("brand doc unreadable for built-in %s", pack.id, exc_info=True)
    meta = (doc or {}).get("brand_metadata") or {}
    enrichment = meta.get("enrichment") or {}
    refs = _references_for(pack.id, _legacy_records())
    summary = _summary_from_pack(pack, reference_count=len(refs))
    primary = list(meta.get("primary_colors") or []) or summary["primary_colors"]
    return {
        **summary,
        "archived_at": None,
        "created_by": None,
        "tone_of_voice": meta.get("tone_of_voice"),
        "website": enrichment.get("website"),
        "fonts": list(meta.get("fonts") or []) or [pack.font_family],
        "colors": {
            "primary": primary,
            "secondary": list(meta.get("secondary_colors") or []),
            "accent": list(meta.get("accent_colors") or []) or [pack.locked_colors["accent"]],
        },
        "assets": {
            "logos": [_asset(u) for u in enrichment.get("logo_files") or []],
            "fonts": [_asset(u) for u in enrichment.get("font_files") or []],
            "guidelines": [_asset(u) for u in enrichment.get("guideline_files") or []],
        },
        "references": refs,
        "reference_cap": BRAND_REFERENCE_CAP,
    }


# --------------------------------------------------------------------------- #
# Store access with the contract's status codes
# --------------------------------------------------------------------------- #
def _brand_doc_or_404(brand_id: str) -> dict:
    """The brand doc behind a studio ``brand_id``: the doc keyed by it
    (self-serve), else the doc the registry's pack of that id points at
    (CLI-ingested, uuid-keyed). Docs without a pack (no ``gd_spec``) are not
    brands the studio knows, so they are 404 here as in the picker."""
    doc = firestore_repo.get_brand(brand_id)
    if doc is None:
        try:
            fid = registry.get_pack(brand_id).firestore_brand_id
        except registry.UnknownBrand:
            fid = None
        doc = firestore_repo.get_brand(fid) if fid and fid != brand_id else None
    if doc is None or not (doc.get("brand_metadata") or {}).get("gd_spec"):
        raise HTTPException(404, "brand_not_found")
    return doc


def _user_brand_or_404(brand_id: str) -> dict:
    doc = _brand_doc_or_404(brand_id)
    if not _is_user_brand(doc):
        raise HTTPException(404, "brand_not_found")
    return doc


def _editable_brand(brand_id: str) -> dict:
    """The self-serve brand a write may touch: 409 for built-in, CLI-ingested
    or archived, 404 unknown."""
    if brand_id in BUILTIN_GD_PACK_IDS:
        raise HTTPException(409, "brand_not_editable")
    doc = _brand_doc_or_404(brand_id)
    if not _is_user_brand(doc) or doc.get("archived_at"):
        raise HTTPException(409, "brand_not_editable")
    return doc


def _spec_for(doc: dict, *, extra_font_variants: list[dict] = ()) -> dict:
    """Rebuild the GD spec from what the doc says now. Uploaded fonts are kept
    only while their file is still recorded on the doc (basename match — the
    same rule ``gd_brand_source`` loads them by)."""
    meta = doc.get("brand_metadata") or {}
    enrichment = meta.get("enrichment") or {}
    uploaded = {u.rsplit("/", 1)[-1] for u in enrichment.get("font_files") or []}
    current = ((meta.get("gd_spec") or {}).get("font_variants") or [])
    variants = [v for v in current if v.get("file") in uploaded]
    seen = {v["file"] for v in variants}
    for v in extra_font_variants:
        if v["file"] not in seen:
            variants.append(v)
            seen.add(v["file"])
    return build_self_serve_spec(
        doc.get("name") or doc.get("brand_name") or doc["id"], doc["id"],
        primary_colors=list(meta.get("primary_colors") or []),
        secondary_colors=list(meta.get("secondary_colors") or []),
        accent_colors=list(meta.get("accent_colors") or []),
        tone_of_voice=meta.get("tone_of_voice"),
        font_variants=variants or None,
    )


def _store_call(fn, *args, **kwargs):
    """Map the store's refusals to the contract's status codes."""
    try:
        return fn(*args, **kwargs)
    except BrandExists as exc:
        raise HTTPException(409, "brand_exists") from exc
    except BrandNotFound as exc:
        raise HTTPException(404, "brand_not_found") from exc
    except BrandNotEditable as exc:
        raise HTTPException(409, "brand_not_editable") from exc
    except ReferenceCapReached as exc:
        raise HTTPException(409, "reference_cap_reached") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/gd/brands")
def list_brands(_user: dict = Depends(get_current_user)) -> dict:
    """Every brand the studio can produce for: every non-archived brand doc
    that has a ``gd_spec`` (self-serve first, then CLI-ingested — the same
    set the registry resolves as packs), then the built-in packs. Reference
    counts come from one batched counter read plus one read of the legacy
    index, whatever the number of brands."""
    packs = [registry.get_pack(pid) for pid in BUILTIN_GD_PACK_IDS]
    builtin_doc_ids = {p.firestore_brand_id for p in packs if p.firestore_brand_id}
    docs = [
        d for d in firestore_repo.list_brands()
        if (d.get("brand_metadata") or {}).get("gd_spec")
        and d["id"] not in builtin_doc_ids and _pack_id(d) not in BUILTIN_GD_PACK_IDS
    ]
    docs.sort(key=lambda d: not _is_user_brand(d))   # stable: name order within each group
    counts = firestore_repo.reference_counts([_pack_id(d) for d in docs] + [p.id for p in packs])
    legacy = _legacy_records()

    def total(bid: str) -> int:
        return counts.get(bid, 0) + _legacy_count(legacy, bid)

    brands = [_summary_from_doc(d, reference_count=total(_pack_id(d))) for d in docs]
    brands += [_summary_from_pack(p, reference_count=total(p.id)) for p in packs]
    return {"brands": brands, "default": registry.DEFAULT_BRAND_ID}


@router.get("/gd/brands/{brand_id}")
def get_brand(brand_id: str, _user: dict = Depends(get_current_user)) -> dict:
    if brand_id in BUILTIN_GD_PACK_IDS:
        return {"brand": _detail_from_pack(registry.get_pack(brand_id))}
    return {"brand": _detail_from_doc(_brand_doc_or_404(brand_id))}


@router.post("/gd/brands", status_code=201)
def create_brand(body: BrandCreate, user: dict = Depends(get_current_user)) -> dict:
    name = body.name.strip()
    slug = firestore_repo.brand_slug_for(name)
    if not name or not slug:
        raise HTTPException(422, "a brand name needs at least one letter or digit")
    kit = {
        "primary_colors": body.primary_colors,
        "secondary_colors": body.secondary_colors,
        "accent_colors": body.accent_colors,
        "fonts": body.fonts,
        "tone_of_voice": body.tone_of_voice,
        "gd_spec": build_self_serve_spec(
            name, slug,
            primary_colors=body.primary_colors,
            secondary_colors=body.secondary_colors,
            accent_colors=body.accent_colors,
            tone_of_voice=body.tone_of_voice,
        ),
    }
    if body.website:
        kit["enrichment"] = {"website": body.website.strip()}
    doc = _store_call(firestore_repo.create_brand, name, kit, created_by=str(user.get("email") or ""))
    registry.refresh()  # this instance sees it now; the others via the version
    return {"brand": _detail_from_doc(doc)}


@router.patch("/gd/brands/{brand_id}")
def update_brand(brand_id: str, body: BrandPatch, user: dict = Depends(get_current_user)) -> dict:
    doc = _editable_brand(brand_id)
    patch = body.model_dump(exclude_none=True)
    website = patch.pop("website", None)
    if website is not None:
        patch["enrichment"] = {"website": website.strip()}
    if "name" in patch:
        patch["name"] = patch["name"].strip()
    # The spec is a pure function of the kit, so rebuild it from the merged
    # view rather than reasoning about which field changed.
    merged_meta = dict(doc.get("brand_metadata") or {}) | {
        k: v for k, v in patch.items() if k not in ("name", "enrichment")}
    merged = doc | {"brand_metadata": merged_meta, "name": patch.get("name", doc.get("name"))}
    patch["gd_spec"] = _spec_for(merged)
    updated = _store_call(firestore_repo.update_brand, brand_id, patch)
    registry.refresh()
    return {"brand": _detail_from_doc(updated)}


@router.post("/gd/brands/{brand_id}/assets")
def upload_assets(
    brand_id: str,
    kind: AssetKind = Form(...),
    files: list[UploadFile] = File(...),
    _user: dict = Depends(get_current_user),
) -> dict:
    """Logo (PNG/SVG/WebP/JPEG ≤5 MB, ≤4096 px a side, ≤8 per brand), fonts
    (TTF/OTF ≤2 MB, ≤16 per brand) or guidelines (one PDF ≤20 MB). Every file
    is validated before any byte is stored, so a bad third file never leaves
    the first two half-applied."""
    rule = ASSET_RULES[kind]
    doc = _editable_brand(brand_id)
    if not files:
        raise HTTPException(422, "no_files")
    if rule.get("per_request") and len(files) > rule["per_request"]:
        raise HTTPException(422, "too_many_files")
    enrichment = (doc.get("brand_metadata") or {}).get("enrichment") or {}
    if rule.get("per_brand") and len(enrichment.get(f"{kind}_files") or []) + len(files) > rule["per_brand"]:
        raise HTTPException(409, f"{kind}_limit_reached")

    prepared = [(f.filename or "", *_read_upload(f, max_bytes=rule["max_bytes"], allowed=rule["exts"]))
                for f in files]
    if kind == "logo":
        for _name, data, ext in prepared:
            _check_logo(data, ext)

    new_variants: list[dict] = []
    for original_name, data, ext in prepared:
        stored = _store_call(firestore_repo.add_brand_asset, brand_id, kind, data, ext)
        if kind == "font":
            new_variants.append(
                font_variant_for_upload(original_name, stored["uri"].rsplit("/", 1)[-1]))
    if kind == "font":
        # The pack loads fonts by ``font_variants[].file`` == stored basename;
        # without this the upload is recorded but the pipeline never uses it.
        fresh = _user_brand_or_404(brand_id)
        _store_call(firestore_repo.update_brand, brand_id,
                    {"gd_spec": _spec_for(fresh, extra_font_variants=new_variants)})
    registry.refresh()
    return {"brand": _detail_from_doc(_user_brand_or_404(brand_id))}


@router.post("/gd/brands/{brand_id}/references", status_code=201)
def upload_references(
    brand_id: str,
    files: list[UploadFile] = File(...),
    kind: ReferenceKind = Form("creative"),
    creative_type: str | None = Form(None),
    note: str = Form(""),
    user: dict = Depends(get_current_user),
) -> dict:
    """Up to 10 PNG/JPEG/WebP references (≤10 MB each) for a self-serve or
    built-in brand. A CLI-ingested brand is listed but takes no references
    (409 ``brand_not_editable``): the store keys references by the doc id
    the studio ``brand_id`` is, and those docs are uuid-keyed. The cap is
    checked for the whole batch before any upload and again per file inside
    the store's transaction."""
    from io import BytesIO

    from PIL import Image

    if brand_id not in BUILTIN_GD_PACK_IDS:
        _editable_brand(brand_id)
    if not files:
        raise HTTPException(422, "no_files")
    if len(files) > REFERENCE_RULE["per_request"]:
        raise HTTPException(422, "too_many_files")
    active = firestore_repo.reference_counts([brand_id]).get(brand_id, 0)
    if active + len(files) > BRAND_REFERENCE_CAP:
        raise HTTPException(409, "reference_cap_reached")

    prepared = []
    for f in files:
        data, ext = _read_upload(f, max_bytes=REFERENCE_RULE["max_bytes"], allowed=REFERENCE_RULE["exts"])
        try:
            width, height = Image.open(BytesIO(data)).size  # header only; no decode
        except Exception as exc:  # noqa: BLE001 - magic bytes matched, image did not
            raise HTTPException(415, "unsupported_file_type") from exc
        prepared.append((data, ext, width, height))

    added = [
        _store_call(
            firestore_repo.add_reference, brand_id, data=data, ext=ext, kind=kind,
            uploaded_by=str(user.get("email") or ""), width=width, height=height,
            note=note.strip()[:500], creative_type=(creative_type or "").strip() or None,
        )
        for data, ext, width, height in prepared
    ]
    count = firestore_repo.reference_counts([brand_id]).get(brand_id, 0)
    return {"references": [_reference(r) for r in added], "reference_count": count}


@router.delete("/gd/brands/{brand_id}/references/{ref_id}", status_code=204)
def delete_reference(brand_id: str, ref_id: str, _user: dict = Depends(get_current_user)) -> Response:
    _store_call(firestore_repo.soft_delete_reference, brand_id, ref_id)
    return Response(status_code=204)


@router.delete("/gd/brands/{brand_id}", status_code=204)
def archive_brand(brand_id: str, _admin: dict = Depends(require_admin)) -> Response:
    """Soft archive (admin only): the brand leaves the picker; its doc, files
    and references all stay."""
    if brand_id in BUILTIN_GD_PACK_IDS:
        raise HTTPException(409, "brand_not_editable")
    _store_call(firestore_repo.archive_brand, brand_id)
    registry.refresh()
    return Response(status_code=204)
