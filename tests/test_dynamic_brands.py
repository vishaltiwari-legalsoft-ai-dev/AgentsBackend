# backend/tests/test_dynamic_brands.py
"""Unit E: registry dynamic-source injection (Task 10) + Firestore brand spec
source + font materialization (Task 11).

Golden id pinning: current STATIC pack ids, printed via
`.venv\\Scripts\\python.exe -c "import app; from graphics_designer_agent import
registry; print([p['id'] for p in registry.list_packs()])"` ->
['legalsoft', 'medvirtual', 'remote_attorneys']. Pinned in
`test_golden_flag_off_registry_unchanged` below so any accidental change to
the static registry (byte-identical guarantee) fails loudly.
"""
from __future__ import annotations

import app  # noqa: F401 - side effect: registers agent roots on sys.path (see app/__init__.py)
import pytest
from graphics_designer_agent import registry


@pytest.fixture(autouse=True)
def clean_registry():
    registry.refresh()
    yield
    registry.register_dynamic_source(None)
    registry.refresh()


# --------------------------------------------------------------------------- #
# Task 10 — registry dynamic-source injection (flag-gated, static-wins,
# fault-isolated)
# --------------------------------------------------------------------------- #

def test_golden_flag_off_registry_unchanged(monkeypatch):
    monkeypatch.delenv("GD_DYNAMIC_BRANDS", raising=False)
    registry.register_dynamic_source(lambda: [{"id": "ghost"}])
    ids = {p["id"] for p in registry.list_packs()}
    assert ids == {"legalsoft", "medvirtual", "remote_attorneys"}  # exact current ids


def test_flag_on_adds_dynamic_brand(monkeypatch, valid_dyn_spec):
    monkeypatch.setenv("GD_DYNAMIC_BRANDS", "1")
    registry.register_dynamic_source(lambda: [valid_dyn_spec])
    assert valid_dyn_spec["id"] in {p["id"] for p in registry.list_packs()}


def test_static_wins_on_id_collision(monkeypatch, valid_dyn_spec):
    monkeypatch.setenv("GD_DYNAMIC_BRANDS", "1")
    valid_dyn_spec["id"] = "legalsoft"
    registry.register_dynamic_source(lambda: [valid_dyn_spec])
    pack = registry.get_pack("legalsoft")
    assert pack.name == "Legal Soft"  # static pack untouched


def test_broken_spec_skipped_not_fatal(monkeypatch):
    monkeypatch.setenv("GD_DYNAMIC_BRANDS", "1")
    registry.register_dynamic_source(lambda: [{"id": "broken"}])  # missing keys
    assert registry.list_packs()  # registry still works


# --------------------------------------------------------------------------- #
# Task 11 — firestore_spec_source + font materialization
# --------------------------------------------------------------------------- #

def test_firestore_spec_source_yields_specs_with_local_fonts(monkeypatch, tmp_path, valid_dyn_spec):
    from app.services import gd_brand_source

    # valid_dyn_spec's font_variants are the full Be Vietnam set (14 faces,
    # since its BrandFolder carries no font_files) — cover every one of them
    # with a matching enrichment URI so this is a true happy path (no fallback).
    font_files = [
        f"gs://bucket/brands/b1/fonts/{v['file']}" for v in valid_dyn_spec["font_variants"]
    ]
    docs = [
        {"id": "b1", "brand_metadata": {
            "gd_spec": valid_dyn_spec,
            "enrichment": {"font_files": font_files},
        }},
        {"id": "b2", "brand_metadata": {}},  # no gd_spec -> skipped
    ]
    monkeypatch.setattr(gd_brand_source, "_list_brands", lambda: docs)
    monkeypatch.setattr(gd_brand_source, "_fonts_root", lambda: tmp_path)
    monkeypatch.setattr(
        gd_brand_source, "_download", lambda uri, dest: dest.write_bytes(b"font")
    )

    specs = gd_brand_source.firestore_spec_source()
    assert len(specs) == 1
    first_file = valid_dyn_spec["font_variants"][0]["file"]
    assert (tmp_path / valid_dyn_spec["id"] / "fonts" / first_file).exists()
    assert specs[0]["font_variants"] == valid_dyn_spec["font_variants"]  # untouched, not fallback


def test_firestore_spec_source_falls_back_to_bevietnam_on_download_failure(
    monkeypatch, tmp_path, valid_dyn_spec
):
    from app.services import gd_brand_source
    from graphics_designer_agent.templated_brands import _BEVIETNAM_FULL

    # Give the spec a distinctive, non-Be-Vietnam font so a successful
    # substitution is actually observable (not coincidentally equal).
    valid_dyn_spec["font_variants"] = [{"name": "Custom Bold", "file": "Custom-Bold.ttf"}]
    valid_dyn_spec["font_family"] = "Custom"
    valid_dyn_spec["default_font"] = "Custom Bold"

    docs = [
        {"id": "b1", "brand_metadata": {
            "gd_spec": valid_dyn_spec,
            "enrichment": {"font_files": ["gs://bucket/brands/b1/fonts/Custom-Bold.ttf"]},
        }},
    ]
    monkeypatch.setattr(gd_brand_source, "_list_brands", lambda: docs)
    monkeypatch.setattr(gd_brand_source, "_fonts_root", lambda: tmp_path)

    def boom(uri, dest):
        raise RuntimeError("network down")

    monkeypatch.setattr(gd_brand_source, "_download", boom)

    specs = gd_brand_source.firestore_spec_source()
    assert len(specs) == 1  # the spec is still yielded, not dropped
    assert specs[0]["font_variants"] == _BEVIETNAM_FULL
    assert specs[0]["font_family"] == "Be Vietnam"
    assert specs[0]["default_font"] == "Be Vietnam Bold"

    # The fallback must also materialize the actual Be Vietnam BYTES (copied
    # from the bundled in-repo medvirtual fonts dir — offline, no network) so
    # Stage-3 text rendering works for the fallback brand, not just its
    # metadata. Every face of the substituted set must be locally present.
    fonts_dir = tmp_path / valid_dyn_spec["id"] / "fonts"
    for variant in _BEVIETNAM_FULL:
        assert (fonts_dir / variant["file"]).exists(), variant["file"]
        assert (fonts_dir / variant["file"]).stat().st_size > 0


def test_firestore_spec_source_isolates_malformed_doc(monkeypatch, tmp_path, valid_dyn_spec):
    """One malformed brand doc must not drop the other, valid dynamic brands."""
    from app.services import gd_brand_source

    font_files = [
        f"gs://bucket/brands/good/fonts/{v['file']}" for v in valid_dyn_spec["font_variants"]
    ]
    docs = [
        {"id": "bad", "brand_metadata": "not-a-dict"},  # malformed -> skipped + logged
        {"id": "good", "brand_metadata": {
            "gd_spec": valid_dyn_spec,
            "enrichment": {"font_files": font_files},
        }},
    ]
    monkeypatch.setattr(gd_brand_source, "_list_brands", lambda: docs)
    monkeypatch.setattr(gd_brand_source, "_fonts_root", lambda: tmp_path)
    monkeypatch.setattr(
        gd_brand_source, "_download", lambda uri, dest: dest.write_bytes(b"font")
    )

    specs = gd_brand_source.firestore_spec_source()
    assert len(specs) == 1
    assert specs[0]["id"] == valid_dyn_spec["id"]


def test_firestore_spec_source_returns_empty_on_firestore_error(monkeypatch):
    """A Firestore failure yields [] with a warning — never an exception."""
    from app.services import gd_brand_source

    def boom():
        raise RuntimeError("firestore down")

    monkeypatch.setattr(gd_brand_source, "_list_brands", boom)
    assert gd_brand_source.firestore_spec_source() == []
