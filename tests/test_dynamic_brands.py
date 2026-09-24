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


# --------------------------------------------------------------------------- #
# Task 12 — _list_brands() bypasses firestore_repo's 60s brands cache
# (Finding 3): the enrichment CLI writes from a separate process, so the
# admin refresh-packs endpoint must always rebuild from fresh Firestore data,
# never a cache window it can't invalidate.
# --------------------------------------------------------------------------- #

def test_list_brands_bypasses_stale_firestore_cache(monkeypatch):
    import time

    from app.services import firestore_repo, gd_brand_source

    class _FakeSnap:
        def __init__(self, doc_id, data):
            self.id = doc_id
            self._data = data

        def to_dict(self):
            return dict(self._data)

    class _FakeCol:
        def __init__(self, docs):
            self._docs = docs

        def order_by(self, field):
            return self

        def stream(self):
            return [_FakeSnap(d["id"], {"brand_name": d["brand_name"]}) for d in self._docs]

    class _FakeDb:
        def __init__(self, docs):
            self._docs = docs

        def collection(self, name):
            assert name == "brands"
            return _FakeCol(self._docs)

    monkeypatch.setattr(
        firestore_repo, "_db",
        lambda: _FakeDb([{"id": "fresh1", "brand_name": "Fresh Brand"}]))
    # Seed a stale-but-still-within-TTL cache entry directly — if
    # `_list_brands()` honored the default `use_cache=True`, it would return
    # this stale value instead of hitting the fake db above.
    monkeypatch.setattr(
        firestore_repo, "_brands_cache",
        (time.monotonic(), [{"id": "stale1", "brand_name": "Stale Brand"}]))

    result = gd_brand_source._list_brands()
    assert [b["id"] for b in result] == ["fresh1"]


def test_firestore_repo_builtin_pack_ids_match_the_static_registry(monkeypatch):
    """``firestore_repo.BUILTIN_GD_PACK_IDS`` is a constant so the store never
    imports the GD pipeline; this keeps it honest against the real registry
    (self-serve create refuses these slugs)."""
    from app.services import firestore_repo

    monkeypatch.delenv("GD_DYNAMIC_BRANDS", raising=False)
    assert set(firestore_repo.BUILTIN_GD_PACK_IDS) == {p["id"] for p in registry.list_packs()}


# --------------------------------------------------------------------------- #
# Self-serve brands (2026-09-25): strict lookup, version-driven rebuild, and
# the spec a browser-made brand becomes.
# --------------------------------------------------------------------------- #

@pytest.fixture()
def version_source():
    """A controllable brand-set version; whatever ``app.main`` wired is put back."""
    prev = registry._VERSION_SOURCE
    state = {"version": 1}
    registry.register_version_source(lambda: state["version"])
    yield state
    registry.register_version_source(prev)


def test_unknown_brand_raises_instead_of_falling_back_to_legalsoft(monkeypatch):
    monkeypatch.delenv("GD_DYNAMIC_BRANDS", raising=False)
    with pytest.raises(registry.UnknownBrand):
        registry.get_pack("ghost")
    assert registry.get_pack(None).id == "legalsoft"  # legacy runs still resolve


def test_registry_rebuilds_when_the_brand_version_moves(monkeypatch, valid_dyn_spec, version_source):
    """A brand created on another instance shows up here once the version it
    bumped is observed — without anyone calling refresh() on this instance."""
    monkeypatch.setenv("GD_DYNAMIC_BRANDS", "1")
    specs: list[dict] = []
    registry.register_dynamic_source(lambda: list(specs))
    assert valid_dyn_spec["id"] not in {p["id"] for p in registry.list_packs()}

    specs.append(valid_dyn_spec)
    monkeypatch.setattr(registry, "_version_checked_at", None)   # past the throttle
    assert valid_dyn_spec["id"] not in {p["id"] for p in registry.list_packs()}  # same version: cached

    version_source["version"] += 1
    monkeypatch.setattr(registry, "_version_checked_at", None)
    assert valid_dyn_spec["id"] in {p["id"] for p in registry.list_packs()}
    assert registry._loaded_version == 2


def test_version_check_is_throttled_to_one_read_per_window(monkeypatch, valid_dyn_spec, version_source):
    monkeypatch.setenv("GD_DYNAMIC_BRANDS", "1")
    specs: list[dict] = []
    registry.register_dynamic_source(lambda: list(specs))
    registry.list_packs()
    specs.append(valid_dyn_spec)
    version_source["version"] += 1
    # inside the window the cached comparison serves — one doc read per ~10 s
    assert valid_dyn_spec["id"] not in {p["id"] for p in registry.list_packs()}
    monkeypatch.setattr(registry, "VERSION_CHECK_SECONDS", 0.0)
    assert valid_dyn_spec["id"] in {p["id"] for p in registry.list_packs()}


def test_a_failing_version_source_keeps_the_loaded_packs(monkeypatch, version_source):
    monkeypatch.setenv("GD_DYNAMIC_BRANDS", "1")
    registry.register_dynamic_source(lambda: [])
    registry.list_packs()

    def boom():
        raise RuntimeError("firestore down")

    registry.register_version_source(boom)
    assert {"legalsoft", "medvirtual", "remote_attorneys"} <= {p["id"] for p in registry.list_packs()}
    assert registry.get_pack("medvirtual").name == "MedVirtual"


def test_version_source_is_never_consulted_while_dynamic_brands_are_off(monkeypatch, version_source):
    monkeypatch.delenv("GD_DYNAMIC_BRANDS", raising=False)
    calls = []
    registry.register_version_source(lambda: calls.append(1) or 1)
    registry.list_packs()
    registry.list_packs()
    assert calls == []


def test_self_serve_spec_is_a_pack_and_keeps_the_users_hex_verbatim():
    from app.services.gd_spec_builder import build_self_serve_spec, font_variant_for_upload
    from graphics_designer_agent.templated_brands import _BEVIETNAM_FULL, build_templated_pack

    spec = build_self_serve_spec("Acme Co", "acme-co", primary_colors=["#1746a2"])
    pack = build_templated_pack(spec)
    assert pack.id == "acme-co" and spec["firestore_brand_id"] == "acme-co"
    assert "#1746A2" in spec["palette"].values()         # the one colour typed, as typed
    assert "#1746A2" in pack.brand_gradient_hexes
    assert spec["font_variants"] == _BEVIETNAM_FULL       # no fonts uploaded → bundled set
    assert spec["font_fallback"] is True

    three = build_self_serve_spec("Acme Co", "acme-co", primary_colors=["#112233", "#445566"],
                                  accent_colors=["#FF0000"])
    assert {"#112233", "#445566", "#FF0000"} <= set(three["palette"].values())

    # uploaded fonts win, and are referenced by the STORED hash names — the
    # basename gd_brand_source matches the enrichment URIs against.
    variant = font_variant_for_upload("Inter-Bold.ttf", "0123456789abcdef.ttf")
    assert variant == {"name": "Inter Bold", "family": "Inter", "file": "0123456789abcdef.ttf",
                       "weight": 700, "style": "normal", "source_name": "Inter-Bold.ttf"}
    with_font = build_self_serve_spec("Acme Co", "acme-co", primary_colors=["#1746A2"],
                                      font_variants=[variant])
    assert with_font["font_variants"] == [variant]
    assert with_font["default_font"] == "Inter Bold" and with_font["font_family"] == "Inter"
    assert with_font["font_fallback"] is False
    assert build_templated_pack(with_font).font_file("Inter Bold") == "0123456789abcdef.ttf"


def test_uploaded_font_materializes_by_the_stored_basename(monkeypatch, tmp_path):
    """End to end of the db engineer's flag: the recorded URI's basename is the
    ``font_variants[].file``, so the pipeline downloads exactly that object."""
    from app.services import gd_brand_source
    from app.services.gd_spec_builder import build_self_serve_spec, font_variant_for_upload

    spec = build_self_serve_spec(
        "Acme Co", "acme-co", primary_colors=["#1746A2"],
        font_variants=[font_variant_for_upload("Inter-Bold.ttf", "abcdefabcdefabcd.ttf")])
    docs = [{"id": "acme-co", "brand_metadata": {
        "gd_spec": spec,
        "enrichment": {"font_files": ["gs://b/brands/acme-co/fonts/abcdefabcdefabcd.ttf"]},
    }}]
    fetched: list[str] = []
    monkeypatch.setattr(gd_brand_source, "_list_brands", lambda: docs)
    monkeypatch.setattr(gd_brand_source, "_fonts_root", lambda: tmp_path)
    monkeypatch.setattr(gd_brand_source, "_download",
                        lambda uri, dest: (fetched.append(uri), dest.write_bytes(b"font")))

    specs = gd_brand_source.firestore_spec_source()
    assert fetched == ["gs://b/brands/acme-co/fonts/abcdefabcdefabcd.ttf"]
    assert specs[0]["font_variants"][0]["file"] == "abcdefabcdefabcd.ttf"  # not the fallback
    assert (tmp_path / "acme-co" / "fonts" / "abcdefabcdefabcd.ttf").read_bytes() == b"font"
