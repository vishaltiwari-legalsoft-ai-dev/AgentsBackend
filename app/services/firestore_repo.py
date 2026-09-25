"""Firestore data access (the metadata "Brain").

Collections:
- brands               brand detail objects
- creatives            files linked to a brand (stores GCS URLs, not bytes)
- reference_creatives  user-uploaded reference material
- brand_references     self-serve GD brand references (one doc each)
- users                application accounts

The client is created lazily so the server can boot before GCP is configured.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from google.cloud import firestore

from app.config import settings

logger = logging.getLogger("agentos.firestore")

_client: Optional[firestore.Client] = None

# Brands change only on ingest, so a short in-process cache keeps the opening
# brand picker instant and avoids re-hitting Firestore mid-conversation.
_BRANDS_TTL_SECONDS = 60.0
_brands_cache: tuple[float, list[dict[str, Any]]] | None = None


def _db() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client(
            project=settings.require("gcp_project_id"),
            database=settings.firestore_database,
        )
    return _client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Brands
# --------------------------------------------------------------------------- #

def list_brands(
    *, use_cache: bool = True, include_archived: bool = False
) -> list[dict[str, Any]]:
    """Every brand, ordered by name. Soft-archived brands (``archived_at`` set)
    are left out unless ``include_archived`` — legacy docs have no such field
    and always count as active. The cache holds the unfiltered list."""
    global _brands_cache
    if use_cache and _brands_cache and (time.monotonic() - _brands_cache[0]) < _BRANDS_TTL_SECONDS:
        brands = _brands_cache[1]
    else:
        docs = _db().collection("brands").order_by("brand_name").stream()
        brands = [doc.to_dict() | {"id": doc.id} for doc in docs]
        _brands_cache = (time.monotonic(), brands)
    if include_archived:
        return brands
    return [b for b in brands if not b.get("archived_at")]


def get_brand(brand_id: str) -> Optional[dict[str, Any]]:
    doc = _db().collection("brands").document(brand_id).get()
    return (doc.to_dict() | {"id": doc.id}) if doc.exists else None


def find_brand_by_name(name: str) -> Optional[dict[str, Any]]:
    query = (
        _db()
        .collection("brands")
        .where(filter=firestore.FieldFilter("brand_name_lower", "==", name.lower()))
        .limit(1)
    )
    for doc in query.stream():
        return doc.to_dict() | {"id": doc.id}
    return None


def _invalidate_brands_cache() -> None:
    global _brands_cache
    _brands_cache = None


def upsert_brand(brand_name: str, brand_metadata: dict[str, Any]) -> dict[str, Any]:
    _invalidate_brands_cache()
    existing = find_brand_by_name(brand_name)
    payload = {
        "brand_name": brand_name,
        "brand_name_lower": brand_name.lower(),
        "brand_metadata": brand_metadata,
    }
    if existing:
        _db().collection("brands").document(existing["id"]).set(payload, merge=True)
        return get_brand(existing["id"]) or (existing | payload)

    brand_id = uuid.uuid4().hex
    payload["created_at"] = _now()
    _db().collection("brands").document(brand_id).set(payload)
    return payload | {"id": brand_id}


def update_brand_metadata(brand_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into brand_metadata; never clobbers other metadata keys
    (Firestore merge=True does a per-key nested merge)."""
    _invalidate_brands_cache()
    _db().collection("brands").document(brand_id).set(
        {"brand_metadata": patch}, merge=True)
    return get_brand(brand_id) or {}


# --------------------------------------------------------------------------- #
# Self-serve Graphics Designer brands (company-wide; any signed-in member)
# --------------------------------------------------------------------------- #
#
# Owner decisions (2026-09-25): brands are ONE company-wide set — no per-user
# scoping, by decision, not by omission; any signed-in member may create and
# edit; archive is soft and nothing here ever hard-deletes.
#
# One string, three roles: a self-serve brand's slug is its Firestore doc id,
# its GD pack id (``brand_metadata.gd_spec.id``, forced here) and the
# ``brand_id`` on each of its ``brand_references``. It is derived ONCE, at
# create, by ``gd_spec_builder._slug`` — the helper enrichment already uses for
# pack ids — and never changes on rename. The legacy reference index keys
# brands by ``reference_library.brand_slug`` (separators dropped: ``legalsoft``);
# ``_slug_key`` compares in that compacted form so the two agree.
#
# Collections / docs:
#   brands/<slug>                     source="user" brand doc (legacy docs keep uuid ids)
#   meta/brands                       {version:int} — bumped on EVERY self-serve brand write
#   brand_references/<slug>__<hash>   one doc per uploaded reference
#   brand_reference_counts/<slug>     {active:int} — the cap counter, same txn as the ref

#: The GD packs that ship in code (``registry.py`` + ``templated_brands.py``).
#: A constant rather than an import: the GD package root imports the whole
#: pipeline. ``tests/test_dynamic_brands.py`` pins this against the registry.
BUILTIN_GD_PACK_IDS: tuple[str, ...] = ("legalsoft", "medvirtual", "remote_attorneys")

BRAND_REFERENCE_CAP = 200
_BRANDS_META_DOC = ("meta", "brands")
_REFERENCES_COLLECTION = "brand_references"
_REFERENCE_COUNTS_COLLECTION = "brand_reference_counts"
_KIT_LIST_KEYS = ("primary_colors", "secondary_colors", "accent_colors")
_KIT_KEYS = frozenset(_KIT_LIST_KEYS + ("fonts", "tone_of_voice", "enrichment", "gd_spec"))
_ENRICHMENT_FILE_KEYS = ("logo_files", "font_files", "guideline_files")
_ASSET_KIND_KEY = {"logo": "logo_files", "font": "font_files", "guidelines": "guideline_files"}
_HEX_RE = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")


class BrandStoreError(Exception):
    """Base for the self-serve brand store's refusals (the router maps to 4xx)."""


class BrandExists(BrandStoreError):
    """The slug (or name) is taken by a built-in pack or an existing brand — 409."""


class BrandNotFound(BrandStoreError):
    """No brand doc (or no such reference) under that id — 404."""


class BrandNotEditable(BrandStoreError):
    """Archived, built-in, or CLI-ingested: not writable through self-serve — 409."""


class ReferenceCapReached(BrandStoreError):
    """The brand already holds ``BRAND_REFERENCE_CAP`` active references — 409."""


def brand_slug_for(name: str) -> str:
    """The canonical self-serve slug (``Acme Co`` -> ``acme-co``)."""
    from app.services.gd_spec_builder import _slug  # pure module, no GCP imports

    return _slug(name or "")


def _slug_key(value: str) -> str:
    """Separator-free comparison key: ``remote_attorneys``, ``remote-attorneys``
    and the legacy index's ``remoteattorneys`` are the same brand."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _transact(fn: Callable[[Any], Any]) -> Any:
    """Run ``fn(txn)`` in a Firestore transaction (retried on contention, so
    ``fn`` reads before it writes and is safe to re-run). The one seam the
    tests replace — every self-serve write goes through here."""
    return firestore.transactional(fn)(_db().transaction())


def _meta_ref():
    return _db().collection(_BRANDS_META_DOC[0]).document(_BRANDS_META_DOC[1])


def _bump_brands_version(txn, now: str) -> None:
    """Every self-serve brand write bumps ``meta/brands.version`` in the SAME
    transaction, so an instance compares one small doc instead of racing a TTL."""
    txn.set(_meta_ref(), {"version": firestore.Increment(1), "updated_at": now}, merge=True)


def brands_version() -> int:
    """Current brand-set version (0 before the first self-serve write)."""
    snap = _meta_ref().get()
    return int((snap.to_dict() or {}).get("version", 0)) if snap.exists else 0


def _clean_hex_list(key: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key} must be a list of hex colour strings")
    bad = [v for v in value if not _HEX_RE.match(v.strip())]
    if bad:
        raise ValueError(f"{key} has values that are not #RGB/#RRGGBB hex: {bad}")
    return [v.strip().upper() for v in value]


def _clean_kit(kit: dict[str, Any] | None) -> dict[str, Any]:
    """Validate a kit / patch into the ``brand_metadata`` shape the GD pipeline
    reads. Unknown keys are refused rather than silently stored."""
    kit = dict(kit or {})
    unknown = sorted(set(kit) - _KIT_KEYS)
    if unknown:
        raise ValueError(f"unknown brand kit fields: {unknown}")
    out: dict[str, Any] = {}
    for key in _KIT_LIST_KEYS:
        if key in kit:
            out[key] = _clean_hex_list(key, kit[key])
    if "fonts" in kit:
        fonts = kit["fonts"]
        if not isinstance(fonts, list) or not all(isinstance(f, str) and f.strip() for f in fonts):
            raise ValueError("fonts must be a list of non-empty strings")
        out["fonts"] = [f.strip() for f in fonts]
    if "tone_of_voice" in kit:
        tone = kit["tone_of_voice"]
        if tone is not None and not isinstance(tone, str):
            raise ValueError("tone_of_voice must be a string")
        out["tone_of_voice"] = tone
    if "enrichment" in kit:
        enr = kit["enrichment"]
        if not isinstance(enr, dict):
            raise ValueError("enrichment must be an object")
        out["enrichment"] = dict(enr)
    if "gd_spec" in kit:
        spec = kit["gd_spec"]
        if spec is not None and not isinstance(spec, dict):
            raise ValueError("gd_spec must be an object")
        out["gd_spec"] = dict(spec) if spec else None
    return out


def _pin_gd_spec(meta: dict[str, Any], slug: str, name: str) -> None:
    """A self-serve brand's pack id IS its slug — whatever the caller sent."""
    spec = meta.get("gd_spec")
    if spec:
        meta["gd_spec"] = dict(spec) | {"id": slug, "firestore_brand_id": slug, "name": name}


def create_brand(name: str, kit: dict[str, Any] | None, *, created_by: str) -> dict[str, Any]:
    """Create a company-wide brand; its slug is derived here, once.

    Raises ``BrandExists`` if the slug (compared separator-free) matches a
    built-in pack, or a brand doc with that id or that exact name exists — the
    latter two checked inside the transaction that writes, so two people
    creating "Acme" at once get one brand and one 409, never two brands.
    """
    display = (name or "").strip()
    slug = brand_slug_for(display)
    if not display or not slug:
        raise ValueError("a brand name needs at least one letter or digit")
    if _slug_key(slug) in {_slug_key(b) for b in BUILTIN_GD_PACK_IDS}:
        raise BrandExists(f"{display!r} collides with a built-in brand pack")

    meta = _clean_kit(kit)
    enrichment = {"palette": {}} | (meta.get("enrichment") or {})
    for key in _ENRICHMENT_FILE_KEYS:
        enrichment[key] = list(enrichment.get(key) or [])
    meta["enrichment"] = enrichment
    for key in _KIT_LIST_KEYS + ("fonts",):
        meta.setdefault(key, [])
    meta.setdefault("tone_of_voice", None)
    meta.setdefault("gd_spec", None)
    _pin_gd_spec(meta, slug, display)

    ref = _db().collection("brands").document(slug)
    same_name = (
        _db().collection("brands")
        .where(filter=firestore.FieldFilter("brand_name_lower", "==", display.lower()))
        .limit(1)
    )

    def _apply(txn) -> dict[str, Any]:
        if ref.get(transaction=txn).exists:
            raise BrandExists(f"a brand with id {slug!r} already exists")
        if any(True for _ in same_name.stream(transaction=txn)):
            raise BrandExists(f"a brand named {display!r} already exists")
        now = _now()
        doc = {
            "name": display,
            "slug": slug,
            # The legacy readers order/look up by these two; without
            # ``brand_name``, ``order_by("brand_name")`` silently drops the doc.
            "brand_name": display,
            "brand_name_lower": display.lower(),
            "created_at": now,
            "updated_at": now,
            "created_by": (created_by or "").lower(),
            "archived_at": None,
            "source": "user",
            "logo_uri": None,
            "brand_metadata": meta,
        }
        txn.set(ref, doc)
        _bump_brands_version(txn, now)
        return doc | {"id": slug}

    result = _transact(_apply)
    _invalidate_brands_cache()
    return result


def _require_user_brand(snap, brand_id: str) -> dict[str, Any]:
    if not snap.exists:
        raise BrandNotFound(f"no brand {brand_id!r}")
    doc = snap.to_dict() or {}
    if brand_id in BUILTIN_GD_PACK_IDS or doc.get("source") != "user":
        raise BrandNotEditable(f"{brand_id!r} is not a self-serve brand")
    if doc.get("archived_at"):
        raise BrandNotEditable(f"{brand_id!r} is archived")
    return doc


def _merge_meta(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Top-level kit keys replace; ``enrichment`` merges key-by-key."""
    merged = dict(current)
    for key, value in patch.items():
        if key == "enrichment":
            merged["enrichment"] = dict(current.get("enrichment") or {}) | value
        else:
            merged[key] = value
    return merged


def update_brand(brand_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a kit patch (and optionally a new display ``name``) into a
    self-serve brand. The slug/id never changes. Refuses archived, built-in and
    CLI-ingested brands with ``BrandNotEditable``."""
    patch = dict(patch or {})
    new_name = patch.pop("name", None)
    if new_name is not None and not str(new_name).strip():
        raise ValueError("a brand name cannot be blank")
    meta_patch = _clean_kit(patch)
    ref = _db().collection("brands").document(brand_id)

    def _apply(txn) -> dict[str, Any]:
        doc = _require_user_brand(ref.get(transaction=txn), brand_id)
        name = str(new_name).strip() if new_name is not None else doc.get("name")
        meta = _merge_meta(doc.get("brand_metadata") or {}, meta_patch)
        _pin_gd_spec(meta, brand_id, name)
        now = _now()
        fields: dict[str, Any] = {"brand_metadata": meta, "updated_at": now}
        if new_name is not None:
            fields |= {"name": name, "brand_name": name, "brand_name_lower": name.lower()}
        txn.update(ref, fields)
        _bump_brands_version(txn, now)
        return doc | fields | {"id": brand_id}

    result = _transact(_apply)
    _invalidate_brands_cache()
    return result


def archive_brand(brand_id: str) -> dict[str, Any]:
    """Soft-archive: sets ``archived_at``. The doc, its assets and references
    all stay; ``list_brands`` stops returning it. Never hard-deletes."""
    ref = _db().collection("brands").document(brand_id)

    def _apply(txn) -> dict[str, Any]:
        doc = _require_user_brand(ref.get(transaction=txn), brand_id)
        now = _now()
        fields = {"archived_at": now, "updated_at": now}
        txn.update(ref, fields)
        _bump_brands_version(txn, now)
        return doc | fields | {"id": brand_id}

    result = _transact(_apply)
    _invalidate_brands_cache()
    return result


def add_brand_asset(brand_id: str, kind: str, data: bytes, ext: str) -> dict[str, Any]:
    """Upload a kit file (``logo`` | ``font`` | ``guidelines``) to
    ``brands/<id>/<kind>s/<sha256[:16]>.<ext>`` and record its ``gs://`` URI
    under ``brand_metadata.enrichment.{logo,font,guideline}_files``; a logo
    also becomes ``logo_uri``. The same bytes twice is one entry.

    Font caveat for whoever builds ``gd_spec``: ``gd_brand_source`` matches
    ``font_variants[].file`` to these URIs by BASENAME, i.e. the hash name.
    """
    from app.services import storage

    if kind not in _ASSET_KIND_KEY:
        raise ValueError(f"unknown brand asset kind {kind!r}")
    ref = _db().collection("brands").document(brand_id)
    _require_user_brand(ref.get(), brand_id)  # refuse before any bytes move
    uri = storage.put_brand_asset(brand_id, kind, data, ext)
    list_key = _ASSET_KIND_KEY[kind]

    def _apply(txn) -> dict[str, Any]:
        doc = _require_user_brand(ref.get(transaction=txn), brand_id)
        meta = dict(doc.get("brand_metadata") or {})
        enrichment = dict(meta.get("enrichment") or {})
        files = list(enrichment.get(list_key) or [])
        if uri not in files:
            files.append(uri)
        enrichment[list_key] = files
        meta["enrichment"] = enrichment
        now = _now()
        fields: dict[str, Any] = {"brand_metadata": meta, "updated_at": now}
        if kind == "logo":
            fields["logo_uri"] = uri
        txn.update(ref, fields)
        _bump_brands_version(txn, now)
        return {"brand_id": brand_id, "kind": kind, "uri": uri}

    result = _transact(_apply)
    _invalidate_brands_cache()
    return result


def list_brand_assets(brand_id: str) -> dict[str, Any]:
    """The brand's recorded kit files, read from its Firestore doc (1 read —
    the doc is the record; a bucket listing would also show orphans)."""
    brand = get_brand(brand_id)
    if brand is None:
        raise BrandNotFound(f"no brand {brand_id!r}")
    enrichment = (brand.get("brand_metadata") or {}).get("enrichment") or {}
    return {
        "brand_id": brand_id,
        "logo_uri": brand.get("logo_uri"),
        **{key: list(enrichment.get(key) or []) for key in _ENRICHMENT_FILE_KEYS},
    }


# --- References: one doc per reference ---------------------------------------

def _reference_doc_id(brand_id: str, ref_id: str) -> str:
    return f"{brand_id}__{ref_id}"


def _require_reference_target(brand_id: str, snap) -> None:
    """References attach to a built-in pack id or an active self-serve brand."""
    if brand_id in BUILTIN_GD_PACK_IDS:
        return
    _require_user_brand(snap, brand_id)


def _active_count(snap) -> int:
    return int(((snap.to_dict() or {}) if snap.exists else {}).get("active", 0))


def add_reference(
    brand_id: str,
    *,
    data: bytes,
    ext: str,
    kind: str,
    uploaded_by: str,
    width: int | None = None,
    height: int | None = None,
    note: str = "",
    creative_type: str | None = None,
) -> dict[str, Any]:
    """Store one reference file and its doc; returns the doc.

    ``ref_id`` is the content hash, so the same bytes uploaded twice are one
    reference (re-adding a soft-deleted one revives it). Raises
    ``ReferenceCapReached`` at ``BRAND_REFERENCE_CAP`` active references —
    enforced on a counter doc written in the same transaction as the
    reference, so parallel uploads cannot overshoot. Legacy Drive-synced
    references do not count toward the cap.
    """
    from app.services import storage

    if kind not in ("creative", "reference"):
        raise ValueError("kind must be 'creative' or 'reference'")
    ref_id = storage.content_hash(data)
    object_path = storage.reference_object_path(brand_id, data, ext)  # validates ext
    brand_ref = _db().collection("brands").document(brand_id)
    counter_ref = _db().collection(_REFERENCE_COUNTS_COLLECTION).document(brand_id)
    doc_ref = _db().collection(_REFERENCES_COLLECTION).document(_reference_doc_id(brand_id, ref_id))

    def _refuse_if_full(existing_snap, count_snap) -> dict[str, Any] | None:
        current = (existing_snap.to_dict() or {}) if existing_snap.exists else None
        if current is not None and not current.get("deleted_at"):
            return current  # same bytes, already active
        if _active_count(count_snap) >= BRAND_REFERENCE_CAP:
            raise ReferenceCapReached(
                f"{brand_id!r} already has {BRAND_REFERENCE_CAP} references — remove one first")
        return None

    # Pre-flight (non-transactional) so an unknown or full brand never uploads.
    if brand_id not in BUILTIN_GD_PACK_IDS:
        _require_reference_target(brand_id, brand_ref.get())
    _refuse_if_full(doc_ref.get(), counter_ref.get())
    gs_uri = storage.put_reference_file(brand_id, data, ext)

    def _apply(txn) -> dict[str, Any]:
        if brand_id not in BUILTIN_GD_PACK_IDS:
            _require_reference_target(brand_id, brand_ref.get(transaction=txn))
        active = _refuse_if_full(doc_ref.get(transaction=txn), counter_ref.get(transaction=txn))
        if active is not None:
            return active | {"id": doc_ref.id}
        now = _now()
        doc = {
            "brand_id": brand_id,
            "ref_id": ref_id,
            "kind": kind,
            "creative_type": creative_type,
            "object_path": object_path,
            "gs_uri": gs_uri,
            "content_type": storage.content_type_for_ext(ext),
            "width": width,
            "height": height,
            "note": note or "",
            "uploaded_by": (uploaded_by or "").lower(),
            "created_at": now,
            "deleted_at": None,
        }
        txn.set(doc_ref, doc)
        txn.set(counter_ref, {"active": firestore.Increment(1), "updated_at": now}, merge=True)
        return doc | {"id": doc_ref.id}

    return _transact(_apply)


#: Same back-off as the runs index fallback: once refused, don't retry the
#: ordered read for ``_RUNS_INDEX_RETRY_SECONDS``.
_references_index_missing_at: float | None = None


def list_references(brand_id: str, limit: int = BRAND_REFERENCE_CAP) -> list[dict[str, Any]]:
    """Active references for one brand, newest first.

    Needs the composite ``brand_references (brand_id ASC, deleted_at ASC,
    created_at DESC)`` recorded in ``firestore.indexes.json``. Until it is
    built, the refused query falls back to ``brand_id ==`` alone, filtered and
    sorted here — correct, but it also reads the soft-deleted docs.
    """
    global _references_index_missing_at
    scoped = _db().collection(_REFERENCES_COLLECTION).where(
        filter=firestore.FieldFilter("brand_id", "==", brand_id))
    now = time.monotonic()
    known_missing = (
        _references_index_missing_at is not None
        and (now - _references_index_missing_at) < _RUNS_INDEX_RETRY_SECONDS
    )
    if not known_missing:
        try:
            docs = (
                scoped.where(filter=firestore.FieldFilter("deleted_at", "==", None))
                .order_by("created_at", direction=firestore.Query.DESCENDING)
                .limit(limit)
                .stream()
            )
            rows = [doc.to_dict() | {"id": doc.id} for doc in docs]
            _references_index_missing_at = None
            return rows
        except Exception:
            _references_index_missing_at = now
            logger.warning(
                "brand_references: ordered read refused — build the composite "
                "(brand_id ASC, deleted_at ASC, created_at DESC). Filtering in process.",
                exc_info=True,
            )
    rows = [
        data | {"id": doc.id}
        for doc in scoped.stream()
        if not (data := doc.to_dict() or {}).get("deleted_at")
    ]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows[:limit]


def reference_counts(brand_ids: list[str]) -> dict[str, int]:
    """Active (uploaded) reference count per brand, in ONE batched read of the
    counter docs — the brand picker lists every brand and must not pay a
    query per row. A brand with no counter doc counts 0."""
    ids = [b for b in dict.fromkeys(brand_ids) if b]
    if not ids:
        return {}
    refs = [_db().collection(_REFERENCE_COUNTS_COLLECTION).document(b) for b in ids]
    out = {b: 0 for b in ids}
    for snap in _db().get_all(refs):
        if snap.exists:
            out[snap.id] = _active_count(snap)
    return out


def soft_delete_reference(brand_id: str, ref_id: str) -> dict[str, Any]:
    """Mark one reference deleted and release its cap slot. The GCS object
    and the doc stay. Deleting an already-deleted reference is a no-op."""
    doc_ref = _db().collection(_REFERENCES_COLLECTION).document(_reference_doc_id(brand_id, ref_id))
    counter_ref = _db().collection(_REFERENCE_COUNTS_COLLECTION).document(brand_id)

    def _apply(txn) -> dict[str, Any]:
        snap = doc_ref.get(transaction=txn)
        doc = (snap.to_dict() or {}) if snap.exists else None
        if doc is None or doc.get("brand_id") != brand_id:
            raise BrandNotFound(f"no reference {ref_id!r} for brand {brand_id!r}")
        if doc.get("deleted_at"):
            return doc | {"id": doc_ref.id}
        now = _now()
        txn.update(doc_ref, {"deleted_at": now})
        txn.set(counter_ref, {"active": firestore.Increment(-1), "updated_at": now}, merge=True)
        return doc | {"deleted_at": now, "id": doc_ref.id}

    return _transact(_apply)


def _legacy_reference_records() -> list[dict[str, Any]]:
    """The Drive-synced ``reference_library/reference_index.json`` records
    (GCS copy). ``[]`` when GCS is unconfigured or the index is absent."""
    from app.services import storage

    if not storage.is_configured():
        return []
    try:
        raw = storage.read_reference_index()
        return list(json.loads(raw.decode("utf-8")).get("records", [])) if raw else []
    except Exception:  # noqa: BLE001 - legacy refs are additive; Firestore refs still serve
        logger.warning("could not read the legacy reference index", exc_info=True)
        return []


def references_for_brand(
    brand_id: str, *, legacy_records: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Every reference generation may use for one brand: its Firestore docs
    (newest first), then the legacy index's records for the same brand.

    Firestore docs also carry the legacy record keys (``file_name``,
    ``creative_type``, ``tags``, ``palette``, ``ingested_at``,
    ``source="upload"``, plus ``gs_uri``) so ``reference_library.retrieve`` and
    ``load_reference_bytes`` accept both. Legacy records pass through untouched,
    matched separator-free (``remote_attorneys`` == ``remoteattorneys``).

    ``legacy_records``: pass ``reference_library.load_index(...)`` to include
    a local-disk index (dev); omitted, the GCS copy is read.
    """
    key = _slug_key(brand_id)
    uploaded = []
    for doc in list_references(brand_id):
        uploaded.append(doc | {
            "file_name": (doc.get("object_path") or "").rsplit("/", 1)[-1],
            "creative_type": doc.get("creative_type") or doc.get("kind"),
            "tags": [t for t in re.split(r"[^a-z0-9]+", (doc.get("note") or "").lower())
                     if len(t) > 2],
            "palette": [],
            "ingested_at": doc.get("created_at"),
            "source": "upload",
        })
    seen = {r["gs_uri"] for r in uploaded if r.get("gs_uri")}
    legacy = legacy_records if legacy_records is not None else _legacy_reference_records()
    return uploaded + [
        r for r in legacy
        if _slug_key(str(r.get("brand_id", ""))) == key and r.get("gs_uri") not in seen
    ]


# --------------------------------------------------------------------------- #
# Creatives
# --------------------------------------------------------------------------- #

def list_creatives_by_brand(brand_id: str, limit: int = 50) -> list[dict[str, Any]]:
    docs = (
        _db()
        .collection("creatives")
        .where(filter=firestore.FieldFilter("brand_id", "==", brand_id))
        .limit(limit)
        .stream()
    )
    return [doc.to_dict() | {"id": doc.id} for doc in docs]


_LOGO_IMAGE_EXTS = (".png", ".svg", ".jpg", ".jpeg", ".webp")


def find_brand_logo(brand_id: str) -> Optional[dict[str, Any]]:
    """Best-guess the brand's logo from its ingested creatives (None if unknown).

    Logos aren't explicitly tagged, so rank the human-curated (non-AgentOS) image
    assets: a "logo" in the file name wins, then SVG, then PNG (usually
    transparent), then any other image. Returns the creative record (carrying the
    ``gs://`` ``file_url``) so callers can sign it or download the bytes.
    """
    if not brand_id:
        return None
    candidates = [
        c
        for c in list_creatives_by_brand(brand_id, limit=500)
        if str(c.get("file_url", "")).startswith("gs://")
        and (c.get("creative_metadata") or {}).get("author", "") != "AgentOS"
        and _is_image_asset(c)
    ]
    if not candidates:
        return None
    return max(candidates, key=_logo_score)


def _is_image_asset(creative: dict[str, Any]) -> bool:
    name = (creative.get("file_name") or "").lower()
    ftype = (creative.get("file_type") or "").lower()
    return ftype.startswith("image/") or name.endswith(_LOGO_IMAGE_EXTS)


def _logo_score(creative: dict[str, Any]) -> int:
    name = (creative.get("file_name") or "").lower()
    ftype = (creative.get("file_type") or "").lower()
    score = 0
    if "logo" in name:
        score += 100
    if name.endswith(".svg") or ftype == "image/svg+xml":
        score += 20
    elif name.endswith(".png") or ftype == "image/png":
        score += 10
    return score


def count_creatives_by_brand(brand_id: str) -> int:
    """Return the real total of creatives for a brand (no limit)."""
    query = (
        _db()
        .collection("creatives")
        .where(filter=firestore.FieldFilter("brand_id", "==", brand_id))
        .count()
    )
    result = query.get()
    # `count()` returns a list of aggregation results; value is on the first.
    return int(result[0][0].value) if result and result[0] else 0


def _delete_collection(collection_name: str) -> int:
    """Delete every document in a Firestore collection. Returns deleted count."""
    deleted = 0
    batch = _db().batch()
    batch_size = 0
    for doc in _db().collection(collection_name).stream():
        batch.delete(doc.reference)
        batch_size += 1
        deleted += 1
        if batch_size >= 400:
            batch.commit()
            batch = _db().batch()
            batch_size = 0
    if batch_size > 0:
        batch.commit()
    return deleted


def delete_all_brands() -> int:
    """Wipe the entire brands collection."""
    _invalidate_brands_cache()
    return _delete_collection("brands")


def delete_all_creatives() -> int:
    """Wipe the entire creatives collection."""
    return _delete_collection("creatives")


def delete_ingested_creatives(brand_id: str) -> int:
    """Delete creatives previously written by the ingestion script (Marketing
    Team author), preserving AI-generated ones. Returns deleted count.
    Used to make re-running `python -m app.ingest` idempotent."""
    query = (
        _db()
        .collection("creatives")
        .where(filter=firestore.FieldFilter("brand_id", "==", brand_id))
        .where(
            filter=firestore.FieldFilter(
                "creative_metadata.author", "==", "Marketing Team"
            )
        )
    )
    deleted = 0
    batch = _db().batch()
    batch_size = 0
    for doc in query.stream():
        batch.delete(doc.reference)
        batch_size += 1
        deleted += 1
        if batch_size >= 400:
            batch.commit()
            batch = _db().batch()
            batch_size = 0
    if batch_size > 0:
        batch.commit()
    return deleted


def create_creative(
    brand_id: str,
    file_name: str,
    file_type: str,
    file_url: str,
    creative_metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    creative_id = uuid.uuid4().hex
    payload = {
        "brand_id": brand_id,
        "file_name": file_name,
        "file_type": file_type,
        "file_url": file_url,
        "creative_metadata": creative_metadata or {},
        "created_at": _now(),
    }
    _db().collection("creatives").document(creative_id).set(payload)
    return payload | {"id": creative_id}


# --------------------------------------------------------------------------- #
# Reference creatives (user uploads)
# --------------------------------------------------------------------------- #

def create_reference(user_id: str, file_name: str, file_url: str) -> dict[str, Any]:
    asset_id = uuid.uuid4().hex
    payload = {
        "user_id": user_id,
        "file_name": file_name,
        "file_url": file_url,
        "upload_timestamp": _now(),
    }
    _db().collection("reference_creatives").document(asset_id).set(payload)
    return payload | {"asset_id": asset_id}


def list_references_by_user(user_id: str) -> list[dict[str, Any]]:
    docs = (
        _db()
        .collection("reference_creatives")
        .where(filter=firestore.FieldFilter("user_id", "==", user_id))
        .stream()
    )
    return [doc.to_dict() | {"asset_id": doc.id} for doc in docs]


# --------------------------------------------------------------------------- #
# Users (Google sign-in)
# --------------------------------------------------------------------------- #

def get_user_by_email(email: str) -> Optional[dict[str, Any]]:
    query = (
        _db()
        .collection("users")
        .where(filter=firestore.FieldFilter("email", "==", email.lower()))
        .limit(1)
    )
    for doc in query.stream():
        return doc.to_dict() | {"id": doc.id}
    return None


def get_users_by_ids(user_ids: list[str]) -> dict[str, dict[str, Any]]:
    """The user documents for ``user_ids``, keyed by id, in ONE batched read.

    An id with no document (the user was deleted) is simply absent from the
    result — callers treat absence as "no such account", so a missing key
    must mean exactly that and never "the read failed": a failed read raises.
    """
    ids = [uid for uid in dict.fromkeys(str(u) for u in user_ids) if uid]
    if not ids:
        return {}
    users = _db().collection("users")
    out: dict[str, dict[str, Any]] = {}
    for snap in _db().get_all([users.document(uid) for uid in ids]):
        if snap.exists:
            out[snap.id] = (snap.to_dict() or {}) | {"id": snap.id}
    return out


def get_or_create_google_user(
    email: str, name: str, picture: str, google_sub: str
) -> dict[str, Any]:
    """Look up a user by email, creating one on first Google sign-in.

    Always refreshes last_login so the admin directory shows recency.
    """
    existing = get_user_by_email(email)
    if existing:
        _db().collection("users").document(existing["id"]).set(
            {"last_login": _now(), "name": name, "picture": picture}, merge=True
        )
        return existing | {"name": name, "picture": picture}

    user_id = uuid.uuid4().hex
    payload = {
        "email": email.lower(),
        "name": name,
        "picture": picture,
        "google_sub": google_sub,
        "provider": "google",
        "created_at": _now(),
        "last_login": _now(),
    }
    _db().collection("users").document(user_id).set(payload)
    return payload | {"id": user_id}


def list_users() -> list[dict[str, Any]]:
    docs = _db().collection("users").stream()
    users = [doc.to_dict() | {"id": doc.id} for doc in docs]
    users.sort(key=lambda u: u.get("created_at", ""), reverse=True)
    return users


# NOTE: The chat-agent "conversations" collection lost its last writer when the
# V1 chat rail was removed; its accessors are gone too. The collection name
# stays in TELEMETRY_COLLECTIONS so the admin purge can still clear old docs.


# --------------------------------------------------------------------------- #
# Analytics (creative request events)
# --------------------------------------------------------------------------- #

def log_usage_event(
    user_id: str,
    email: str,
    agent_id: str,
    category: str,
    action: str,
    *,
    count: int = 1,
    brand: Optional[str] = None,
    engine: Optional[str] = None,
) -> None:
    """Record one usage event for the per-user Home dashboard + admin analytics.

    ``action`` is "session" (a run/conversation was started — the per-agent tile
    count) or "generate" (creatives were produced — ``count`` is how many). One
    document per event keeps the model flexible; the dashboard reads a per-user,
    date-windowed slice and aggregates in Python. Logging must never break the
    request it accompanies, so Firestore errors are swallowed.
    """
    now = datetime.now(timezone.utc)
    event_id = uuid.uuid4().hex
    try:
        _db().collection("creative_events").document(event_id).set(
            {
                "user_id": user_id,
                "email": email,
                "agent_id": agent_id,
                "category": category,
                "action": action,
                "count": int(count),
                "brand": brand,
                "engine": engine,
                "created_at": now.isoformat(),
                "day": now.strftime("%Y-%m-%d"),
                "year_month": now.strftime("%Y-%m"),
            }
        )
    except Exception:  # analytics is best-effort — never fail the user's action
        pass


def list_creative_events(limit: int = 5000) -> list[dict[str, Any]]:
    docs = _db().collection("creative_events").limit(limit).stream()
    return [doc.to_dict() for doc in docs]


# --------------------------------------------------------------------------- #
# Run tracking — Table 1 (per-agent) + Table 2 (master "runs")
# --------------------------------------------------------------------------- #
# Two complementary records the admin Database panel renders:
#   Table 1  agent_runs__<agent_id>  — one row per run for THAT agent, carrying
#            the per-stage status (A/B/C/D…) and a creative summary. A new
#            collection is created automatically the first time a new agent runs.
#   Table 2  runs                    — one master row per run across ALL agents:
#            overall run status, brand, summary and the produced assets.
# Both rows share the run id and are updated as the run progresses. All writes
# are best-effort so tracking never breaks a generation.

# Status slots for Table 1 (GD uses A–D; longer pipelines extend into e/f/…).
STAGE_SLOTS = ("a", "b", "c", "d", "e", "f", "g", "h")

# Superseded telemetry the admin "purge" clears. Operational collections
# (users, app_config, brands, creatives, gd_runs, reference_creatives) are never
# touched here.
TELEMETRY_COLLECTIONS = ("creative_events", "sessions", "requests", "conversations")


def new_session_id() -> str:
    """A fresh session id, baked into the JWT at login and referenced by runs."""
    return uuid.uuid4().hex


def agent_runs_collection(agent_id: str) -> str:
    """Table-1 collection name for an agent (one collection per agent)."""
    return f"agent_runs__{agent_id}"


def list_agent_run_collections() -> list[str] | None:
    """Discover the per-agent Table-1 collections that currently exist.

    Returns ``[]`` when the database genuinely holds none, or ``None`` when the
    listing could not be read (Firestore unreachable/unconfigured) — same
    contract as :func:`count_collection`, so "couldn't connect" is never
    disguised as "there are no agent tables".
    """
    try:
        return sorted(c.id for c in _db().collections() if c.id.startswith("agent_runs__"))
    except Exception:
        logger.warning("could not list agent_runs__ collections", exc_info=True)
        return None


def start_run(
    *,
    run_id: str,
    agent_id: str,
    agent_name: str,
    user_id: str,
    user: str,
    session_id: str = "",
    timezone: str = "UTC",
    brand: Optional[str] = None,
    brand_id: Optional[str] = None,
    stages: Optional[list[str]] = None,
) -> None:
    """Create the Table-1 (per-agent) and Table-2 (master) rows for a new run."""
    now = _now()
    stages = stages or []
    status = {STAGE_SLOTS[i]: "pending" for i in range(min(len(stages), len(STAGE_SLOTS)))}
    base = {
        "run_id": run_id,
        "date": now[:10],
        "timezone": timezone,
        "created_at": now,
        "session_id": session_id,
        "user_id": user_id,
        "user": user,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "brand": brand,
        "brand_id": brand_id,
        "updated_at": now,
    }
    try:
        _db().collection(agent_runs_collection(agent_id)).document(run_id).set(
            {**base, "stages": stages, "status": status, "creative_summary": ""}
        )
    except Exception:
        pass
    try:
        _db().collection("runs").document(run_id).set(
            {**base, "run_status": "in_progress", "run_summary": "", "assets": []}
        )
    except Exception:
        pass


def update_run(
    *,
    run_id: str,
    agent_id: str,
    stage_index: Optional[int] = None,
    stage_status: Optional[str] = None,
    asset: Optional[dict[str, Any]] = None,
    summary: Optional[str] = None,
    run_status: Optional[str] = None,
) -> None:
    """Advance a run: set one stage's status (Table 1) and append an asset / set
    the overall status + summary (Table 2)."""
    now = _now()
    slot = (
        STAGE_SLOTS[stage_index]
        if stage_index is not None and 0 <= stage_index < len(STAGE_SLOTS)
        else None
    )
    try:
        upd: dict[str, Any] = {"updated_at": now}
        if slot and stage_status:
            upd[f"status.{slot}"] = stage_status
        if summary is not None:
            upd["creative_summary"] = summary
        _db().collection(agent_runs_collection(agent_id)).document(run_id).update(upd)
    except Exception:
        pass
    try:
        upd2: dict[str, Any] = {"updated_at": now}
        if run_status:
            upd2["run_status"] = run_status
        if summary is not None:
            upd2["run_summary"] = summary
        if asset:
            upd2["assets"] = firestore.ArrayUnion([asset])
        _db().collection("runs").document(run_id).update(upd2)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Image library (admin-only gallery of completed Graphics Designer runs)
# --------------------------------------------------------------------------- #
# One document per COMPLETED run (doc id = run id, so a re-approved Stage 4
# simply refreshes the same entry). The final creative's bytes live in GCS
# (``generated/gallery/…``); this collection stores only the pointer + context
# the admin gallery renders. Writes are best-effort — archiving must never
# break the user's approval request.

def upsert_gallery_image(item: dict[str, Any]) -> None:
    """Create/refresh the image-library entry for a completed run."""
    run_id = str(item.get("run_id") or "")
    if not run_id:
        return
    try:
        _db().collection("image_library").document(run_id).set(
            {**item, "updated_at": _now()}, merge=True
        )
    except Exception:  # archiving is best-effort — never fail the approval
        pass


def list_gallery_images(limit: int = 200) -> list[dict[str, Any]] | None:
    """Image-library entries, newest completion first.

    ``[]`` means the library is genuinely empty; ``None`` means the read failed
    (see :func:`count_collection` for why the two are kept apart)."""
    try:
        docs = (
            _db()
            .collection("image_library")
            .order_by("completed_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        return [doc.to_dict() | {"id": doc.id} for doc in docs]
    except Exception:
        logger.warning("could not read the image library", exc_info=True)
        return None


def get_gallery_image(run_id: str) -> Optional[dict[str, Any]]:
    try:
        doc = _db().collection("image_library").document(run_id).get()
        return (doc.to_dict() | {"id": doc.id}) if doc.exists else None
    except Exception:
        return None


def purge_telemetry() -> dict[str, int]:
    """Delete the superseded telemetry collections (Tables 1/2 replace them).
    Operational collections are never touched. Returns {collection: deleted}
    (-1 on error)."""
    result: dict[str, int] = {}
    for name in TELEMETRY_COLLECTIONS:
        try:
            result[name] = _delete_collection(name)
        except Exception:
            result[name] = -1
    return result


def list_usage_events(
    user_id: Optional[str], since_iso: str, limit: int = 10000
) -> list[dict[str, Any]] | None:
    """Usage events at/after ``since_iso``. Pass ``user_id`` for one user's data
    (the per-user dashboard) or ``None`` for everyone (creator all-users view).

    The ``user_id``-filtered query needs a composite index on
    ``(user_id ASC, created_at ASC)`` — Firestore prints a one-click link to
    create it the first time the query runs.
    """
    try:
        col = _db().collection("creative_events")
        query = col.where(
            filter=firestore.FieldFilter("created_at", ">=", since_iso)
        )
        if user_id is not None:
            query = col.where(
                filter=firestore.FieldFilter("user_id", "==", user_id)
            ).where(filter=firestore.FieldFilter("created_at", ">=", since_iso))
        return [doc.to_dict() for doc in query.limit(limit).stream()]
    except Exception:
        # ``[]`` here rendered a Firestore outage (or a missing composite index)
        # as "you did nothing this week". ``None`` = could not read; the caller
        # answers honestly. Same contract as :func:`count_collection`.
        logger.warning("could not read usage events", exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# The record — one caller's runs across every agent
# --------------------------------------------------------------------------- #
# ``runs`` is written by two shapes of the same trail (see run_tracking): an
# append-only row per unit of work, and one row per Graphics Designer run
# updated in place. Both carry ``user_id``, so the record reads the same way for
# either. The admin Database panel browses this collection raw; this pair is the
# *product* read — scoped to the caller, newest first.

def count_runs_for_user(user_id: str) -> int | None:
    """How many runs this caller has ever filed.

    ``None`` when the count could not be read — same contract as
    :func:`count_collection`, so the console can say "we never found out"
    instead of printing a zero that reads as "you have never run anything".
    """
    if not user_id:
        return 0
    try:
        result = (
            _db()
            .collection("runs")
            .where(filter=firestore.FieldFilter("user_id", "==", user_id))
            .count()
            .get()
        )
        return int(result[0][0].value) if result and result[0] else 0
    except Exception:
        logger.warning("could not count runs for a user", exc_info=True)
        return None


#: How many of one caller's rows the unordered fallback will pull before it
#: gives up on being complete. Well above today's whole-collection size, and low
#: enough that a runaway agent cannot turn one page load into a full scan.
_RUNS_SCAN_CAP = 3000

#: When the ordered read was last refused for want of an index, as a monotonic
#: clock reading. Firestore rejects the query *before* running it, so an
#: un-memoised fallback pays a guaranteed failed round-trip on every single page
#: load — measured at ~1s of the 2.3s this endpoint took. Remembering the
#: refusal skips it; re-trying after the interval means creating the index
#: brings the fast path back without a redeploy.
_runs_index_missing_at: float | None = None
_RUNS_INDEX_RETRY_SECONDS = 600.0


def list_runs_for_user(user_id: str, limit: int = 200) -> list[dict[str, Any]] | None:
    """This caller's runs, newest first.

    The ordered form needs a composite index on ``(user_id ASC, created_at
    DESC)``. Until that index exists Firestore refuses the query outright, and
    refusing to show anybody their own record because an index is missing is a
    worse answer than reading the rows and sorting them here — so a refused
    ordered query falls back to an unordered read of the same filter, sorted in
    process. The fallback is capped and logs loudly, because it stops being
    acceptable the moment one caller has more runs than the cap.

    ``None`` means the read itself failed. ``[]`` means this caller has no runs.
    """
    global _runs_index_missing_at
    if not user_id:
        return []
    col = _db().collection("runs")
    scoped = col.where(filter=firestore.FieldFilter("user_id", "==", user_id))

    now = time.monotonic()
    index_known_missing = (
        _runs_index_missing_at is not None
        and (now - _runs_index_missing_at) < _RUNS_INDEX_RETRY_SECONDS
    )
    if not index_known_missing:
        try:
            docs = scoped.order_by(
                "created_at", direction=firestore.Query.DESCENDING
            ).limit(limit).stream()
            rows = [doc.to_dict() | {"id": doc.id} for doc in docs]
            _runs_index_missing_at = None
            return rows
        except Exception:
            _runs_index_missing_at = now
            logger.warning(
                "runs: the ordered read was refused — create the composite index "
                "(user_id ASC, created_at DESC) on `runs`. Sorting in process "
                "meanwhile; not retrying the ordered read for %ss.",
                int(_RUNS_INDEX_RETRY_SECONDS),
                exc_info=True,
            )

    try:
        rows = [doc.to_dict() | {"id": doc.id} for doc in scoped.limit(_RUNS_SCAN_CAP).stream()]
    except Exception:
        logger.warning("could not read the runs record", exc_info=True)
        return None
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows[:limit]


# --------------------------------------------------------------------------- #
# Admin database viewer (read-only inspection of raw collections)
# --------------------------------------------------------------------------- #
# A whitelist the admin "Database" panel may read so the team can *see* the data
# really living in Firestore (visual proof), without needing GCP console access.
# Order here is the order shown in the UI; anything not listed is unreachable.

VIEWABLE_COLLECTIONS: list[dict[str, str]] = [
    {"name": "runs", "label": "Runs (all agents)",
     "description": "Master table: one row per run across every agent — status, brand, summary, assets."},
    {"name": "users", "label": "Users",
     "description": "Registered application accounts (Google sign-in)."},
    {"name": "brands", "label": "Brands",
     "description": "Brand profiles and their metadata."},
    {"name": "creatives", "label": "Creatives",
     "description": "Generated & ingested assets (stores GCS links, not bytes)."},
    {"name": "gd_runs", "label": "Designer runs",
     "description": "Graphics Designer run manifests / live state (cloud storage mode)."},
    {"name": "reference_creatives", "label": "Reference uploads",
     "description": "User-uploaded reference material."},
    {"name": "image_library", "label": "Image library",
     "description": "Final creatives of completed Graphics Designer runs (admin gallery)."},
    {"name": "app_config", "label": "App config",
     "description": "Runtime settings (single global document; secrets masked)."},
]
# Per-agent Table-1 collections (``agent_runs__<id>``) are created on demand and
# discovered dynamically by the viewer — see ``list_agent_run_collections``.
# NOTE: "conversations" is deliberately NOT viewable — chat history is private to
# each user and must not be browsable. The superseded telemetry collections
# (creative_events, sessions, requests) are likewise dropped from the viewer.

_VIEWABLE_NAMES = {c["name"] for c in VIEWABLE_COLLECTIONS}


def is_viewable_collection(name: str) -> bool:
    """Whether ``name`` is on the admin-viewer whitelist (incl. per-agent run tables)."""
    return name in _VIEWABLE_NAMES or name.startswith("agent_runs__")


def count_collection(name: str) -> int | None:
    """Total document count for a collection (server-side aggregation).

    Returns 0 for a genuinely empty collection, or ``None`` when the count could
    not be read (Firestore unreachable/unconfigured). The viewer relies on this
    distinction so "can't connect" is never disguised as "empty".
    """
    try:
        result = _db().collection(name).count().get()
        return int(result[0][0].value) if result and result[0] else 0
    except Exception:
        return None


def list_collection_documents(name: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return up to ``limit`` raw documents from a collection, each carrying its
    document id under ``id``. The caller is responsible for sanitising values."""
    docs = _db().collection(name).limit(limit).stream()
    return [doc.to_dict() | {"id": doc.id} for doc in docs]


# --------------------------------------------------------------------------- #
# App config (admin-editable runtime settings — single document)
# --------------------------------------------------------------------------- #
# A single ``app_config/global`` doc holds admin-set overrides for sensitive
# runtime config (the OpenRouter key + model ids). It lets the Super Admin manage
# these from the UI instead of Cloud Run env vars. Read through a short cache so
# the hot path (every LLM/image call) doesn't hit Firestore each time; writes
# invalidate it. Firestore failures fall back to {} so the app still boots off
# the environment.

_APP_CONFIG_TTL_SECONDS = 30.0
_app_config_cache: tuple[float, dict[str, Any]] | None = None


def get_app_config(*, use_cache: bool = True) -> dict[str, Any]:
    global _app_config_cache
    if (
        use_cache
        and _app_config_cache
        and (time.monotonic() - _app_config_cache[0]) < _APP_CONFIG_TTL_SECONDS
    ):
        return _app_config_cache[1]
    try:
        doc = _db().collection("app_config").document("global").get()
        data = doc.to_dict() if doc.exists else {}
    except Exception:  # Firestore unavailable/unconfigured — fall back to env.
        data = {}
    data = data or {}
    _app_config_cache = (time.monotonic(), data)
    return data


def set_app_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a patch into the global app-config doc and return the fresh state."""
    global _app_config_cache
    _db().collection("app_config").document("global").set(
        {**patch, "updated_at": _now()}, merge=True
    )
    _app_config_cache = None
    return get_app_config(use_cache=False)


def set_agent_config(agent_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Set per-agent model overrides under ``agents.{agent_id}`` and return the
    fresh global config.

    The merge is done explicitly in Python (read → merge → write the whole
    ``agents`` map) rather than relying on Firestore's nested-merge semantics, so
    the behaviour is identical whether or not Firestore is reachable in tests. An
    empty-string value clears that field's override so it reverts to the global /
    environment default.
    """
    current = get_app_config(use_cache=False)
    agents: dict[str, Any] = dict(current.get("agents") or {})
    agent_cfg: dict[str, Any] = dict(agents.get(agent_id) or {})
    for field, value in patch.items():
        if value == "" or value is None:
            agent_cfg.pop(field, None)
        else:
            agent_cfg[field] = value
    agents[agent_id] = agent_cfg
    return set_app_config({"agents": agents})
# --------------------------------------------------------------------------- #
# Inbox Triage (a12) — one connection document per user, one tracking
# document per message
# --------------------------------------------------------------------------- #
# ``inbox_triage/{user_id}`` is the record and the checkpoint for one person's
# Gmail connection: the sealed refresh token, the history checkpoint, the
# backfill cursor, the sheet reference and every number the panel shows
# (counters and a rolling day of fires). One document, keyed by the user id,
# so status is ONE read with no query and no index — and no doc id string is
# ever assembled anywhere but here.
#
# ``inbox_triage_messages/{user_id}__{message_id}`` tracks a message only for
# retries: a row the model could not read is written as ``needs_review`` and
# re-asked on later fires until ``retry_due`` goes false. Every read here is
# equality-only (``user_id ==``, ``retry_due ==``) on purpose: Firestore
# serves that from single-field indexes, so nothing in this section needs an
# entry in ``firestore.indexes.json``. No mail body is ever stored.

INBOX_CONNECTIONS = "inbox_triage"
INBOX_MESSAGES = "inbox_triage_messages"


def get_inbox_connection(user_id: str) -> Optional[dict[str, Any]]:
    doc = _db().collection(INBOX_CONNECTIONS).document(user_id).get()
    return doc.to_dict() if doc.exists else None


def save_inbox_connection(
    user_id: str, patch: dict[str, Any], *, clear: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Merge ``patch`` into the user's connection document and return the fresh
    state. Fields named in ``clear`` are deleted — that is how a disconnect
    removes the sealed token without the caller importing a Firestore sentinel.
    Firestore's merge is per key and nests, so callers write whole sub-maps
    (``gmail``, ``backfill``, ``last_poll``) rather than single leaves."""
    payload: dict[str, Any] = dict(patch)
    for field in clear:
        payload[field] = firestore.DELETE_FIELD
    payload["user_id"] = user_id
    payload["updated_at"] = _now()
    _db().collection(INBOX_CONNECTIONS).document(user_id).set(payload, merge=True)
    return get_inbox_connection(user_id) or {}


def delete_inbox_connection(user_id: str) -> None:
    _db().collection(INBOX_CONNECTIONS).document(user_id).delete()


def list_connected_inbox_user_ids() -> list[str]:
    """User ids whose connection document says Gmail is connected — i.e. the
    documents that can hold a sealed token (a revoke or a disconnect clears
    the token and ``gmail.connected`` together). One equality filter on a map
    subfield: served by the automatic single-field index, no composite entry
    needed. The cron polls exactly these, and disconnects the ones whose user
    no longer has platform access."""
    query = _db().collection(INBOX_CONNECTIONS).where(
        filter=firestore.FieldFilter("gmail.connected", "==", True)
    )
    return [str((doc.to_dict() or {}).get("user_id") or doc.id) for doc in query.stream()]


def inbox_lease_free(current: Optional[dict[str, Any]], now: datetime) -> bool:
    """The lease rule, pure so the fake store in the a12 tests applies the SAME
    rule the transaction does: free when no document, no ``lease_until``, an
    unparseable one, or one at or before ``now``."""
    raw = (current or {}).get("lease_until")
    if not raw:
        return True
    try:
        until = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until <= now


def take_inbox_lease(
    user_id: str, *, now: datetime, until: datetime
) -> Optional[dict[str, Any]]:
    """Atomically take the fire lease on ``inbox_triage/{user_id}``: read and
    write ``lease_until`` inside ONE transaction, so two overlapping fires
    cannot both read "free" and both proceed (a read-then-``set`` could).

    Returns the document as read inside the transaction, with the new lease,
    when the lease was taken; ``None`` when another fire holds it or there is
    no document. Firestore retries the function on contention, and the loser
    of a race re-reads and sees the winner's lease.

    Semantics against real Firestore were not emulator-tested; the offline
    tests pin the rule (:func:`inbox_lease_free`) through the a12 fake store."""
    ref = _db().collection(INBOX_CONNECTIONS).document(user_id)
    transaction = _db().transaction()

    @firestore.transactional
    def _apply(txn) -> Optional[dict[str, Any]]:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return None
        current = snap.to_dict() or {}
        if not inbox_lease_free(current, now):
            return None
        stamp = {"lease_until": until.isoformat(), "updated_at": _now()}
        txn.update(ref, stamp)
        return {**current, **stamp}

    return _apply(transaction)


def inbox_message_doc_id(user_id: str, message_id: str) -> str:
    return f"{user_id}__{message_id}"


def list_inbox_messages(
    user_id: str, *, retry_due: Optional[bool] = None
) -> list[dict[str, Any]]:
    """The user's per-message tracking rows, optionally only the ones a later
    fire should re-ask. Equality filters only — see the section note."""
    query = _db().collection(INBOX_MESSAGES).where(
        filter=firestore.FieldFilter("user_id", "==", user_id)
    )
    if retry_due is not None:
        query = query.where(filter=firestore.FieldFilter("retry_due", "==", retry_due))
    return [doc.to_dict() for doc in query.stream()]


def save_inbox_messages(user_id: str, docs: dict[str, dict[str, Any]]) -> int:
    """Upsert ``{message_id: fields}`` in batches of 400. Every doc is stamped
    with the user id and the message id so the equality reads above hold."""
    written = 0
    batch = _db().batch()
    batch_size = 0
    collection = _db().collection(INBOX_MESSAGES)
    for message_id, fields in docs.items():
        payload = {**fields, "user_id": user_id, "message_id": message_id, "updated_at": _now()}
        batch.set(collection.document(inbox_message_doc_id(user_id, message_id)), payload, merge=True)
        batch_size += 1
        written += 1
        if batch_size >= 400:
            batch.commit()
            batch = _db().batch()
            batch_size = 0
    if batch_size > 0:
        batch.commit()
    return written


def delete_inbox_messages(user_id: str) -> int:
    """Delete every tracking row of one user (a disconnect). Returns the count."""
    query = _db().collection(INBOX_MESSAGES).where(
        filter=firestore.FieldFilter("user_id", "==", user_id)
    )
    deleted = 0
    batch = _db().batch()
    batch_size = 0
    for doc in query.stream():
        batch.delete(doc.reference)
        batch_size += 1
        deleted += 1
        if batch_size >= 400:
            batch.commit()
            batch = _db().batch()
            batch_size = 0
    if batch_size > 0:
        batch.commit()
    return deleted
