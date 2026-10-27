"""Firestore data access (the metadata "Brain").

Collections:
- brands               brand detail objects
- creatives            files linked to a brand (stores GCS URLs, not bytes)
- reference_creatives  user-uploaded reference material
- brand_references     self-serve GD brand references (one doc each)
- users                application accounts
- brand_cards          PR Writer (a13) brand cards, keyed by the seo_geo brand id
- pr_runs              PR Writer runs (+ versions / events / steps subcollections)

The client is created lazily so the server can boot before GCP is configured.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from google.api_core import exceptions as gexc
from google.cloud import firestore

from app.config import settings

logger = logging.getLogger("agentos.firestore")

_client: Optional[firestore.Client] = None

# Brands change rarely, so a short in-process cache keeps the opening brand
# picker instant and avoids re-hitting Firestore mid-conversation. The cache is
# keyed on ``meta/brands.version`` (re-read at most once per
# ``_BRANDS_VERSION_CHECK_SECONDS``, the GD registry's window), so a brand
# written on ANOTHER Cloud Run instance is listed here within ~10 s instead of
# after the full TTL; the TTL stays as the backstop for writers that do not bump
# the version (the enrichment CLI). (fetched_at, version_at_fetch, brands)
_BRANDS_TTL_SECONDS = 60.0
_BRANDS_VERSION_CHECK_SECONDS = 10.0
_brands_cache: tuple[float, int | None, list[dict[str, Any]]] | None = None
# (checked_at, version | None) — None when the version doc could not be read.
_brands_version_seen: tuple[float, int | None] | None = None


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
    and always count as active. The cache holds the unfiltered list.

    A cached list is served only while it is inside the TTL AND was fetched at
    the brand-set version currently observed; an unreadable version degrades
    to the TTL alone."""
    global _brands_cache
    # The version is read BEFORE the list: a write landing between the two is
    # then newer than the version recorded and triggers the next refetch.
    version = _observed_brands_version()
    cached = _brands_cache
    if (use_cache and cached
            and (time.monotonic() - cached[0]) < _BRANDS_TTL_SECONDS
            and (version is None or cached[1] == version)):
        brands = cached[2]
    else:
        docs = _db().collection("brands").order_by("brand_name").stream()
        brands = [doc.to_dict() | {"id": doc.id} for doc in docs]
        _brands_cache = (time.monotonic(), version, brands)
    if include_archived:
        return brands
    return [b for b in brands if not b.get("archived_at")]


def _observed_brands_version() -> int | None:
    """``meta/brands.version`` as last read, re-read at most once per
    ``_BRANDS_VERSION_CHECK_SECONDS`` — one small doc read per window per
    instance, never one per request. ``None`` when it cannot be read."""
    global _brands_version_seen
    now = time.monotonic()
    seen = _brands_version_seen
    if seen is not None and (now - seen[0]) < _BRANDS_VERSION_CHECK_SECONDS:
        return seen[1]
    try:
        version: int | None = brands_version()
    except Exception as exc:  # noqa: BLE001 - the list still works on its TTL
        logger.warning("brand version unreadable; brand list cache falls back to its "
                       "%.0fs TTL: %s", _BRANDS_TTL_SECONDS, exc)
        version = None
    _brands_version_seen = (now, version)
    return version


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
    """After a write on THIS instance: drop the list and the observed version,
    so the next read refetches and records the version the write produced."""
    global _brands_cache, _brands_version_seen
    _brands_cache = None
    _brands_version_seen = None


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


@dataclass(frozen=True)
class StoredUpload:
    """A file a direct upload's finalize step has ALREADY placed in GCS: the
    working copy GD reads (``working_uri`` — what every existing consumer of
    ``logo_uri`` / ``*_files`` / a reference's ``gs_uri`` resolves to) and the
    untouched original it was derived from. For fonts and PDFs, which are not
    derived, the working copy IS the original.

    ``content_id`` is the GCS MD5 of the original (hex) — measured by GCS, so
    finalizing the same upload twice lands on the same ids."""

    content_id: str
    working_uri: str
    working_object_path: str
    working_content_type: str
    original_uri: str
    original_bytes: int
    working_width: int | None = None
    working_height: int | None = None
    original_width: int | None = None
    original_height: int | None = None
    flags: tuple[str, ...] = ()
    pages_used: str | None = None
    pages: int | None = None

    def original_fields(self) -> dict[str, Any]:
        """The optional fields a stored record carries (read with defaults)."""
        return {
            "working_uri": self.working_uri,
            "original_uri": self.original_uri,
            "original_bytes": self.original_bytes,
            "original_width": self.original_width,
            "original_height": self.original_height,
            "flags": list(self.flags),
            "pages_used": self.pages_used,
            "pages": self.pages,
        }


#: ``brand_metadata.enrichment.originals``: one record per directly-uploaded
#: kit file, keyed by ``working_uri`` — the ``*_files`` lists stay plain URI
#: strings because every existing reader iterates them as such.
_ENRICHMENT_ORIGINALS_KEY = "originals"


def add_brand_asset(brand_id: str, kind: str, data: bytes | None = None, ext: str | None = None,
                    *, stored: StoredUpload | None = None) -> dict[str, Any]:
    """Record a kit file (``logo`` | ``font`` | ``guidelines``) under
    ``brand_metadata.enrichment.{logo,font,guideline}_files``; a logo also
    becomes ``logo_uri``. The same file twice is one entry.

    Two sources, one transaction:
    - ``data`` + ``ext`` (multipart route): uploaded here to
      ``brands/<id>/<kind>s/<sha256[:16]>.<ext>``;
    - ``stored`` (direct upload, already in GCS): its ``working_uri`` is the
      URI recorded, and its original's facts are appended to
      ``enrichment.originals``.

    Font caveat for whoever builds ``gd_spec``: ``gd_brand_source`` matches
    ``font_variants[].file`` to these URIs by BASENAME, i.e. the hash name.
    """
    from app.services import storage

    if kind not in _ASSET_KIND_KEY:
        raise ValueError(f"unknown brand asset kind {kind!r}")
    if (stored is None) == (data is None):
        raise ValueError("pass either the file bytes or a stored upload")
    ref = _db().collection("brands").document(brand_id)
    _require_user_brand(ref.get(), brand_id)  # refuse before any bytes move
    uri = stored.working_uri if stored else storage.put_brand_asset(brand_id, kind, data, ext or "")
    list_key = _ASSET_KIND_KEY[kind]

    def _apply(txn) -> dict[str, Any]:
        doc = _require_user_brand(ref.get(transaction=txn), brand_id)
        meta = dict(doc.get("brand_metadata") or {})
        enrichment = dict(meta.get("enrichment") or {})
        files = list(enrichment.get(list_key) or [])
        already = uri in files
        if not already:
            files.append(uri)
        enrichment[list_key] = files
        if stored is not None:
            originals = [r for r in (enrichment.get(_ENRICHMENT_ORIGINALS_KEY) or [])
                         if isinstance(r, dict) and r.get("working_uri") != uri]
            originals.append({"kind": kind, **stored.original_fields()})
            enrichment[_ENRICHMENT_ORIGINALS_KEY] = originals
        meta["enrichment"] = enrichment
        now = _now()
        fields: dict[str, Any] = {"brand_metadata": meta, "updated_at": now}
        if kind == "logo":
            fields["logo_uri"] = uri
        txn.update(ref, fields)
        _bump_brands_version(txn, now)
        return {"brand_id": brand_id, "kind": kind, "uri": uri, "already_recorded": already}

    result = _transact(_apply)
    _invalidate_brands_cache()
    return result


def brand_asset_originals(brand: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{working_uri: original record}`` for a brand doc (``{}`` for kits
    uploaded before direct uploads existed — read with a default)."""
    enrichment = (brand.get("brand_metadata") or {}).get("enrichment") or {}
    return {r["working_uri"]: r for r in enrichment.get(_ENRICHMENT_ORIGINALS_KEY) or []
            if isinstance(r, dict) and r.get("working_uri")}


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
    data: bytes | None = None,
    ext: str | None = None,
    kind: str,
    uploaded_by: str,
    width: int | None = None,
    height: int | None = None,
    note: str = "",
    creative_type: str | None = None,
    stored: StoredUpload | None = None,
) -> dict[str, Any]:
    """Store one reference file and its doc; returns the doc.

    ``ref_id`` is the content hash, so the same bytes uploaded twice are one
    reference (re-adding a soft-deleted one revives it). Raises
    ``ReferenceCapReached`` at ``BRAND_REFERENCE_CAP`` active references —
    enforced on a counter doc written in the same transaction as the
    reference, so parallel uploads cannot overshoot. Legacy Drive-synced
    references do not count toward the cap.

    ``stored`` (a direct upload already in GCS) replaces ``data``/``ext``:
    nothing is uploaded here, ``ref_id`` is the original's GCS MD5, the doc's
    ``gs_uri``/``object_path`` are the WORKING copy's (what generation reads)
    and the original's facts ride along as optional fields.
    """
    from app.services import storage

    if kind not in ("creative", "reference"):
        raise ValueError("kind must be 'creative' or 'reference'")
    if (stored is None) == (data is None):
        raise ValueError("pass either the file bytes or a stored upload")
    if stored is not None:
        ref_id = stored.content_id
        object_path = stored.working_object_path
        content_type = stored.working_content_type
    else:
        ref_id = storage.content_hash(data)
        object_path = storage.reference_object_path(brand_id, data, ext or "")  # validates ext
        content_type = storage.content_type_for_ext(ext or "")
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
    gs_uri = stored.working_uri if stored else storage.put_reference_file(brand_id, data, ext or "")

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
            "content_type": content_type,
            "width": width,
            "height": height,
            "note": note or "",
            "uploaded_by": (uploaded_by or "").lower(),
            "created_at": now,
            "deleted_at": None,
        }
        if stored is not None:
            doc |= stored.original_fields()
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


def list_users(*, timeout: float | None = None) -> list[dict[str, Any]]:
    """Every user document, newest account first.

    ``timeout`` bounds the read for a caller that can do without the result —
    a list that only labels its rows with author names should give up quickly
    rather than wait out Firestore's default deadline. Unset keeps the default.
    """
    users_ref = _db().collection("users")
    docs = users_ref.stream() if timeout is None else users_ref.stream(timeout=timeout)
    users = [doc.to_dict() | {"id": doc.id} for doc in docs]
    users.sort(key=lambda u: u.get("created_at", ""), reverse=True)
    return users


# --------------------------------------------------------------------------- #
# Asks — what people type into the console's "Submit feedback", "Report a
# problem" and "Request a new agent" forms. One document each; the admins'
# inbox on the console is the delivery (this backend sends no e-mail).
# --------------------------------------------------------------------------- #

ASKS_COLLECTION = "asks"
ASK_KINDS = ("feedback", "issue", "agent")
ASK_STATUSES = ("new", "seen", "done")

#: Ceiling on any single Firestore call made for an ask. A form submit or the
#: admin inbox must fail loudly, not hang a request on a cold channel.
_ASKS_IO_TIMEOUT_S = 20.0

#: How many asks a status-filtered read will pull before sorting. There is no
#: ``(status, created_at)`` composite index, so "the newest N unseen" cannot
#: be asked of Firestore directly: a ``limit`` on the filtered query would cut
#: in document-id order and hand back N arbitrary unseen asks. The filtered
#: read therefore pulls the whole status up to this cap and slices after
#: sorting; at the cap it reports a failed read rather than a wrong page.
_ASKS_SCAN_CAP = 2000


def _ask_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("created_at") or ""), str(row.get("id") or ""))


def create_ask(
    *, kind: str, fields: dict[str, str], sender: dict[str, str], page: str
) -> dict[str, Any]:
    """File one ask. Raises when the write did not land — the caller answers
    honestly rather than telling the person it was filed."""
    now = _now()
    ask_id = uuid.uuid4().hex
    doc: dict[str, Any] = {
        "id": ask_id,
        "kind": kind,
        "fields": dict(fields),
        "from": dict(sender),
        "page": page,
        "status": "new",
        "created_at": now,
        "updated_at": now,
    }
    _db().collection(ASKS_COLLECTION).document(ask_id).set(doc, timeout=_ASKS_IO_TIMEOUT_S)
    return doc


def list_asks(status: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]] | None:
    """Asks newest first, optionally only those in one ``status``.

    ``[]`` means the inbox is genuinely empty; ``None`` means the read failed
    (the :func:`count_collection` contract). No composite index exists for
    ``asks``: the unfiltered read orders on the single ``created_at`` field
    and limits server-side; the filtered one is one equality filter pulled up
    to :data:`_ASKS_SCAN_CAP`, sorted in process and sliced to ``limit`` —
    both served by Firestore's automatic indexes.
    """
    try:
        col = _db().collection(ASKS_COLLECTION)
        if status:
            query = (col.where(filter=firestore.FieldFilter("status", "==", status))
                     .limit(_ASKS_SCAN_CAP))
        else:
            query = (col.order_by("created_at", direction=firestore.Query.DESCENDING)
                     .limit(limit))
        docs = query.stream(timeout=_ASKS_IO_TIMEOUT_S)
        rows = [(doc.to_dict() or {}) | {"id": doc.id} for doc in docs]
    except Exception:
        logger.warning("could not read the asks inbox", exc_info=True)
        return None
    if status and len(rows) >= _ASKS_SCAN_CAP:
        logger.warning("asks: %s+ rows in status %r; the filtered read is capped and "
                       "reports failure rather than a wrong page", _ASKS_SCAN_CAP, status)
        return None
    rows.sort(key=_ask_sort_key, reverse=True)
    return rows[:limit]


def count_asks(status: str) -> int | None:
    """How many asks sit in ``status`` — server-side, so the number is right
    even when the listing is capped. ``None`` when it could not be read."""
    try:
        result = (
            _db()
            .collection(ASKS_COLLECTION)
            .where(filter=firestore.FieldFilter("status", "==", status))
            .count()
            .get(timeout=_ASKS_IO_TIMEOUT_S)
        )
        return int(result[0][0].value) if result and result[0] else 0
    except Exception:
        logger.warning("could not count asks", exc_info=True)
        return None


def set_ask_status(ask_id: str, status: str) -> Optional[dict[str, Any]]:
    """Move one ask to ``status`` and return it as it now stands. ``None`` when
    no such ask exists; a failed read or write raises."""
    if status not in ASK_STATUSES:
        raise ValueError(f"unknown ask status: {status!r}")
    ref = _db().collection(ASKS_COLLECTION).document(ask_id)
    snap = ref.get(timeout=_ASKS_IO_TIMEOUT_S)
    if not snap.exists:
        return None
    patch = {"status": status, "updated_at": _now()}
    ref.update(patch, timeout=_ASKS_IO_TIMEOUT_S)
    return (snap.to_dict() or {}) | {"id": snap.id} | patch


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
        # The same calendar keys ``run_tracking.record_activity`` stamps, so
        # both kinds of row can be bucketed alike (team usage reads either).
        "day": now[:10],
        "year_month": now[:7],
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


#: Ceiling on any single ``runs`` read made for the team-usage panel. Firestore
#: retries transient faults on its own; what it will not do unasked is stop
#: waiting, and the Home panel must not hang on a reportee's row.
_RUNS_READ_TIMEOUT_S = 20.0

#: Fields the team-usage panel needs per row. Projected so a reportee's month
#: of rows (one per unit of work) travels as a few bytes each, not the full
#: task text and summary.
TEAM_RUN_FIELDS: tuple[str, ...] = ("agent_id", "day", "date", "year_month", "created_at")

#: Ceiling on one user's projected rows in a team-usage read. Far above any
#: real account today; hitting it means the figure would be a floor, not a
#: count, so the read reports failure rather than a number that is quietly
#: short.
_TEAM_ROWS_CAP = 10000


def run_row_month(row: dict[str, Any]) -> str:
    """The ``YYYY-MM`` a ``runs`` row belongs to.

    Two writers fill the collection: ``run_tracking.record_activity`` stamps
    ``year_month``/``day``; ``create_run`` (the staged GD / Blog / Creative
    runs — most of what people actually do) historically stamped only ``date``
    and ``created_at``. Reading the month off whichever field a row carries is
    what makes both kinds count; filtering on ``year_month`` alone counted
    nearly nothing (2026-10-08).
    """
    ym = str(row.get("year_month") or "")
    if len(ym) == 7:
        return ym
    return str(row.get("date") or row.get("day") or row.get("created_at") or "")[:7]


def _is_transient_read_failure(exc: BaseException) -> bool:
    """A deadline or availability fault from the Firestore transport — the kind
    that clears on its own — as opposed to a bad query, which does not."""
    try:
        from google.api_core import exceptions as gexc
    except Exception:  # noqa: BLE001
        return False
    return isinstance(exc, (gexc.DeadlineExceeded, gexc.ServiceUnavailable, gexc.Aborted))


def _user_run_rows(user_id: str, fields: tuple[str, ...]) -> list[dict[str, Any]] | None:
    """Every row one user filed, projected to ``fields`` — one equality filter,
    no ``order_by`` (the ``(user_id, created_at DESC)`` index is not built in
    production) and no second filter (rows disagree on which date field they
    carry, see :func:`run_row_month`). ``None`` on a failed read or when the
    cap is hit; ``[]`` when the user filed nothing."""
    if not user_id:
        return []
    want = list(dict.fromkeys([*fields, "date", "year_month", "created_at"]))
    rows: list[dict[str, Any]] | None = None
    # The first streams on a cold Cloud Run instance have timed out at the gRPC
    # layer (DeadlineExceeded, 2026-10-08, eight at once on a fresh revision)
    # and then answered in under a second. One retry covers that; a second
    # failure is reported as a failed read, never as an empty one.
    for attempt in (1, 2):
        try:
            query = (
                _db().collection("runs")
                .where(filter=firestore.FieldFilter("user_id", "==", user_id))
                .select(want)
                .limit(_TEAM_ROWS_CAP)
            )
            rows = [doc.to_dict() or {} for doc in query.stream(timeout=_RUNS_READ_TIMEOUT_S)]
            break
        except Exception as exc:  # noqa: BLE001 — classified below
            if attempt == 1 and _is_transient_read_failure(exc):
                logger.warning("runs: a user's read timed out on a cold channel; retrying once")
                continue
            logger.warning("could not read a user's runs", exc_info=True)
            return None
    if rows is None:
        return None
    if len(rows) >= _TEAM_ROWS_CAP:
        logger.warning("runs: a user has %s+ rows; the team-usage read is capped "
                       "and reports failure rather than a short count", _TEAM_ROWS_CAP)
        return None
    return rows


def list_runs_for_user_months(
    user_id: str, year_months: list[str], fields: tuple[str, ...] = TEAM_RUN_FIELDS
) -> list[dict[str, Any]] | None:
    """One user's rows for the given ``YYYY-MM`` months, projected to ``fields``.

    ``None`` means the read failed; ``[]`` means the user filed nothing in
    those months. Same contract as :func:`count_runs_for_user`.
    """
    rows = _user_run_rows(user_id, fields)
    if rows is None:
        return None
    wanted = set(year_months)
    return [r for r in rows if run_row_month(r) in wanted]


def run_row_day(row: dict[str, Any], tz: Any = None) -> str:
    """The ``YYYY-MM-DD`` a ``runs`` row belongs to, on ``tz``'s calendar.

    ``created_at`` (UTC ISO, both writers stamp it) is converted when it
    parses; otherwise the stamped ``day`` / ``date`` (UTC) is used as is.
    """
    created = str(row.get("created_at") or "")
    if created:
        try:
            when = datetime.fromisoformat(created)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return when.astimezone(tz or timezone.utc).date().isoformat()
        except ValueError:
            pass
    return str(row.get("day") or row.get("date") or "")[:10]


def run_rollup_for_user(user_id: str, tz: Any = None) -> dict[str, Any] | None:
    """One user's rows rolled up two ways from ONE projected read:
    ``by_month`` — ``{"2026-10": {"a1": 12, "a2": 3}}`` (the rows' own UTC
    month, see :func:`run_row_month`) — and ``by_day`` — ``{"2026-10-07": 4}``
    on ``tz``'s calendar.

    ``None`` when the read failed, never an empty rollup that would read as
    "did nothing". A row without an agent counts as "unknown".
    """
    if not user_id:
        return {"by_month": {}, "by_day": {}}
    rows = _user_run_rows(user_id, ("date", "day", "agent_id"))
    if rows is None:
        return None
    by_month: dict[str, dict[str, int]] = {}
    by_day: dict[str, int] = {}
    for r in rows:
        ym = run_row_month(r)
        if ym:
            agent = str(r.get("agent_id") or "unknown")
            month = by_month.setdefault(ym, {})
            month[agent] = month.get(agent, 0) + 1
        day = run_row_day(r, tz)
        if day:
            by_day[day] = by_day.get(day, 0) + 1
    return {"by_month": by_month, "by_day": by_day}


def runs_for_user_by_month_and_agent(user_id: str) -> dict[str, dict[str, int]] | None:
    """One user's rows, per ``YYYY-MM`` and per agent — one read per user.

    ``{"2026-10": {"a1": 12, "a2": 3}}``. ``None`` when the read failed, never
    an empty map that would read as "did nothing". A month absent from the
    map is a month with no rows; a row without an agent counts as "unknown".
    """
    rollup = run_rollup_for_user(user_id)
    return None if rollup is None else rollup["by_month"]


def count_runs_for_user_by_month(user_id: str) -> dict[str, int] | None:
    """How many rows one user filed, per ``YYYY-MM`` — one read per user.

    ``None`` when the read failed, never a zero map that would read as "did
    nothing". A month absent from the map is a month with no rows.
    """
    by_agent = runs_for_user_by_month_and_agent(user_id)
    if by_agent is None:
        return None
    return {ym: sum(agents.values()) for ym, agents in by_agent.items()}


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
# SEO Analyst (a2) — one Search Console grant per brand
# --------------------------------------------------------------------------- #
# ``seo_gsc_grants/{brand_id}`` holds the brand's Search Console connection:
# the property it reads, the refresh token SEALED by ``seo_geo_agent.gsc_oauth``
# (nothing here ever receives plaintext from a new connect), and who connected
# it and when. One document per brand, so the SEO overview reads every brand's
# grant in ONE batched ``get_all`` — no query, no index.
#
# Before 2026-10-14 a grant was written by ``seo_geo_agent.state`` to
# ``seo_geo/gsc-auth-{brand_id}`` with the token in the clear. That flow never
# completed in any deployment, but the read still looks there as a fallback so
# a stray legacy grant is honoured rather than silently ignored, and every save
# or delete removes it — a plaintext copy does not outlive the next connect.

SEO_GSC_GRANTS = "seo_gsc_grants"
_SEO_LEGACY_COLLECTION = "seo_geo"
_SEO_LEGACY_PREFIX = "gsc-auth-"
#: Bounded on purpose: the overview is a page load, and a Firestore stall must
#: not hold a worker thread for the client library's default minute.
_GSC_GRANTS_TIMEOUT_S = 15.0


def _legacy_gsc_ref(brand_id: str):
    return _db().collection(_SEO_LEGACY_COLLECTION).document(f"{_SEO_LEGACY_PREFIX}{brand_id}")


def get_gsc_grants(brand_ids: list[str]) -> dict[str, dict[str, Any]]:
    """``{brand_id: grant}`` for the brands that have one, in one round trip.

    A grant in ``seo_gsc_grants`` wins; a legacy ``seo_geo/gsc-auth-*`` doc is
    returned only when there is no current one, marked ``legacy: True`` so the
    caller knows its token is the plaintext ``refresh_token`` field."""
    ids = [b for b in dict.fromkeys(brand_ids) if b]
    if not ids:
        return {}
    current = _db().collection(SEO_GSC_GRANTS)
    refs = [current.document(b) for b in ids] + [_legacy_gsc_ref(b) for b in ids]
    found: dict[str, dict[str, Any]] = {}
    legacy: dict[str, dict[str, Any]] = {}
    for snap in _db().get_all(refs, timeout=_GSC_GRANTS_TIMEOUT_S):
        if not snap.exists:
            continue
        if snap.reference.parent.id == _SEO_LEGACY_COLLECTION:
            legacy[snap.id.removeprefix(_SEO_LEGACY_PREFIX)] = {
                **(snap.to_dict() or {}), "legacy": True,
            }
        else:
            found[snap.id] = snap.to_dict() or {}
    return {b: found.get(b) or legacy[b] for b in ids if b in found or b in legacy}


def save_gsc_grant(brand_id: str, grant: dict[str, Any]) -> None:
    """Replace the brand's grant with ``grant`` (already sealed by the caller)
    and drop any legacy plaintext copy, in one batch."""
    batch = _db().batch()
    batch.set(_db().collection(SEO_GSC_GRANTS).document(brand_id),
              {**grant, "brand_id": brand_id, "updated_at": _now()})
    batch.delete(_legacy_gsc_ref(brand_id))
    batch.commit(timeout=_GSC_GRANTS_TIMEOUT_S)


def delete_gsc_grant(brand_id: str) -> None:
    """Forget the brand's grant — current and legacy — in one batch."""
    batch = _db().batch()
    batch.delete(_db().collection(SEO_GSC_GRANTS).document(brand_id))
    batch.delete(_legacy_gsc_ref(brand_id))
    batch.commit(timeout=_GSC_GRANTS_TIMEOUT_S)


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


def inbox_lease_held_by(current: Optional[dict[str, Any]], owner: str, held: str) -> bool:
    """Is the lease still this fire's? Pure, like :func:`inbox_lease_free`, so
    the fake store applies the same rule: the fire's token is on it, AND the
    time on it is the one that fire last wrote (``held``, as stored).

    Both, because a lease can change hands without the token changing: a
    fire of a revision from before ``lease_owner`` existed takes the lease by
    writing the time alone. A document with no token is held by no fire that
    asks."""
    doc = current or {}
    return (
        bool(owner) and bool(held)
        and str(doc.get("lease_owner") or "") == owner
        and str(doc.get("lease_until") or "") == held
    )


def take_inbox_lease(
    user_id: str, *, now: datetime, until: datetime, owner: str = ""
) -> Optional[dict[str, Any]]:
    """Atomically take the fire lease on ``inbox_triage/{user_id}``: read and
    write ``lease_until`` inside ONE transaction, so two overlapping fires
    cannot both read "free" and both proceed (a read-then-``set`` could).

    ``owner`` is the taking fire's own token, stored beside the time as
    ``lease_owner``: a lease is extended or cleared only by the fire whose
    token is on it (:func:`renew_inbox_lease`, :func:`release_inbox_lease`).

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
        stamp = {"lease_until": until.isoformat(), "lease_owner": owner, "updated_at": _now()}
        txn.update(ref, stamp)
        return {**current, **stamp}

    return _apply(transaction)


def renew_inbox_lease(user_id: str, *, owner: str, held: str, until: datetime) -> bool:
    """Extend the fire lease to ``until`` — only if it is still ``owner``'s
    and still reads ``held``, read and written in ONE transaction. ``False``
    when the lease is another fire's, was cleared (a disconnect), or the
    document is gone: the caller no longer holds it and must stop writing.

    A lease that ran out but that nobody took is still its owner's to extend.
    The same transaction shape as :func:`take_inbox_lease`, with the same
    caveat about emulator testing."""
    ref = _db().collection(INBOX_CONNECTIONS).document(user_id)
    transaction = _db().transaction()

    @firestore.transactional
    def _apply(txn) -> bool:
        snap = ref.get(transaction=txn)
        if not snap.exists or not inbox_lease_held_by(snap.to_dict() or {}, owner, held):
            return False
        txn.update(ref, {"lease_until": until.isoformat(), "updated_at": _now()})
        return True

    return _apply(transaction)


def release_inbox_lease(user_id: str, *, owner: str, held: str) -> bool:
    """Clear the fire lease — only if it is still ``owner``'s and still reads
    ``held``, in ONE transaction. ``False``, and nothing written, when it is
    not: a fire that overran must not free the inbox under the fire that
    replaced it."""
    ref = _db().collection(INBOX_CONNECTIONS).document(user_id)
    transaction = _db().transaction()

    @firestore.transactional
    def _apply(txn) -> bool:
        snap = ref.get(transaction=txn)
        if not snap.exists or not inbox_lease_held_by(snap.to_dict() or {}, owner, held):
            return False
        txn.update(ref, {"lease_until": None, "lease_owner": None, "updated_at": _now()})
        return True

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


# --------------------------------------------------------------------------- #
# PR Writer (a13): brand cards and PR runs
# --------------------------------------------------------------------------- #
# Access patterns — the only reads this section issues:
#
#   brand_cards/{brand_id}                get by id (each run start, card editor)
#   brand_cards/{brand_id}/history/{rev}  newest first on ``revision``
#   pr_runs/{run_id}                      get by id, then the participant check
#   pr_runs  where participants array-contains <uid>
#            order by updated_at DESC, __name__ DESC, limit, start_after
#                                         THE one composite (firestore.indexes.json)
#   pr_runs/{id}/versions/{0001..}        get by id; list ordered on ``version``
#   pr_runs/{id}/events/{000001..}        list ordered on ``seq``
#   pr_runs/{id}/steps/{v001_<step>}      get by id
#
# Subcollection orderings are single-field and served by automatic indexes.
#
# Tenancy: ``brand_cards`` is shared workspace config (one card per seo_geo
# brand id; who may edit is the router's role check). A run's tenant key is
# ``participants`` — owner + the card's reviewers at creation + people added
# explicitly. Every read and write here takes the caller's user id and a
# non-participant is answered exactly as if the run did not exist (``None``
# from reads, :class:`PrNotFound` from writes; the router says 404). Admins are
# NOT participants by default (owner decision 2026-10-27).
#
# Human-only: a run reaches ``approved`` in exactly one place,
# :func:`record_pr_signoff`, which takes a human participant's id. The writes
# the pipeline makes (:func:`create_pr_run`, :func:`complete_pr_step`,
# :func:`create_pr_version`) cannot set that status, sign-offs, quote
# approvals, escalation clearance or participants.
#
# Denormalised on the run doc, each with ONE writer:
#   open_verify_count, quotes, latest_version  <- create_pr_version
#   quotes[].status / attested_by              <- attest_pr_quote
#   event_count                                <- _append_pr_event (same txn)

BRAND_CARDS = "brand_cards"
PR_RUNS = "pr_runs"
_BRAND_CARD_HISTORY = "history"
_PR_VERSIONS = "versions"
_PR_EVENTS = "events"
_PR_STEP_DOCS = "steps"

#: Exactly the AI core's ids (``pr_writer_agent.content_types.CONTENT_TYPES``),
#: in its order. A literal, not an import: the shared store must not load an
#: agent package. ``tests/test_pr_writer_store.py`` pins the two equal.
PR_CONTENT_TYPES = (
    "press_release", "media_advisory", "product_launch", "partnership",
    "milestone", "award", "event", "thought_leadership",
)
PR_STEPS = ("intake", "research", "angle", "draft", "fact_check", "seo", "distribution", "qa")
PR_BRIEF_SOURCES = ("stated", "extracted", "missing")
PR_FACT_STATUSES = ("verified", "needs_confirmation", "removed")
PR_QUOTE_STATUSES = ("pending", "approved", "rejected")
PR_REVIEW_ROLES = ("editorial", "brand", "compliance")
PR_SIGNOFF_ROLES = PR_REVIEW_ROLES + ("final",)
#: Statuses the pipeline may set. ``approved`` and ``changes_requested`` are
#: NOT here — only a human sign-off reaches them.
PR_PIPELINE_STATUSES = ("intake", "in_progress", "needs_input", "escalated", "in_review", "failed")
_RUN_APPROVED = "approved"
_RUN_CHANGES_REQUESTED = "changes_requested"
#: The identity the pipeline writes under. Never accepted as a human actor.
PR_AGENT_ACTOR = "agent:a13"
_NON_HUMAN_ACTORS = frozenset({"a13", "agent", "system", "cron", "pr_writer", "pr-writer"})
_NON_HUMAN_PREFIXES = ("agent:", "system:", "cron:", "service:")

#: Evidence rows kept on the run doc. One research round reads 8 pages
#: (Blog Writer's READS_PER_ROUND) at ~3-5 claims each; two rounds is ~60,
#: about twice what a 400-700-word release needs for its fact-check table.
#: Each row's text fields are clipped (below), so 60 rows stay under ~250 KB
#: and the run doc keeps headroom under Firestore's 1 MiB with brief, quotes
#: and sign-offs beside it. Rows past the cap are dropped deterministically
#: (first 60 kept) and counted in ``evidence_dropped`` — never silently.
PR_EVIDENCE_CAP = 60
_PR_EVIDENCE_TEXT_CHARS = 1000
_PR_EVIDENCE_URL_CHARS = 2048
_PR_EVIDENCE_KEYS_CAP = 12
PR_PARTICIPANTS_CAP = 25
PR_QUOTES_CAP = 20
PR_FACT_ROWS_CAP = 200
PR_VERSIONS_CAP = 50
PR_EVENTS_CAP = 2000
PR_LIST_PAGE_MAX = 50
_PR_BRIEF_FIELDS_CAP = 40
_PR_TEXT_CHARS = 4000
_PR_TITLE_CHARS = 300
#: Firestore refuses documents over 1 MiB; refuse ours a little earlier with
#: an error that says which document, instead of an opaque INVALID_ARGUMENT.
_PR_DOC_BYTES_CAP = 900_000
_BRAND_CARD_BYTES_CAP = 200_000
#: The unordered fallback's ceiling (index not built yet). At the cap the read
#: reports failure rather than a wrong page — the ``asks`` rule.
_PR_LIST_SCAN_CAP = 1000
_PR_IO_TIMEOUT_S = 20.0
_PR_LIST_FIELDS = (
    "user_id", "brand_id", "content_type", "title", "status",
    "latest_version", "open_verify_count", "created_at", "updated_at",
)
_PR_BRAND_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_PR_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PR_QUOTE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: Same shape as the AI core's qa.VERIFY: ``[VERIFY]`` or ``[VERIFY: what]``.
_PR_VERIFY_RE = re.compile(r"\[VERIFY(?::[^\]]*)?\]")
_PR_STEP_PATCH_KEYS = frozenset({"title", "content_type", "brief", "evidence", "angle", "status",
                                 "escalation_reason"})

_pr_runs_index_missing_at: float | None = None


class PrStoreError(Exception):
    """Base for the PR Writer store. Router mapping: :class:`PrNotFound` 404,
    :class:`PrConflict` (and subclasses) 409, :class:`PrActorRefused` 403,
    :class:`PrCapReached` 413, ``ValueError`` 422."""


class PrNotFound(PrStoreError):
    """No such run — or the caller is not one of its participants."""


class PrConflict(PrStoreError):
    """The write was refused because the stored state moved or forbids it."""


class BrandCardConflict(PrConflict):
    def __init__(self, brand_id: str, current_revision: int, expected_revision: int):
        self.brand_id = brand_id
        self.current_revision = current_revision
        self.expected_revision = expected_revision
        super().__init__(
            f"brand card {brand_id!r} is at revision {current_revision}, "
            f"not {expected_revision} — reload and re-apply the edit")


class PrAlreadyExists(PrConflict):
    """A create-only document (version, event, step output, card history) is
    already written; it is never overwritten."""


class PrStaleVersion(PrConflict):
    """The write named a version (or quote text) that is no longer current."""


class PrRunLocked(PrConflict):
    """The run is approved; its locked version is never edited."""


class PrStepAlreadyDone(PrConflict):
    """The step is already checkpointed for this round."""


class PrSignoffBlocked(PrConflict):
    def __init__(self, blockers: list[str]):
        self.blockers = list(blockers)
        super().__init__("final sign-off blocked: " + "; ".join(self.blockers))


class PrActorRefused(PrStoreError):
    """The actor is missing or is not a person."""


class PrCapReached(PrStoreError):
    """A size or count cap on the run was reached."""


# --- small pure helpers ------------------------------------------------------

def _pr_json_bytes(value: Any) -> int:
    return len(json.dumps(value, default=str, separators=(",", ":")).encode("utf-8"))


def _pr_check_size(what: str, value: Any, cap: int) -> None:
    size = _pr_json_bytes(value)
    if size > cap:
        raise PrCapReached(f"{what} is {size} bytes; the cap is {cap}")


def _pr_clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _pr_require_human(actor_id: Any) -> str:
    """The id of a signed-in person, or :class:`PrActorRefused`. The pipeline's
    own identity and any service-ish sentinel are refused by name."""
    actor = str(actor_id or "").strip()
    lowered = actor.lower()
    if not actor or lowered in _NON_HUMAN_ACTORS or lowered.startswith(_NON_HUMAN_PREFIXES):
        raise PrActorRefused("this action needs a signed-in person's id")
    return actor


def _pr_clean_user_ids(values: Any, what: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{what} must be a list of user ids")
    out: list[str] = []
    for value in values:
        uid = str(value or "").strip()
        if not uid or "/" in uid or len(uid) > 128:
            raise ValueError(f"{what}: not a user id: {value!r}")
        if uid not in out:
            out.append(uid)
    return out


def _pr_content_type(value: Any) -> str:
    if value not in PR_CONTENT_TYPES:
        raise ValueError(f"content_type must be one of {PR_CONTENT_TYPES}")
    return value


def _pr_printed(value: Any) -> Any:
    """``value`` without what is never printed: ``blocks[].last_comment`` (a
    reviewer's comment on a block). Headline/subhead alternates stay — they
    are handed to reviewers as part of the package."""
    if isinstance(value, dict):
        return {k: ([{bk: bv for bk, bv in b.items() if bk != "last_comment"} if isinstance(b, dict) else b
                     for b in v] if k == "blocks" and isinstance(v, list) else _pr_printed(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_pr_printed(v) for v in value]
    return value


def _pr_brand_id(brand_id: Any) -> str:
    if not isinstance(brand_id, str) or not _PR_BRAND_ID_RE.match(brand_id):
        raise ValueError(f"not a brand id: {brand_id!r}")
    return brand_id


def quote_text_hash(text: str) -> str:
    """sha256 of the quote with whitespace collapsed. Whitespace is the only
    thing normalised: a changed word, letter case or comma is an edit, and an
    edited quote goes back to pending (blueprint §7, §11)."""
    return hashlib.sha256(" ".join(str(text or "").split()).encode("utf-8")).hexdigest()


def normalize_pr_brief(brief: Any) -> dict[str, dict[str, Any]]:
    """``{field: {"value", "source"}}`` with ``source`` one of
    :data:`PR_BRIEF_SOURCES`. A stated/extracted field with an empty value is
    stored as ``missing`` — a blank is not an answer."""
    if brief is None:
        return {}
    if not isinstance(brief, dict):
        raise ValueError("brief must be a mapping of field -> {value, source}")
    if len(brief) > _PR_BRIEF_FIELDS_CAP:
        raise ValueError(f"brief has more than {_PR_BRIEF_FIELDS_CAP} fields")
    out: dict[str, dict[str, Any]] = {}
    for key, entry in brief.items():
        if not isinstance(key, str) or not re.match(r"^[a-z][a-z0-9_]{0,63}$", key):
            raise ValueError(f"brief field name not allowed: {key!r}")
        if not isinstance(entry, dict) or entry.get("source") not in PR_BRIEF_SOURCES:
            raise ValueError(f"brief.{key} needs a source in {PR_BRIEF_SOURCES}")
        source = entry["source"]
        value = None if source == "missing" else entry.get("value")
        if value is None or value == "" or value == [] or value == {}:
            source, value = "missing", None
        out[key] = {"value": value, "source": source}
    return out


def merge_pr_brief(current: Any, incoming: Any) -> dict[str, dict[str, Any]]:
    """Fold ``incoming`` into ``current``. A field the user STATED is a hard
    requirement: only another stated value replaces it, never an extracted
    guess or a "missing"."""
    merged = {k: dict(v) for k, v in (current or {}).items()}
    for key, entry in normalize_pr_brief(incoming).items():
        held = merged.get(key) or {}
        if held.get("source") == "stated" and entry["source"] != "stated":
            continue
        merged[key] = entry
    return merged


def normalize_pr_evidence(items: Any) -> tuple[list[dict[str, Any]], int]:
    """Clip each row and keep the first :data:`PR_EVIDENCE_CAP`. Returns
    ``(rows, dropped)``; the caller stores ``dropped`` so the cap is visible."""
    if items is None:
        return [], 0
    if not isinstance(items, list):
        raise ValueError("evidence must be a list")
    rows: list[dict[str, Any]] = []
    for item in items[:PR_EVIDENCE_CAP]:
        if not isinstance(item, dict):
            raise ValueError("each evidence row must be a mapping")
        row: dict[str, Any] = {}
        for key in list(item)[:_PR_EVIDENCE_KEYS_CAP]:
            value = item[key]
            limit = _PR_EVIDENCE_URL_CHARS if key == "url" else _PR_EVIDENCE_TEXT_CHARS
            row[str(key)[:64]] = value[:limit] if isinstance(value, str) else (
                value if isinstance(value, (int, float, bool)) or value is None else str(value)[:limit])
        rows.append(row)
    return rows, max(0, len(items) - PR_EVIDENCE_CAP)


def normalize_pr_fact_checks(rows: Any) -> list[dict[str, Any]]:
    """Fact-check rows with ``status`` folded onto :data:`PR_FACT_STATUSES`.
    Anything unrecognised becomes ``needs_confirmation`` — fail closed, so an
    odd label can never clear a flag."""
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("fact_checks must be a list")
    if len(rows) > PR_FACT_ROWS_CAP:
        raise PrCapReached(f"more than {PR_FACT_ROWS_CAP} fact-check rows")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each fact-check row must be a mapping")
        status = re.sub(r"[\s-]+", "_", str(row.get("status") or "").strip().lower())
        out.append(dict(row) | {"status": status if status in PR_FACT_STATUSES else "needs_confirmation"})
    return out


def count_open_verify(package: Any, fact_checks: list[dict[str, Any]]) -> int:
    """Open ``[VERIFY]`` flags: the larger of the rows still needing
    confirmation and the DISTINCT ``[VERIFY…]`` markers left anywhere in the
    package. Distinct, because a package carries the same draft more than
    once (markdown and blocks); a verified row never clears a marker still
    printed in the draft, and any marker at all keeps the count above 0.
    A reviewer's ``blocks[].last_comment`` is not printed and is not scanned."""
    open_rows = sum(1 for r in fact_checks if r.get("status") == "needs_confirmation")
    text = json.dumps(_pr_printed(package), default=str, ensure_ascii=False)
    markers = set(_PR_VERIFY_RE.findall(text))
    if "[VERIFY" in text and not markers:
        markers = {"[VERIFY"}  # malformed marker: still open
    return max(open_rows, len(markers))


def _pr_normalize_incoming_quotes(quotes: Any) -> list[dict[str, Any]]:
    if quotes is None:
        return []
    if not isinstance(quotes, list):
        raise ValueError("quotes must be a list")
    if len(quotes) > PR_QUOTES_CAP:
        raise PrCapReached(f"more than {PR_QUOTES_CAP} quotes")
    out, seen = [], set()
    for q in quotes:
        if not isinstance(q, dict):
            raise ValueError("each quote must be a mapping")
        qid = str(q.get("quote_id") or "")
        if not _PR_QUOTE_ID_RE.match(qid):
            raise ValueError(f"quote_id not allowed: {qid!r}")
        if qid in seen:
            raise ValueError(f"duplicate quote_id {qid!r}")
        seen.add(qid)
        text = str(q.get("text") or "").strip()
        if not text:
            raise ValueError(f"quote {qid!r} has no text")
        out.append({
            "quote_id": qid,
            "speaker": _pr_clip(q.get("speaker"), 200),
            "title": _pr_clip(q.get("title"), 200),
            "organization": _pr_clip(q.get("organization"), 200),
            "text": text[:_PR_TEXT_CHARS],
            "text_hash": quote_text_hash(text[:_PR_TEXT_CHARS]),
        })
    return out


def reset_changed_quotes(previous: Any, incoming: Any) -> list[dict[str, Any]]:
    """The quote states for a new version. Deterministic and hash-based: a
    quote keeps its status/attestation only when the previous state has the
    same ``quote_id`` AND the same ``text_hash``, speaker, title and
    organization. Anything else — new quote, edited text, re-attributed — is
    ``pending`` with no attestation. Quotes absent from ``incoming`` drop."""
    prev = {str(q.get("quote_id")): q for q in (previous or []) if isinstance(q, dict)}
    out = []
    for q in _pr_normalize_incoming_quotes(incoming):
        old = prev.get(q["quote_id"]) or {}
        same = old and all(old.get(k) == q[k] for k in ("text_hash", "speaker", "title", "organization"))
        if same and old.get("status") in PR_QUOTE_STATUSES:
            carried = {k: old.get(k) for k in ("status", "attested_by", "attested_at", "note")}
        else:
            carried = {"status": "pending", "attested_by": None, "attested_at": None, "note": ""}
        out.append(q | carried)
    return out


def pr_build_round(run: dict[str, Any]) -> int:
    """The version the pipeline is currently building (latest + 1)."""
    return int(run.get("latest_version") or 0) + 1


def pr_step_done(run: dict[str, Any], step: str) -> bool:
    """Is ``step`` already checkpointed for the round being built? The pipeline
    asks this before spending a model call."""
    return ((run.get("steps") or {}).get(step) or {}).get("round") == pr_build_round(run)


def pr_final_blockers(run: dict[str, Any]) -> list[str]:
    """Why the final sign-off would be refused right now (empty = allowed).
    Pure, so the UI can show the same list the transaction enforces."""
    latest = int(run.get("latest_version") or 0)
    if latest == 0:
        return ["there is no draft version yet"]
    blockers = []
    open_verify = int(run.get("open_verify_count") or 0)
    if open_verify:
        blockers.append(f"{open_verify} [VERIFY] flag(s) still open on v{latest}")
    unapproved = [q.get("quote_id") for q in run.get("quotes") or [] if q.get("status") != "approved"]
    if unapproved:
        blockers.append("quote(s) not approved: " + ", ".join(map(str, unapproved)))
    esc = run.get("escalation") or {}
    if esc.get("flagged") and not esc.get("cleared_by"):
        blockers.append("escalation not cleared")
    signoffs = run.get("signoffs") or {}
    for role in PR_REVIEW_ROLES:
        s = signoffs.get(role) or {}
        if s.get("decision") != "approve" or s.get("version") != latest:
            blockers.append(f"{role} sign-off missing on v{latest}")
    return blockers


def _pr_encode_cursor(row: dict[str, Any]) -> str:
    raw = json.dumps([str(row.get("updated_at") or ""), str(row.get("id") or "")])
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _pr_decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8")
        updated_at, run_id = json.loads(raw)
    except Exception as exc:
        raise ValueError("bad cursor") from exc
    if not isinstance(updated_at, str) or not isinstance(run_id, str) or not _PR_RUN_ID_RE.match(run_id):
        raise ValueError("bad cursor")
    return updated_at, run_id


# --- brand cards ---------------------------------------------------------------

def _brand_card_ref(brand_id: str):
    return _db().collection(BRAND_CARDS).document(_pr_brand_id(brand_id))


def get_brand_card(brand_id: str) -> Optional[dict[str, Any]]:
    """``{brand_id, revision, card, created_at, updated_at, updated_by}`` or
    ``None`` when the brand has no card yet (revision 0). One read."""
    snap = _brand_card_ref(brand_id).get()
    return ((snap.to_dict() or {}) | {"brand_id": brand_id}) if snap.exists else None


def save_brand_card(
    brand_id: str, card: dict[str, Any], *, expected_revision: int, actor_id: str
) -> dict[str, Any]:
    """Replace the card IF it is still at ``expected_revision`` (0 = create).

    Optimistic concurrency in one transaction: a stale revision raises
    :class:`BrandCardConflict` (router: 409) and nothing is written or merged.
    On success the prior revision is copied, create-only, to
    ``brand_cards/{id}/history/{rev:06d}`` and the card becomes
    ``revision + 1``. ``card["reviewers"]`` (user ids) is who a new run adds
    as participants. Validating ``brand_id`` against the seo_geo registry is
    the caller's job. 1 read + 2 writes (1 on create)."""
    actor = _pr_require_human(actor_id)
    if not isinstance(card, dict):
        raise ValueError("card must be a mapping")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise ValueError("expected_revision must be a non-negative integer")
    card = dict(card) | {"reviewers": _pr_clean_user_ids(card.get("reviewers"), "reviewers")}
    _pr_check_size("brand card", card, _BRAND_CARD_BYTES_CAP)
    ref = _brand_card_ref(brand_id)

    def _apply(txn) -> dict[str, Any]:
        snap = ref.get(transaction=txn)
        current = (snap.to_dict() or {}) if snap.exists else None
        current_rev = int((current or {}).get("revision") or 0)
        if current_rev != expected_revision:
            raise BrandCardConflict(brand_id, current_rev, expected_revision)
        now = _now()
        if current is not None:
            txn.create(ref.collection(_BRAND_CARD_HISTORY).document(f"{current_rev:06d}"),
                       current | {"superseded_at": now, "superseded_by": actor})
        doc = {
            "brand_id": brand_id,
            "revision": current_rev + 1,
            "card": card,
            "created_at": (current or {}).get("created_at") or now,
            "updated_at": now,
            "updated_by": actor,
        }
        txn.set(ref, doc)
        return doc

    return _transact(_apply)


def list_brand_card_history(brand_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Prior revisions, newest first (single-field order on ``revision``)."""
    docs = (_brand_card_ref(brand_id).collection(_BRAND_CARD_HISTORY)
            .order_by("revision", direction=firestore.Query.DESCENDING)
            .limit(max(1, min(int(limit), 100))).stream())
    return [doc.to_dict() or {} for doc in docs]


# --- runs ------------------------------------------------------------------------

def _pr_run_ref(run_id: str):
    if not isinstance(run_id, str) or not _PR_RUN_ID_RE.match(run_id):
        raise PrNotFound("no such run")
    return _db().collection(PR_RUNS).document(run_id)


def _pr_participant_run(txn, ref, user_id: str) -> dict[str, Any]:
    """The run as read inside ``txn`` — :class:`PrNotFound` when it does not
    exist OR ``user_id`` is not a participant (indistinguishable on purpose)."""
    snap = ref.get(transaction=txn)
    run = (snap.to_dict() or {}) if snap.exists else None
    if run is None or not user_id or user_id not in (run.get("participants") or []):
        raise PrNotFound("no such run")
    return run | {"id": ref.id}


def _pr_refuse_locked(run: dict[str, Any]) -> None:
    if run.get("status") == _RUN_APPROVED:
        raise PrRunLocked(f"run is approved (v{run.get('approved_version')}); it is locked")


def _pr_refuse_escalated(run: dict[str, Any]) -> None:
    """Blueprint §8: an escalated topic pauses drafting until a person clears it."""
    esc = run.get("escalation") or {}
    if esc.get("flagged") and not esc.get("cleared_by"):
        raise PrConflict("run is escalated; a person must clear it before drafting continues")


def _append_pr_event(txn, run_ref, run: dict[str, Any], kind: str, actor: str, now: str,
                     **data: Any) -> dict[str, Any]:
    """Create-only event ``pr_runs/{id}/events/{seq:06d}`` in ``txn``. ``seq``
    comes from the run's ``event_count`` read in the same transaction, so two
    racing writers cannot share one. Returns the run patch to merge."""
    seq = int(run.get("event_count") or 0) + 1
    if seq > PR_EVENTS_CAP:
        raise PrCapReached(f"run has {PR_EVENTS_CAP} events")
    txn.create(run_ref.collection(_PR_EVENTS).document(f"{seq:06d}"),
               {"seq": seq, "kind": kind, "actor_id": actor, "at": now, **data})
    return {"event_count": seq}


def _pr_commit(fn: Callable[[Any], Any]) -> Any:
    """``_transact`` with a create that lost a race reported as the store's own
    conflict rather than a raw API error."""
    try:
        return _transact(fn)
    except gexc.AlreadyExists as exc:
        raise PrAlreadyExists(str(exc)) from exc


def create_pr_run(
    *, owner_id: str, brand_id: str, content_type: str, title: str,
    brief: dict[str, Any] | None = None, participants: list[str] | tuple = (),
) -> dict[str, Any]:
    """Start a run. ``participants`` = owner + the brand card's ``reviewers``
    as they stand now + ``participants`` given here. Records which card
    revision the run started from (0 = no card yet). 1 read + 2 writes."""
    owner = _pr_require_human(owner_id)
    _pr_content_type(content_type)
    title = _pr_clip(title, _PR_TITLE_CHARS)
    if not title:
        raise ValueError("a run needs a title (the news in one sentence)")
    card = get_brand_card(brand_id)
    reviewers = _pr_clean_user_ids(((card or {}).get("card") or {}).get("reviewers"), "reviewers")
    members = _pr_clean_user_ids([owner, *reviewers, *list(participants or ())], "participants")
    if len(members) > PR_PARTICIPANTS_CAP:
        raise PrCapReached(f"more than {PR_PARTICIPANTS_CAP} participants")
    run_id = uuid.uuid4().hex
    ref = _db().collection(PR_RUNS).document(run_id)
    now = _now()
    doc: dict[str, Any] = {
        "user_id": owner,
        "participants": members,
        "brand_id": brand_id,
        "brand_card_revision": int((card or {}).get("revision") or 0),
        "content_type": content_type,
        "title": title,
        "status": "intake",
        "brief": normalize_pr_brief(brief),
        "evidence": [],
        "evidence_dropped": 0,
        "angle": "",
        "quotes": [],
        "signoffs": {},
        "latest_version": 0,
        "open_verify_count": 0,
        "escalation": {"flagged": False, "reason": "", "flagged_at": None,
                       "cleared_by": None, "cleared_at": None, "note": ""},
        "steps": {},
        "event_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    _pr_check_size("run", doc, _PR_DOC_BYTES_CAP)

    def _apply(txn) -> dict[str, Any]:
        created = doc | _append_pr_event(txn, ref, doc, "run_created", owner, now)
        txn.create(ref, created)
        return created | {"id": run_id}

    return _pr_commit(_apply)


def get_pr_run(run_id: str, user_id: str) -> Optional[dict[str, Any]]:
    """The run, or ``None`` when it does not exist or ``user_id`` is not a
    participant (router: 404 either way). One read."""
    if not user_id or not isinstance(run_id, str) or not _PR_RUN_ID_RE.match(run_id):
        return None
    snap = _db().collection(PR_RUNS).document(run_id).get()
    run = (snap.to_dict() or {}) if snap.exists else None
    if run is None or user_id not in (run.get("participants") or []):
        return None
    return run | {"id": run_id}


def list_pr_runs(
    user_id: str, *, limit: int = 20, cursor: str | None = None
) -> Optional[dict[str, Any]]:
    """The caller's runs (any they participate in), most recently updated
    first: ``{"runs": [...], "next_cursor": str | None}``. Rows carry only
    :data:`_PR_LIST_FIELDS` + ``id``. ``None`` means the read failed.

    One query, ``participants array-contains`` + ``updated_at DESC`` (+ the
    doc id as tie-break) — needs the ``pr_runs`` composite in
    ``firestore.indexes.json``. Until it is built the refused query falls back
    to the array-contains filter alone (automatic index), sorted here and
    capped at :data:`_PR_LIST_SCAN_CAP`. Cost: ``limit + 1`` reads per page
    (fallback: every one of the caller's runs)."""
    global _pr_runs_index_missing_at
    if not user_id:
        return {"runs": [], "next_cursor": None}
    limit = max(1, min(int(limit), PR_LIST_PAGE_MAX))
    after = _pr_decode_cursor(cursor) if cursor else None
    doc_id = "__name__"  # FieldPath.document_id(); a str cursor value becomes a doc reference
    scoped = _db().collection(PR_RUNS).where(
        filter=firestore.FieldFilter("participants", "array_contains", user_id))

    def _page(rows: list[dict[str, Any]]) -> dict[str, Any]:
        more = len(rows) > limit
        rows = rows[:limit]
        return {"runs": rows, "next_cursor": _pr_encode_cursor(rows[-1]) if more and rows else None}

    now = time.monotonic()
    known_missing = (_pr_runs_index_missing_at is not None
                     and (now - _pr_runs_index_missing_at) < _RUNS_INDEX_RETRY_SECONDS)
    if not known_missing:
        try:
            query = (scoped.order_by("updated_at", direction=firestore.Query.DESCENDING)
                     .order_by(doc_id, direction=firestore.Query.DESCENDING))
            if after:
                query = query.start_after({"updated_at": after[0], doc_id: after[1]})
            docs = query.select(list(_PR_LIST_FIELDS)).limit(limit + 1).stream(timeout=_PR_IO_TIMEOUT_S)
            rows = [(doc.to_dict() or {}) | {"id": doc.id} for doc in docs]
            _pr_runs_index_missing_at = None
            return _page(rows)
        except Exception:
            _pr_runs_index_missing_at = now
            logger.warning(
                "pr_runs: the ordered read was refused — build the composite "
                "(participants CONTAINS, updated_at DESC). Sorting in process.",
                exc_info=True)
    try:
        docs = scoped.select(list(_PR_LIST_FIELDS)).limit(_PR_LIST_SCAN_CAP).stream(timeout=_PR_IO_TIMEOUT_S)
        rows = [(doc.to_dict() or {}) | {"id": doc.id} for doc in docs]
    except Exception:
        logger.warning("could not read pr_runs", exc_info=True)
        return None
    if len(rows) >= _PR_LIST_SCAN_CAP:
        logger.warning("pr_runs: %s+ runs for one caller; the unordered read reports "
                       "failure rather than a wrong page — build the index", _PR_LIST_SCAN_CAP)
        return None
    key = lambda r: (str(r.get("updated_at") or ""), str(r.get("id") or ""))  # noqa: E731
    rows.sort(key=key, reverse=True)
    if after:
        rows = [r for r in rows if key(r) < after]
    return _page(rows)


def add_pr_participant(run_id: str, *, actor_id: str, user_id: str) -> dict[str, Any]:
    """A participant adds someone (a quoted executive, a second reviewer).
    1 read + 2 writes."""
    actor = _pr_require_human(actor_id)
    new_uid = _pr_clean_user_ids([user_id], "user_id")[0]
    ref = _pr_run_ref(run_id)

    def _apply(txn) -> dict[str, Any]:
        run = _pr_participant_run(txn, ref, actor)
        members = list(run.get("participants") or [])
        if new_uid in members:
            return run
        if len(members) >= PR_PARTICIPANTS_CAP:
            raise PrCapReached(f"run already has {PR_PARTICIPANTS_CAP} participants")
        now = _now()
        patch = {"participants": members + [new_uid], "updated_at": now}
        patch |= _append_pr_event(txn, ref, run, "participant_added", actor, now, user_id=new_uid)
        txn.update(ref, patch)
        return run | patch

    return _pr_commit(_apply)


# --- pipeline writes (the agent's ONLY write paths) ------------------------------

def _pr_apply_step_patch(run: dict[str, Any], patch: dict[str, Any], now: str) -> dict[str, Any]:
    unknown = set(patch) - _PR_STEP_PATCH_KEYS
    if unknown:
        raise ValueError(f"the pipeline cannot write {sorted(unknown)}")
    out: dict[str, Any] = {}
    if "title" in patch:
        title = _pr_clip(patch["title"], _PR_TITLE_CHARS)
        if not title:
            raise ValueError("title cannot be blank")
        out["title"] = title
    if "brief" in patch:
        out["brief"] = merge_pr_brief(run.get("brief"), patch["brief"])
    if "evidence" in patch:
        out["evidence"], out["evidence_dropped"] = normalize_pr_evidence(patch["evidence"])
    if "content_type" in patch:
        out["content_type"] = _pr_content_type(patch["content_type"])
    if "angle" in patch:
        out["angle"] = _pr_clip(patch["angle"], _PR_TEXT_CHARS)
    if "status" in patch:
        if patch["status"] not in PR_PIPELINE_STATUSES:
            raise ValueError(f"the pipeline may only set status to one of {PR_PIPELINE_STATUSES}")
        out["status"] = patch["status"]
    if patch.get("escalation_reason"):
        out["escalation"] = {"flagged": True, "reason": _pr_clip(patch["escalation_reason"], _PR_TEXT_CHARS),
                             "flagged_at": now, "cleared_by": None, "cleared_at": None, "note": ""}
        out["status"] = "escalated"
    return out


def complete_pr_step(
    run_id: str, step: str, *, user_id: str, output: Any = None,
    patch: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Checkpoint ``step`` for the round being built, once.

    In one transaction: if the step is already done this round, nothing is
    written and ``(run, False)`` comes back — a retried request never re-runs
    or re-writes it. Otherwise ``output`` (if any) is created at
    ``steps/v{round:03d}_{step}``, the allow-listed ``patch`` fields
    (title, brief, evidence, angle, pipeline status, escalation_reason) land on
    the run, and ``(run, True)`` returns. ``user_id`` is the participant whose
    request drives the pipeline. 1 read + 1-2 writes."""
    if step not in PR_STEPS:
        raise ValueError(f"step must be one of {PR_STEPS}")
    if output is not None:
        _pr_check_size(f"{step} output", output, _PR_DOC_BYTES_CAP)
    ref = _pr_run_ref(run_id)

    def _apply(txn) -> tuple[dict[str, Any], bool]:
        run = _pr_participant_run(txn, ref, user_id)
        _pr_refuse_locked(run)
        if pr_step_done(run, step):
            return run, False
        _pr_refuse_escalated(run)
        now = _now()
        rnd = pr_build_round(run)
        update = _pr_apply_step_patch(run, patch or {}, now)
        update["steps"] = dict(run.get("steps") or {}) | {step: {"round": rnd, "at": now}}
        update["updated_at"] = now
        _pr_check_size("run", run | update, _PR_DOC_BYTES_CAP)
        if output is not None:
            txn.create(ref.collection(_PR_STEP_DOCS).document(f"v{rnd:03d}_{step}"),
                       {"step": step, "round": rnd, "output": output, "at": now})
        txn.update(ref, update)
        return run | update, True

    return _pr_commit(_apply)


def get_pr_step_output(
    run_id: str, step: str, *, user_id: str, round: int | None = None
) -> Optional[dict[str, Any]]:
    """A checkpointed step's stored output (default: the round being built).
    ``None`` when absent or the caller is not a participant. 2 reads."""
    run = get_pr_run(run_id, user_id)
    if run is None or step not in PR_STEPS:
        return None
    rnd = int(round or pr_build_round(run))
    snap = (_db().collection(PR_RUNS).document(run_id).collection(_PR_STEP_DOCS)
            .document(f"v{rnd:03d}_{step}").get())
    return (snap.to_dict() or {}) if snap.exists else None


def create_pr_version(
    run_id: str, *, user_id: str, version: int, package: dict[str, Any],
    fact_checks: list[dict[str, Any]], qa: Any, changes: list[Any],
    quotes: list[dict[str, Any]], created_by: str = PR_AGENT_ACTOR,
    label: str = "", close_step: str | None = None,
) -> dict[str, Any]:
    """Write ``pr_runs/{id}/versions/{version:04d}`` — create-only.

    ``version`` must be exactly ``latest_version + 1`` (else
    :class:`PrStaleVersion`); a version doc that already exists raises
    :class:`PrAlreadyExists` and is never overwritten. In the same transaction
    the run gets ``latest_version``, ``open_verify_count`` (computed here from
    the rows and the package's ``[VERIFY`` markers — never trusted from the
    caller), the quote states from :func:`reset_changed_quotes`, sign-offs
    cleared (they were for the previous version), status ``in_review``, and
    ``close_step`` checkpointed. Refused on an approved run. ``label`` is the
    blueprint's display version (``v1.1``); the integer is the identity.
    1-2 reads + 3 writes."""
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("version must be a positive integer")
    if version > PR_VERSIONS_CAP:
        raise PrCapReached(f"a run holds at most {PR_VERSIONS_CAP} versions")
    if not isinstance(package, dict):
        raise ValueError("package must be a mapping")
    if close_step is not None and close_step not in PR_STEPS:
        raise ValueError(f"close_step must be one of {PR_STEPS}")
    rows = normalize_pr_fact_checks(fact_checks)
    incoming_quotes = _pr_normalize_incoming_quotes(quotes)
    if not isinstance(changes, list):
        raise ValueError("changes must be a list")
    open_verify = count_open_verify(package, rows)
    ref = _pr_run_ref(run_id)
    version_ref = ref.collection(_PR_VERSIONS).document(f"{version:04d}")
    created_by = _pr_clip(created_by, 128) or PR_AGENT_ACTOR

    def _apply(txn) -> dict[str, Any]:
        run = _pr_participant_run(txn, ref, user_id)
        _pr_refuse_locked(run)
        _pr_refuse_escalated(run)
        latest = int(run.get("latest_version") or 0)
        if version != latest + 1:
            raise PrStaleVersion(f"run is at v{latest}; the next version is {latest + 1}, not {version}")
        if version_ref.get(transaction=txn).exists:
            raise PrAlreadyExists(f"version {version} already exists")
        if close_step and pr_step_done(run, close_step):
            raise PrStepAlreadyDone(f"{close_step} is already done for v{version}")
        now = _now()
        doc = {
            "run_id": run_id, "version": version, "label": _pr_clip(label, 40) or f"v{version}",
            "package": package, "fact_checks": rows, "open_verify_count": open_verify,
            "qa": qa, "changes": changes, "quotes": incoming_quotes,
            "created_by": created_by, "created_at": now,
        }
        _pr_check_size(f"version {version}", doc, _PR_DOC_BYTES_CAP)
        update: dict[str, Any] = {
            "latest_version": version,
            "open_verify_count": open_verify,
            "quotes": reset_changed_quotes(run.get("quotes"), quotes),
            "signoffs": {},
            "status": "in_review",
            "updated_at": now,
        }
        if close_step:
            update["steps"] = dict(run.get("steps") or {}) | {close_step: {"round": version, "at": now}}
        txn.create(version_ref, doc)
        update |= _append_pr_event(txn, ref, run, "version_created", created_by, now,
                                   version=version, open_verify_count=open_verify)
        txn.update(ref, update)
        return doc

    return _pr_commit(_apply)


def get_pr_version(run_id: str, version: int, *, user_id: str) -> Optional[dict[str, Any]]:
    """One version, participant-scoped. 2 reads."""
    if get_pr_run(run_id, user_id) is None or not isinstance(version, int) or version < 1:
        return None
    snap = (_db().collection(PR_RUNS).document(run_id).collection(_PR_VERSIONS)
            .document(f"{version:04d}").get())
    return (snap.to_dict() or {}) if snap.exists else None


def list_pr_versions(run_id: str, *, user_id: str) -> Optional[list[dict[str, Any]]]:
    """Version summaries oldest first (no package bodies). 1 + N reads."""
    if get_pr_run(run_id, user_id) is None:
        return None
    docs = (_db().collection(PR_RUNS).document(run_id).collection(_PR_VERSIONS)
            .order_by("version").select(["version", "label", "open_verify_count", "created_by", "created_at"])
            .limit(PR_VERSIONS_CAP).stream())
    return [doc.to_dict() or {} for doc in docs]


# --- human actions -----------------------------------------------------------------

def list_pr_events(
    run_id: str, *, user_id: str, after_seq: int = 0, limit: int = 200
) -> Optional[list[dict[str, Any]]]:
    """The run's event trail in order, participant-scoped. 1 + N reads."""
    if get_pr_run(run_id, user_id) is None:
        return None
    docs = (_db().collection(PR_RUNS).document(run_id).collection(_PR_EVENTS)
            .order_by("seq").start_after({"seq": int(after_seq)})
            .limit(max(1, min(int(limit), 500))).stream())
    return [doc.to_dict() or {} for doc in docs]


def add_pr_comment(
    run_id: str, *, actor_id: str, text: str, version: int | None = None
) -> dict[str, Any]:
    """Append a comment event. 1 read + 2 writes."""
    actor = _pr_require_human(actor_id)
    body = _pr_clip(text, _PR_TEXT_CHARS)
    if not body:
        raise ValueError("a comment needs text")
    ref = _pr_run_ref(run_id)

    def _apply(txn) -> dict[str, Any]:
        run = _pr_participant_run(txn, ref, actor)
        now = _now()
        patch = _append_pr_event(txn, ref, run, "comment", actor, now, text=body, version=version)
        patch["updated_at"] = now
        txn.update(ref, patch)
        return {"seq": patch["event_count"], "kind": "comment", "actor_id": actor,
                "at": now, "text": body, "version": version}

    return _pr_commit(_apply)


def attest_pr_quote(
    run_id: str, quote_id: str, *, actor_id: str, text_hash: str,
    decision: str = "approved", note: str = "",
) -> dict[str, Any]:
    """A person records a quote's approval or rejection (e.g. "approved by
    email 11-02"). ``text_hash`` is the hash the person was shown: if the quote
    changed since, :class:`PrStaleVersion` — nobody approves text they did not
    see. 1 read + 2 writes."""
    actor = _pr_require_human(actor_id)
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be 'approved' or 'rejected'")
    ref = _pr_run_ref(run_id)

    def _apply(txn) -> dict[str, Any]:
        run = _pr_participant_run(txn, ref, actor)
        _pr_refuse_locked(run)
        quotes = [dict(q) for q in run.get("quotes") or []]
        target = next((q for q in quotes if q.get("quote_id") == quote_id), None)
        if target is None:
            raise PrNotFound(f"no quote {quote_id!r} on the current version")
        if target.get("text_hash") != text_hash:
            raise PrStaleVersion(f"quote {quote_id!r} changed since it was shown")
        now = _now()
        target.update(status=decision, attested_by=actor, attested_at=now, note=_pr_clip(note, 1000))
        patch = {"quotes": quotes, "updated_at": now}
        patch |= _append_pr_event(txn, ref, run, f"quote_{decision}", actor, now,
                                  quote_id=quote_id, text_hash=text_hash,
                                  version=run.get("latest_version"), note=target["note"])
        txn.update(ref, patch)
        return run | patch

    return _pr_commit(_apply)


def clear_pr_escalation(run_id: str, *, actor_id: str, note: str) -> dict[str, Any]:
    """A person (comms lead / legal) clears a flagged escalation so drafting
    may continue. 1 read + 2 writes."""
    actor = _pr_require_human(actor_id)
    note = _pr_clip(note, _PR_TEXT_CHARS)
    if not note:
        raise ValueError("clearing an escalation needs a note")
    ref = _pr_run_ref(run_id)

    def _apply(txn) -> dict[str, Any]:
        run = _pr_participant_run(txn, ref, actor)
        _pr_refuse_locked(run)
        esc = dict(run.get("escalation") or {})
        if not esc.get("flagged") or esc.get("cleared_by"):
            raise PrConflict("there is no open escalation on this run")
        now = _now()
        esc.update(cleared_by=actor, cleared_at=now, note=note)
        patch = {"escalation": esc, "updated_at": now,
                 "status": "in_review" if run.get("latest_version") else "in_progress"}
        patch |= _append_pr_event(txn, ref, run, "escalation_cleared", actor, now, note=note)
        txn.update(ref, patch)
        return run | patch

    return _pr_commit(_apply)


def record_pr_signoff(
    run_id: str, *, role: str, actor_id: str, version: int,
    decision: str = "approve", note: str = "",
) -> dict[str, Any]:
    """Record one human sign-off on the CURRENT version, transactionally.

    ``version`` must be ``latest_version`` (else :class:`PrStaleVersion`).
    ``reject`` records the rejection and moves the run to
    ``changes_requested``. ``approve`` on a review role records it; on
    ``final`` it runs :func:`pr_final_blockers` inside the transaction — open
    ``[VERIFY]`` flags, any quote not approved, an uncleared escalation, or a
    review role not approved on this version raises
    :class:`PrSignoffBlocked` and nothing is written. Otherwise the run is
    ``approved`` and locked. This is the ONLY writer of ``approved``; it
    requires a human participant's id. 1 read + 2 writes."""
    actor = _pr_require_human(actor_id)
    if role not in PR_SIGNOFF_ROLES:
        raise ValueError(f"role must be one of {PR_SIGNOFF_ROLES}")
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be 'approve' or 'reject'")
    note = _pr_clip(note, _PR_TEXT_CHARS)
    ref = _pr_run_ref(run_id)

    def _apply(txn) -> dict[str, Any]:
        run = _pr_participant_run(txn, ref, actor)
        _pr_refuse_locked(run)
        latest = int(run.get("latest_version") or 0)
        if latest == 0:
            raise PrSignoffBlocked(["there is no draft version yet"])
        if version != latest:
            raise PrStaleVersion(f"sign-off names v{version}; the current version is v{latest}")
        now = _now()
        entry = {"decision": decision, "actor_id": actor, "version": version, "at": now, "note": note}
        patch: dict[str, Any] = {"signoffs": dict(run.get("signoffs") or {}) | {role: entry},
                                 "updated_at": now}
        if decision == "reject":
            patch["status"] = _RUN_CHANGES_REQUESTED
            kind = "rejection"
        else:
            kind = "signoff"
            if role == "final":
                blockers = pr_final_blockers(run)
                if blockers:
                    raise PrSignoffBlocked(blockers)
                patch.update(status=_RUN_APPROVED, approved_version=version,
                             approved_by=actor, approved_at=now)
        patch |= _append_pr_event(txn, ref, run, kind, actor, now,
                                  role=role, version=version, note=note)
        txn.update(ref, patch)
        return run | patch

    return _pr_commit(_apply)


# --------------------------------------------------------------------------- #
# PR Writer (a13): the router's flow state — step leases and the run's ``flow``
# --------------------------------------------------------------------------- #
# Added by senior-python-backend for the HTTP contract (docs/pr-writer-api.md).
# Everything here writes ONE field the section above never reads or writes:
# ``pr_runs/{id}.flow`` — the step lease/failure table, the clarify card, the
# escalation record (category, excerpt, notes, close/withdraw), the reviewer
# snapshot by role, the package sections and the version metadata the UI
# lists. Status, sign-offs, quote approvals, the escalation flag/clearance,
# evidence, versions and participants stay with the functions above, so the
# human-only invariants there are untouched: nothing here can approve,
# attest, sign off or clear.

_PR_FLOW_BYTES_CAP = 400_000


def _pr_flow_of(run: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(run.get("flow") or {}, default=str))


def _pr_iso_after(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat()


def lease_pr_step(run_id: str, step: str, *, user_id: str, lease_s: float) -> dict[str, Any]:
    """Take the lease on ``step`` so only ONE request spends model calls on it.

    In one transaction, against ``flow.steps[step]``:

    * ``done`` already                        -> ``{"done": True}``, nothing written;
    * ``running`` with a lease still in force -> ``{"acquired": False,
      "retry_after_s": n}``, nothing written;
    * anything else (pending, failed, partial, an expired lease) -> the lease
      is taken: ``state="running"``, ``lease_until`` = now + ``lease_s``, the
      previous error cleared.

    Returns ``{"run", "acquired", "done", "retry_after_s"}``. ``lease_s`` must
    cover the step's whole server deadline, so a lease never expires under a
    request that is still working. 1 read + 0-1 write."""
    if not step or not re.match(r"^[a-z_]{1,32}$", step):
        raise ValueError(f"not a step id: {step!r}")
    if lease_s <= 0:
        raise ValueError("lease_s must be positive")
    ref = _pr_run_ref(run_id)

    def _apply(txn) -> dict[str, Any]:
        run = _pr_participant_run(txn, ref, user_id)
        flow = _pr_flow_of(run)
        entry = dict((flow.get("steps") or {}).get(step) or {})
        now = _now()
        if entry.get("state") == "done":
            return {"run": run, "acquired": False, "done": True, "retry_after_s": 0}
        lease_until = str(entry.get("lease_until") or "")
        if entry.get("state") == "running" and lease_until > now:
            left = datetime.fromisoformat(lease_until) - datetime.fromisoformat(now)
            return {"run": run, "acquired": False, "done": False,
                    "retry_after_s": max(1, int(left.total_seconds()) + 1)}
        entry.update(state="running", lease_until=_pr_iso_after(lease_s), started_at=now,
                     holder=user_id, error=None)
        flow.setdefault("steps", {})[step] = entry
        patch = {"flow": flow, "updated_at": now}
        txn.update(ref, patch)
        return {"run": run | patch, "acquired": True, "done": False, "retry_after_s": 0}

    return _pr_commit(_apply)


def finish_pr_step(
    run_id: str, step: str, *, user_id: str, state: str, result: Any = None,
    error: dict[str, Any] | None = None, flow_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Release the lease on ``step`` with its outcome (``done``, ``partial`` or
    ``failed``) and merge ``flow_patch`` (top-level keys of ``flow``, never
    ``steps``) in the same write. 1 read + 1 write."""
    if state not in ("done", "partial", "failed", "pending"):
        raise ValueError("state must be done, partial, failed or pending")
    if flow_patch and "steps" in flow_patch:
        raise ValueError("flow_patch cannot replace the step table")
    ref = _pr_run_ref(run_id)

    def _apply(txn) -> dict[str, Any]:
        run = _pr_participant_run(txn, ref, user_id)
        flow = _pr_flow_of(run)
        entry = dict((flow.get("steps") or {}).get(step) or {})
        now = _now()
        entry.update(state=state, lease_until=None, holder=None,
                     finished_at=now if state in ("done", "partial") else entry.get("finished_at"),
                     result=result if result is not None else entry.get("result"),
                     error=error)
        flow.setdefault("steps", {})[step] = entry
        flow.update(flow_patch or {})
        _pr_check_size("run flow", flow, _PR_FLOW_BYTES_CAP)
        patch = {"flow": flow, "updated_at": now}
        txn.update(ref, patch)
        return run | patch

    return _pr_commit(_apply)


def update_pr_flow(
    run_id: str, *, actor_id: str, mutate: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    event_kind: str | None = None, event_data: dict[str, Any] | None = None,
    require_human: bool = True,
) -> dict[str, Any]:
    """Read-modify-write ``flow`` in one transaction. ``mutate(run, flow)``
    returns the new flow (it may raise to refuse; nothing is written then).
    ``actor_id`` must be a participant and, unless ``require_human`` is
    False (the router's own bookkeeping after a pipeline write), a person.
    ``event_kind`` appends one create-only event in the same transaction.
    When the new flow names a ``content_type`` the run doc's ``content_type``
    is set to it in the SAME write (unknown id -> ``ValueError``, nothing
    written), so the run doc and ``flow`` never disagree. 1 read + 1-2 writes."""
    actor = _pr_require_human(actor_id) if require_human else str(actor_id or "")
    if event_kind is not None and not re.match(r"^[a-z_]{1,48}$", event_kind):
        raise ValueError(f"event kind not allowed: {event_kind!r}")
    ref = _pr_run_ref(run_id)

    def _apply(txn) -> dict[str, Any]:
        run = _pr_participant_run(txn, ref, actor)
        flow = mutate(run, _pr_flow_of(run))
        if not isinstance(flow, dict):
            raise ValueError("mutate must return the flow mapping")
        _pr_check_size("run flow", flow, _PR_FLOW_BYTES_CAP)
        now = _now()
        patch: dict[str, Any] = {"flow": flow, "updated_at": now}
        if flow.get("content_type") is not None:
            patch["content_type"] = _pr_content_type(flow["content_type"])
        if event_kind is not None:
            patch |= _append_pr_event(txn, ref, run, event_kind, actor, now, **(event_data or {}))
        txn.update(ref, patch)
        return run | patch

    return _pr_commit(_apply)


def get_pr_runs_by_ids(run_ids: list[str], *, user_id: str) -> list[dict[str, Any]]:
    """The full run docs for ``run_ids`` in ONE batched read, keeping only the
    ones ``user_id`` participates in, in the order asked. Absent = no access.
    Used after :func:`list_pr_runs` (whose rows carry list fields only)."""
    ids = [rid for rid in dict.fromkeys(run_ids) if isinstance(rid, str) and _PR_RUN_ID_RE.match(rid)]
    if not ids or not user_id:
        return []
    col = _db().collection(PR_RUNS)
    found: dict[str, dict[str, Any]] = {}
    for snap in _db().get_all([col.document(rid) for rid in ids]):
        data = (snap.to_dict() or {}) if snap.exists else None
        if data is not None and user_id in (data.get("participants") or []):
            found[snap.id] = data | {"id": snap.id}
    return [found[rid] for rid in ids if rid in found]


def flag_pr_escalation(run_id: str, *, actor_id: str, reason: str) -> dict[str, Any]:
    """Pause a run for comms and legal AFTER intake — when the owner's clarify
    answers screen as sensitive. :func:`complete_pr_step` can only raise the
    flag with a step that is not yet done, and by then the intake step is.

    Raise-only: it sets ``escalation.flagged`` (and status ``escalated``) and
    can never clear, approve or sign anything — clearing stays
    :func:`clear_pr_escalation`'s, by a person. An escalation that is already
    open is left as it is. Refused on an approved run. 1 read + 2 writes."""
    actor = _pr_require_human(actor_id)
    reason = _pr_clip(reason, _PR_TEXT_CHARS) or "escalated"
    ref = _pr_run_ref(run_id)

    def _apply(txn) -> dict[str, Any]:
        run = _pr_participant_run(txn, ref, actor)
        _pr_refuse_locked(run)
        esc = run.get("escalation") or {}
        if esc.get("flagged") and not esc.get("cleared_by"):
            return run
        now = _now()
        patch: dict[str, Any] = {
            "escalation": {"flagged": True, "reason": reason, "flagged_at": now, "cleared_by": None,
                           "cleared_at": None, "note": ""},
            "status": "escalated", "updated_at": now,
        }
        patch |= _append_pr_event(txn, ref, run, "escalation_flagged", actor, now, reason=reason)
        txn.update(ref, patch)
        return run | patch

    return _pr_commit(_apply)
