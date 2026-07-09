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

Three module-level seams, monkeypatched directly in tests:
- ``_list_brands()``   -- all brand docs (id + brand_metadata)
- ``_fonts_root()``    -- the templated-brands directory
- ``_download(uri, dest)`` -- gs:// URI -> bytes -> write to dest
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.services import firestore_repo, storage
from graphics_designer_agent.templated_brands import _BEVIETNAM_FULL, _BRANDS_DIR

logger = logging.getLogger("agentos.gd_brand_source")


def _list_brands() -> list[dict]:
    """Seam: all brand docs. Delegates to the existing Firestore helper
    (``app/routers/brands.py`` uses the same one to list brands for the UI)."""
    return firestore_repo.list_brands()


def _fonts_root() -> Path:
    """Seam: the templated-brands directory — the same constant
    ``templated_brands.build_templated_pack`` derives ``fonts_dir`` from.
    Fonts materialize to ``<this>/<spec id>/fonts/<file>``."""
    return _BRANDS_DIR


def _download(uri: str, dest: Path) -> None:
    """Seam: gs:// URI -> bytes -> write to dest. Raises on any failure; the
    caller (``_materialize_fonts``) decides what a failure means."""
    dest.write_bytes(storage.download_bytes(uri))


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
            meta = doc.get("brand_metadata") or {}
            gd_spec = meta.get("gd_spec")
            if not gd_spec:
                continue
            font_file_uris = (meta.get("enrichment") or {}).get("font_files") or []
            specs.append(_materialize_fonts(dict(gd_spec), font_file_uris))
        return specs
    except Exception as exc:  # noqa: BLE001 - Firestore must never break startup/the registry
        logger.warning("gd_brand_source: firestore_spec_source failed: %s", exc)
        return []
