# backend/app/services/gd_brand_source.py
"""Firestore -> Graphics Designer templated-brand spec source (Stage 4).

Single responsibility: turn Firestore ``brands`` docs carrying a
``brand_metadata.gd_spec`` into the spec-dict contract
``graphics_designer_agent.templated_brands.build_templated_pack`` consumes,
with every font file the spec needs locally materialized under the exact
directory ``build_templated_pack`` derives ``fonts_dir`` from
(``<brands dir>/<spec id>/fonts/<file>``).

This is the ONLY place in the app that is allowed to import the GD registry
for injection purposes — the GD package itself imports nothing from here (see
``graphics_designer_agent.registry.register_dynamic_source``). Wired at app
startup (``app/main.py``).

Module-level seams, monkeypatched directly in tests:
- ``_list_brands()``   -- all brand docs (id + brand_metadata)
- ``_fonts_root()``    -- the templated-brands directory
- ``_download(uri, dest)`` -- gs:// URI -> bytes -> write to dest
- ``_copy_font(src, dest)`` -- one bundled fallback font file into place
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.services import firestore_repo, storage
from graphics_designer_agent.templated_brands import _BEVIETNAM_FULL, _BRANDS_DIR

logger = logging.getLogger("agentos.gd_brand_source")

# On-disk home of the bundled Be Vietnam set: the medvirtual templated brand
# ships all 14 .ttf faces under the REAL brands dir (deliberately _BRANDS_DIR,
# not _fonts_root(), so the byte source stays valid when tests repoint the
# destination root). This is where fallback font bytes are copied FROM.
_BEVIETNAM_SRC_DIR = _BRANDS_DIR / "medvirtual" / "fonts"


def _list_brands() -> list[dict]:
    """Seam: all brand docs. Delegates to the existing Firestore helper
    (``app/routers/brands.py`` uses the same one to list brands for the UI),
    bypassing its 60s in-process cache: the enrichment CLI writes from a
    separate process, so an admin-triggered refresh must always see the
    freshest Firestore state rather than a cache window it can't invalidate."""
    return firestore_repo.list_brands(use_cache=False)


def _fonts_root() -> Path:
    """Seam: the templated-brands directory — the same constant
    ``templated_brands.build_templated_pack`` derives ``fonts_dir`` from.
    Fonts materialize to ``<this>/<spec id>/fonts/<file>``."""
    return _BRANDS_DIR


def _download(uri: str, dest: Path) -> None:
    """Seam: gs:// URI -> bytes -> write to dest. Raises on any failure; the
    caller (``_materialize_fonts``) decides what a failure means."""
    dest.write_bytes(storage.download_bytes(uri))


def _copy_font(src: Path, dest: Path) -> None:
    """Seam: copy one bundled fallback font file into place."""
    shutil.copyfile(src, dest)


def _materialize_bevietnam_fallback(spec_id: str) -> None:
    """Copy the bundled Be Vietnam .ttf BYTES into
    ``_fonts_root()/<spec_id>/fonts/`` so Stage-3 text rendering works for a
    fallback brand — substituting the metadata alone would leave ``fonts_dir``
    pointing at files that don't exist. Per-file best-effort: a copy failure
    is logged and skipped, never raised (the registry/startup guarantee wins
    over any individual face)."""
    fonts_dir = _fonts_root() / spec_id / "fonts"
    for variant in _BEVIETNAM_FULL:
        dest = fonts_dir / variant["file"]
        if dest.exists():
            continue
        try:
            fonts_dir.mkdir(parents=True, exist_ok=True)
            _copy_font(_BEVIETNAM_SRC_DIR / variant["file"], dest)
        except Exception as exc:  # noqa: BLE001 - one face must not break the brand
            logger.warning(
                "gd_brand_source: fallback font copy failed for brand %r file %r: %s",
                spec_id, variant["file"], exc,
            )


def _materialize_fonts(spec: dict, font_file_uris: list[str]) -> dict:
    """Ensure every ``font_variants[].file`` exists locally under
    ``_fonts_root()/<spec id>/fonts/``, downloading whatever is missing by
    matching the enrichment URIs' basename. On ANY download failure, or a
    needed file with no matching URI, the WHOLE spec's font set is replaced
    with the full Be Vietnam fallback (never a partially-materialized brand
    font) and a warning is logged.
    """
    uris_by_basename = {uri.rstrip("/").rsplit("/", 1)[-1]: uri for uri in font_file_uris}
    fonts_dir = _fonts_root() / spec["id"] / "fonts"
    try:
        for variant in spec["font_variants"]:
            file_name = variant["file"]
            dest = fonts_dir / file_name
            if dest.exists():
                continue
            uri = uris_by_basename.get(file_name)
            if uri is None:
                raise FileNotFoundError(f"no enrichment font URI for {file_name!r}")
            fonts_dir.mkdir(parents=True, exist_ok=True)
            _download(uri, dest)
    except Exception as exc:  # noqa: BLE001 - any font problem -> full fallback, not a crash
        logger.warning(
            "gd_brand_source: font materialization failed for brand %r (%s) — "
            "substituting the Be Vietnam fallback set",
            spec.get("id"), exc,
        )
        spec = dict(spec)
        spec["font_variants"] = list(_BEVIETNAM_FULL)
        spec["font_family"] = "Be Vietnam"
        spec["default_font"] = "Be Vietnam Bold"
        _materialize_bevietnam_fallback(spec["id"])
    return spec


def firestore_spec_source() -> list[dict]:
    """``registry.register_dynamic_source`` callable: Firestore brand docs
    with a baked ``brand_metadata.gd_spec`` -> spec dicts with locally-present
    fonts. Never raises — any Firestore error (or anything else going wrong
    while listing/reading brands) yields ``[]`` with a logged warning, so a
    down Firestore or a malformed brand can never break app startup or the
    registry.
    """
    try:
        specs: list[dict] = []
        for doc in _list_brands():
            # Per-doc fault isolation: one malformed brand doc is logged and
            # skipped — it must never drop the other, valid dynamic brands.
            try:
                meta = doc.get("brand_metadata") or {}
                gd_spec = meta.get("gd_spec")
                if not gd_spec:
                    continue
                font_file_uris = (meta.get("enrichment") or {}).get("font_files") or []
                spec = _materialize_fonts(dict(gd_spec), font_file_uris)
                # Per-brand preset libraries (Unit P1): gd_spec itself never
                # carries these keys (gd_spec_builder.build_gd_spec never
                # emits them, so a later re-enrich can't clobber curated
                # content) — they live in their own metadata key, admin-
                # refreshable independent of enrichment, and win over any
                # same-name keys already on the spec.
                gd_presets = meta.get("gd_presets")
                if gd_presets:
                    spec["extra_gradients"] = gd_presets.get("extra_gradients", [])
                    spec["curated_elements"] = gd_presets.get("curated_elements", [])
                specs.append(spec)
            except Exception as exc:  # noqa: BLE001 - isolate the bad doc, keep the rest
                doc_id = doc.get("id") if isinstance(doc, dict) else None
                logger.warning(
                    "gd_brand_source: brand doc %r skipped (malformed): %s", doc_id, exc
                )
                continue
        return specs
    except Exception as exc:  # noqa: BLE001 - Firestore must never break startup/the registry
        logger.warning("gd_brand_source: firestore_spec_source failed: %s", exc)
        return []
