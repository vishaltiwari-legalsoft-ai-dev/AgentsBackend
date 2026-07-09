# backend/tests/test_brand_enrichment.py
import json
from pathlib import Path

import pytest

from app.services import firestore_repo


class _FakeDoc:
    def __init__(self, store, doc_id):
        self._store, self._id = store, doc_id

    def set(self, payload, merge=False):
        cur = self._store.setdefault(self._id, {})
        if merge:
            _deep_merge(cur, payload)
        else:
            self._store[self._id] = payload

    def get(self):
        class _Snap:
            exists = self._id in self._store
            id = self._id

            def to_dict(inner):
                return dict(self._store.get(self._id, {}))
        return _Snap()


def _deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


class _FakeSnap:
    """A stream()-yielded query result — distinct from _FakeDoc.get()'s snapshot
    (which needs the live-lookup closure); this one is a frozen copy."""
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _FakeQuery:
    """Just enough of the Firestore query surface for find_brand_by_name:
    one .where(filter=FieldFilter(field, '==', value)), then .limit(n).stream()."""
    def __init__(self, store, predicate=None, limit=None):
        self._store = store
        self._predicate = predicate
        self._limit = limit

    def where(self, filter):
        field, op, value = filter.field_path, filter.op_string, filter.value
        if op != "==":
            raise NotImplementedError(f"fake Firestore: unsupported op {op!r}")
        prev = self._predicate

        def combined(data):
            return (prev is None or prev(data)) and data.get(field) == value

        return _FakeQuery(self._store, combined, self._limit)

    def limit(self, n):
        return _FakeQuery(self._store, self._predicate, n)

    def stream(self):
        out = []
        for doc_id, data in self._store.items():
            if self._predicate is None or self._predicate(data):
                out.append(_FakeSnap(doc_id, data))
                if self._limit is not None and len(out) >= self._limit:
                    break
        return out


class _FakeCol:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return _FakeDoc(self._store, doc_id)

    def where(self, filter):
        return _FakeQuery(self._store).where(filter)


class _FakeDb:
    def __init__(self):
        self.brands = {}

    def collection(self, name):
        return _FakeCol(self.brands)


def test_update_brand_metadata_merges_without_clobbering(monkeypatch):
    db = _FakeDb()
    db.brands["b1"] = {"brand_name": "Acme",
                        "brand_metadata": {"source_folder": "Acme", "fonts": ["Old Font"]}}
    monkeypatch.setattr(firestore_repo, "_db", lambda: db)

    firestore_repo.update_brand_metadata("b1", {"primary_colors": ["#1A2B3C"],
                                                 "fonts": ["Inter Bold"]})

    meta = db.brands["b1"]["brand_metadata"]
    assert meta["source_folder"] == "Acme"          # untouched key preserved
    assert meta["primary_colors"] == ["#1A2B3C"]    # new key added
    assert meta["fonts"] == ["Inter Bold"]          # owned key updated
    assert db.brands["b1"]["brand_name"] == "Acme"  # sibling doc keys preserved


# --------------------------------------------------------------------------- #
# Task 7 — enrichment orchestrator
# --------------------------------------------------------------------------- #

def _make_kit_pdf(path: Path) -> Path:
    """Same drawString content as the Task 1 fixture in test_brand_kit_extractor.py."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 14)
    c.drawString(72, 760, "Brand Colors")
    c.drawString(72, 730, "Primary  #1A2B3C")
    c.drawString(72, 700, "Secondary  #24B9CE")
    c.drawString(72, 670, "Accent HEX 19B1E3")
    c.drawString(72, 640, "Ink  R: 22, G: 21, B: 17")
    c.showPage()
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 760, "Typography: Be Vietnam Pro")
    c.save()
    return path


def _brand_tree(tmp_path: Path) -> Path:
    """Build <root>/Acme Health/Brand Kit/kit.pdf with the reportlab fixture."""
    kit_dir = tmp_path / "root" / "Acme Health" / "Brand Kit"
    kit_dir.mkdir(parents=True)
    _make_kit_pdf(kit_dir / "Acme Brand Guidelines.pdf")
    return tmp_path / "root"


def test_enrich_root_dry_run_writes_nothing(monkeypatch, tmp_path):
    from app.services import brand_enrichment
    db = _FakeDb()
    monkeypatch.setattr(firestore_repo, "_db", lambda: db)

    reports = brand_enrichment.enrich_root(_brand_tree(tmp_path), dry_run=True,
                                            now_iso="2026-07-09T00:00:00Z")
    assert reports[0]["wrote"] is False
    assert reports[0]["patch"]["primary_colors"] == ["#1A2B3C"]
    assert db.brands == {}                                 # nothing written


def test_enrich_root_live_creates_new_brand(monkeypatch, tmp_path):
    from app.services import brand_enrichment
    db = _FakeDb()
    monkeypatch.setattr(firestore_repo, "_db", lambda: db)
    monkeypatch.setattr(firestore_repo, "find_brand_by_name", lambda name: None)
    created = {}

    def fake_upsert(name, meta):
        created.update({"name": name, "meta": meta})
        return {"id": "new1", "brand_name": name}
    monkeypatch.setattr(firestore_repo, "upsert_brand", fake_upsert)

    updated = {}

    def fake_update(brand_id, patch):
        updated.update({"brand_id": brand_id, "patch": patch})
        return {}
    monkeypatch.setattr(firestore_repo, "update_brand_metadata", fake_update)

    reports = brand_enrichment.enrich_root(_brand_tree(tmp_path), dry_run=False,
                                            now_iso="2026-07-09T00:00:00Z")
    assert reports[0]["wrote"] is True and reports[0]["brand_id"] == "new1"
    assert created["meta"] == {}                            # allocate-only upsert (R2d)
    assert updated["brand_id"] == "new1"
    assert updated["patch"]["fonts"]                        # patch flowed through update_brand_metadata


def test_enrich_root_skips_brand_without_kit(monkeypatch, tmp_path):
    from app.services import brand_enrichment
    (tmp_path / "root" / "NoKit Co" / "Social").mkdir(parents=True)
    (tmp_path / "root" / "NoKit Co" / "Social" / "a.png").write_bytes(b"x")
    reports = brand_enrichment.enrich_root(tmp_path / "root", dry_run=True,
                                            now_iso="2026-07-09T00:00:00Z")
    assert reports[0]["skipped_reason"] == "no extractable sources"  # R2b reason string


def test_profile_to_patch_drops_none_and_empty_keys(tmp_path):
    """R2c patch hygiene: never write clobbering empties (tone_of_voice=None,
    empty secondary_colors) — but brand_kit_source and enrichment always land."""
    from app.services.brand_enrichment import profile_to_patch
    from app.services.brand_folder_scanner import BrandFolder
    from app.services.brand_kit_extractor import BrandKitProfile

    profile = BrandKitProfile(
        brand_name="Acme", colors=[], fonts=[],
        primary_colors=["#1A2B3C"], secondary_colors=[], accent_colors=[],
        font_family=None, tone_of_voice=None, palette={},
        confidence="low", provenance={"pages_scanned": 0},
    )
    folder = BrandFolder(brand_name="Acme", root=tmp_path, kit_pdf=None)

    patch = profile_to_patch(profile, folder, now_iso="2026-07-09T00:00:00Z")

    assert "tone_of_voice" not in patch
    assert "secondary_colors" not in patch
    assert "accent_colors" not in patch
    assert "brand_kit_source" not in patch      # folder.kit_pdf is None
    assert patch["primary_colors"] == ["#1A2B3C"]
    assert "enrichment" in patch                # always written
    assert patch["enrichment"]["palette"] == {}


def test_profile_to_patch_includes_brand_kit_source_when_kit_pdf_present(tmp_path):
    from app.services.brand_enrichment import profile_to_patch
    from app.services.brand_folder_scanner import BrandFolder
    from app.services.brand_kit_extractor import BrandKitProfile

    kit_pdf = tmp_path / "kit.pdf"
    kit_pdf.write_bytes(b"x")
    profile = BrandKitProfile(
        brand_name="Acme", colors=[], fonts=[],
        primary_colors=["#1A2B3C"], secondary_colors=[], accent_colors=[],
        font_family=None, tone_of_voice=None, palette={},
        confidence="low", provenance={"pages_scanned": 0},
    )
    folder = BrandFolder(brand_name="Acme", root=tmp_path, kit_pdf=kit_pdf)

    patch = profile_to_patch(profile, folder, now_iso="2026-07-09T00:00:00Z")
    assert patch["brand_kit_source"] == str(kit_pdf)


def test_source_ladder_flags_reflect_contribution_not_presence(tmp_path):
    """The flags mean "this rung CONTRIBUTED hits to the merged profile":
    a ColorHit context starting "svg:" -> svg, "pixel-share=" -> pixel, any
    other context (while a kit PDF was present) -> kit_pdf; a FontHit whose
    raw_name is a .ttf/.otf file name -> font_files (PDF-embedded fonts carry
    basefont names instead)."""
    from app.services.brand_enrichment import profile_to_patch
    from app.services.brand_folder_scanner import BrandFolder
    from app.services.brand_kit_extractor import BrandKitProfile, ColorHit, FontHit

    kit_pdf = tmp_path / "kit.pdf"
    kit_pdf.write_bytes(b"x")
    profile = BrandKitProfile(
        brand_name="Acme",
        colors=[ColorHit(hex="#1A2B3C", page=1, context="Primary  #1A2B3C"),
                ColorHit(hex="#00FF00", page=0, context="svg:brand.svg")],
        fonts=[FontHit(family="Inter", style="Bold",
                       raw_name="Inter-Bold.ttf", embedded=True)],
        primary_colors=["#1A2B3C"], secondary_colors=[], accent_colors=[],
        font_family="Inter", tone_of_voice=None, palette={},
        confidence="high", provenance={"pages_scanned": 1},
    )
    folder = BrandFolder(brand_name="Acme", root=tmp_path, kit_pdf=kit_pdf)

    patch = profile_to_patch(profile, folder, now_iso="2026-07-09T00:00:00Z")
    assert patch["enrichment"]["source_ladder"] == {
        "kit_pdf": True, "svg": True, "font_files": True, "pixel": False,
    }


def test_source_ladder_pdf_font_does_not_set_font_files_flag(tmp_path):
    """A font that came from the kit PDF (basefont raw_name, no .ttf/.otf)
    must not light the font_files rung; and a non-svg/pixel color context with
    NO kit pdf present must not light kit_pdf."""
    from app.services.brand_enrichment import profile_to_patch
    from app.services.brand_folder_scanner import BrandFolder
    from app.services.brand_kit_extractor import BrandKitProfile, ColorHit, FontHit

    profile = BrandKitProfile(
        brand_name="Acme",
        colors=[ColorHit(hex="#1A2B3C", page=1, context="Primary  #1A2B3C")],
        fonts=[FontHit(family="BeVietnamPro", style="Bold",
                       raw_name="ABCDEF+BeVietnamPro-Bold", embedded=True)],
        primary_colors=["#1A2B3C"], secondary_colors=[], accent_colors=[],
        font_family="Be Vietnam Pro", tone_of_voice=None, palette={},
        confidence="high", provenance={"pages_scanned": 1},
    )
    folder = BrandFolder(brand_name="Acme", root=tmp_path, kit_pdf=None)

    patch = profile_to_patch(profile, folder, now_iso="2026-07-09T00:00:00Z")
    assert patch["enrichment"]["source_ladder"] == {
        "kit_pdf": False, "svg": False, "font_files": False, "pixel": False,
    }


def test_source_ladder_pixel_false_when_svg_shadows_all_pixel_colors(tmp_path):
    """Integration through build_profile: the PNG's only color is the same hex
    as the SVG's, so the higher-priority svg rung dedupes every pixel hit out
    of the merged profile -> pixel must read False despite images existing."""
    from PIL import Image

    from app.services import brand_enrichment

    brand = tmp_path / "root" / "SvgCo"
    (brand / "Brand Kit" / "SVGs").mkdir(parents=True)
    (brand / "Brand Kit" / "SVGs" / "mark.svg").write_text(
        '<svg><path fill="#0892D0"/></svg>', encoding="utf-8")
    (brand / "Social").mkdir(parents=True)
    Image.new("RGB", (50, 50), (0x08, 0x92, 0xD0)).save(brand / "Social" / "post.png")

    reports = brand_enrichment.enrich_root(tmp_path / "root", dry_run=True,
                                            now_iso="2026-07-09T00:00:00Z")
    ladder = reports[0]["patch"]["enrichment"]["source_ladder"]
    assert ladder == {"kit_pdf": False, "svg": True, "font_files": False, "pixel": False}


def test_enrich_root_dry_run_performs_zero_firestore_calls(monkeypatch, tmp_path):
    """Dry-run must perform ZERO Firestore calls — including reads: the
    find_brand_by_name lookup is deferred to live runs, so dry-run entries
    carry matched_existing=None ("not checked")."""
    from app.services import brand_enrichment

    def boom(*args, **kwargs):
        raise AssertionError("must not be called during dry-run")
    monkeypatch.setattr(firestore_repo, "find_brand_by_name", boom)
    monkeypatch.setattr(firestore_repo, "update_brand_metadata", boom)
    monkeypatch.setattr(firestore_repo, "upsert_brand", boom)
    monkeypatch.setattr(firestore_repo, "_db", boom)

    reports = brand_enrichment.enrich_root(_brand_tree(tmp_path), dry_run=True,
                                            now_iso="2026-07-09T00:00:00Z")
    assert reports[0]["wrote"] is False
    assert reports[0]["matched_existing"] is None


# --------------------------------------------------------------------------- #
# Task 8 — CLI + font/logo GCS upload + static backfill
# --------------------------------------------------------------------------- #

def test_enrich_live_uploads_fonts_and_records_uris(monkeypatch, tmp_path):
    from app.services import brand_enrichment
    root = _brand_tree(tmp_path)
    fonts_dir = root / "Acme Health" / "Fonts"
    fonts_dir.mkdir()
    (fonts_dir / "Inter-Bold.ttf").write_bytes(b"font")

    db = _FakeDb()
    monkeypatch.setattr(firestore_repo, "_db", lambda: db)
    monkeypatch.setattr(firestore_repo, "find_brand_by_name", lambda n: None)
    monkeypatch.setattr(firestore_repo, "upsert_brand",
                         lambda n, m: {"id": "new1", "brand_name": n})
    uploaded = []
    monkeypatch.setattr(brand_enrichment, "_upload_file",
                         lambda local, dest: uploaded.append(dest) or f"gs://bucket/{dest}")

    reports = brand_enrichment.enrich_root(root, dry_run=False,
                                            now_iso="2026-07-09T00:00:00Z")
    assert "brands/new1/fonts/Inter-Bold.ttf" in uploaded
    assert reports[0]["patch"]["enrichment"]["font_files"] == \
        ["gs://bucket/brands/new1/fonts/Inter-Bold.ttf"]


def test_enrich_live_uploads_logos_and_records_uris(monkeypatch, tmp_path):
    from app.services import brand_enrichment
    root = _brand_tree(tmp_path)
    logos_dir = root / "Acme Health" / "Logos"
    logos_dir.mkdir()
    (logos_dir / "acme-logo.png").write_bytes(b"logo")

    db = _FakeDb()
    monkeypatch.setattr(firestore_repo, "_db", lambda: db)
    monkeypatch.setattr(firestore_repo, "find_brand_by_name", lambda n: None)
    monkeypatch.setattr(firestore_repo, "upsert_brand",
                         lambda n, m: {"id": "new1", "brand_name": n})
    uploaded = []
    monkeypatch.setattr(brand_enrichment, "_upload_file",
                         lambda local, dest: uploaded.append(dest) or f"gs://bucket/{dest}")

    reports = brand_enrichment.enrich_root(root, dry_run=False,
                                            now_iso="2026-07-09T00:00:00Z")
    assert "brands/new1/logos/acme-logo.png" in uploaded
    assert reports[0]["patch"]["enrichment"]["logo_files"] == \
        ["gs://bucket/brands/new1/logos/acme-logo.png"]


def test_enrich_root_dry_run_never_uploads(monkeypatch, tmp_path):
    from app.services import brand_enrichment
    root = _brand_tree(tmp_path)
    fonts_dir = root / "Acme Health" / "Fonts"
    fonts_dir.mkdir()
    (fonts_dir / "Inter-Bold.ttf").write_bytes(b"font")

    def boom(*args, **kwargs):
        raise AssertionError("must not upload during dry-run")
    monkeypatch.setattr(brand_enrichment, "_upload_file", boom)

    reports = brand_enrichment.enrich_root(root, dry_run=True,
                                            now_iso="2026-07-09T00:00:00Z")
    assert reports[0]["wrote"] is False


def test_upload_file_returns_none_when_bucket_not_configured(monkeypatch, tmp_path):
    from app.services import brand_enrichment, storage
    monkeypatch.setattr(storage, "is_configured", lambda: False)
    local = tmp_path / "a.ttf"
    local.write_bytes(b"x")
    assert brand_enrichment._upload_file(local, "brands/x/fonts/a.ttf") is None


def test_upload_file_delegates_to_public_storage_helper(monkeypatch, tmp_path):
    """Real _upload_file exercised one seam down: storage.upload_brand_asset
    receives the parsed brand_id/kind/filename, the file bytes and the
    extension-mapped content type; the returned gs:// URI flows back."""
    from app.services import brand_enrichment, storage

    monkeypatch.setattr(storage, "is_configured", lambda: True)
    calls = []

    def fake_upload(brand_id, kind, filename, data, content_type=None):
        calls.append((brand_id, kind, filename, data, content_type))
        return f"gs://bucket/brands/{brand_id}/{kind}/{filename}"
    monkeypatch.setattr(storage, "upload_brand_asset", fake_upload)

    cases = [("fonts", "Inter-Bold.ttf", "font/ttf"),
             ("fonts", "Magistral_Medium.otf", "font/otf"),
             ("logos", "acme-logo.png", "image/png")]
    for kind, name, expected_ctype in cases:
        local = tmp_path / name
        local.write_bytes(b"data-" + name.encode())
        uri = brand_enrichment._upload_file(local, f"brands/b1/{kind}/{name}")
        assert uri == f"gs://bucket/brands/b1/{kind}/{name}"

    assert calls == [
        ("b1", "fonts", "Inter-Bold.ttf", b"data-Inter-Bold.ttf", "font/ttf"),
        ("b1", "fonts", "Magistral_Medium.otf", b"data-Magistral_Medium.otf", "font/otf"),
        ("b1", "logos", "acme-logo.png", b"data-acme-logo.png", "image/png"),
    ]


def test_enrich_live_upload_failure_contained_per_file(monkeypatch, tmp_path):
    """One flaky upload gets a note and never aborts the batch: the other
    files still upload, and the brand's Firestore write still happens."""
    from app.services import brand_enrichment, storage

    root = _brand_tree(tmp_path)
    fonts_dir = root / "Acme Health" / "Fonts"
    fonts_dir.mkdir()
    (fonts_dir / "Bad-Font.ttf").write_bytes(b"bad")
    (fonts_dir / "Good-Font.ttf").write_bytes(b"good")

    monkeypatch.setattr(firestore_repo, "find_brand_by_name", lambda n: None)
    monkeypatch.setattr(firestore_repo, "upsert_brand",
                         lambda n, m: {"id": "new1", "brand_name": n})
    updated = {}
    monkeypatch.setattr(firestore_repo, "update_brand_metadata",
                         lambda i, p: updated.update({"id": i, "patch": p}) or {})
    monkeypatch.setattr(storage, "is_configured", lambda: True)

    def flaky_upload(brand_id, kind, filename, data, content_type=None):
        if filename == "Bad-Font.ttf":
            raise RuntimeError("boom-bucket")
        return f"gs://bucket/brands/{brand_id}/{kind}/{filename}"
    monkeypatch.setattr(storage, "upload_brand_asset", flaky_upload)

    reports = brand_enrichment.enrich_root(root, dry_run=False,
                                            now_iso="2026-07-09T00:00:00Z")
    r = reports[0]
    assert r["wrote"] is True                              # run continued
    assert r["patch"]["enrichment"]["font_files"] == \
        ["gs://bucket/brands/new1/fonts/Good-Font.ttf"]    # other file uploaded
    assert any(n.startswith("upload failed: Bad-Font.ttf:") and "boom-bucket" in n
               for n in r["notes"])
    assert updated["id"] == "new1"                         # Firestore write happened


def test_backfill_static_medvirtual_mapping(monkeypatch):
    from app.services import brand_enrichment
    from graphics_designer_agent.templated_brands import _MEDVIRTUAL

    captured = {}

    def fake_update(brand_id, patch):
        captured.update({"brand_id": brand_id, "patch": patch})
        return {}
    monkeypatch.setattr(firestore_repo, "update_brand_metadata", fake_update)

    report = brand_enrichment.backfill_static("medvirtual", dry_run=False,
                                               now_iso="2026-07-09T00:00:00Z")

    palette = _MEDVIRTUAL["palette"]
    assert captured["brand_id"] == _MEDVIRTUAL["firestore_brand_id"]
    patch = captured["patch"]
    assert patch["primary_colors"] == [palette["mid"], palette["deep"]]
    assert patch["secondary_colors"] == [palette["light"]]
    assert patch["accent_colors"] == [palette["accent"]]
    assert patch["fonts"] == [v["name"] for v in _MEDVIRTUAL["font_variants"]]
    assert patch["brand_kit_source"] == "static-spec:templated_brands/medvirtual"
    assert patch["enrichment"] == {
        "confidence": "high", "extracted_at": "2026-07-09T00:00:00Z",
        "palette": dict(palette), "source": "static_spec",
    }
    assert report["wrote"] is True
    assert report["brand_name"] == "MedVirtual"
    # pinned: the spec names a concrete existing Firestore doc, and its fonts
    # are exact spec values (never a derived fallback)
    assert report["matched_existing"] is True
    assert report["font_fallback"] is False


def test_backfill_static_dry_run_never_writes(monkeypatch):
    from app.services import brand_enrichment

    def boom(*args, **kwargs):
        raise AssertionError("must not be called during dry-run")
    monkeypatch.setattr(firestore_repo, "update_brand_metadata", boom)

    report = brand_enrichment.backfill_static("medvirtual", dry_run=True,
                                                now_iso="2026-07-09T00:00:00Z")
    assert report["wrote"] is False
    assert report["patch"]["primary_colors"]


def test_backfill_static_unknown_pack_id_raises():
    from app.services import brand_enrichment
    with pytest.raises(ValueError):
        brand_enrichment.backfill_static("nonexistent-pack", dry_run=True,
                                          now_iso="2026-07-09T00:00:00Z")


# --------------------------------------------------------------------------- #
# CLI (app/enrich_brands.py) — pure helper + argparse wiring, offline
# --------------------------------------------------------------------------- #

def test_cli_sources_label_lists_active_ladder_rungs():
    from app.enrich_brands import _sources_label
    report = {"patch": {"enrichment": {"source_ladder": {
        "kit_pdf": False, "svg": True, "font_files": True, "pixel": True,
    }}}}
    assert _sources_label(report) == "svg+fonts+pixel"


def test_cli_sources_label_handles_skipped_brand_without_patch():
    from app.enrich_brands import _sources_label
    assert _sources_label({"patch": None}) == "-"


def test_cli_sources_label_handles_static_backfill():
    from app.enrich_brands import _sources_label
    report = {"patch": {"enrichment": {"source": "static_spec"}}}
    assert _sources_label(report) == "static_spec"


def test_cli_main_dry_run_writes_report_and_touches_no_firestore_write(
        monkeypatch, tmp_path, capsys):
    from app import enrich_brands
    from app.services import brand_enrichment

    db = _FakeDb()
    monkeypatch.setattr(firestore_repo, "_db", lambda: db)

    def boom(*args, **kwargs):
        raise AssertionError("must not write during a dry-run CLI invocation")
    monkeypatch.setattr(firestore_repo, "update_brand_metadata", boom)
    monkeypatch.setattr(firestore_repo, "upsert_brand", boom)
    monkeypatch.setattr(brand_enrichment, "_upload_file", boom)

    root = _brand_tree(tmp_path)
    report_path = tmp_path / "out.json"
    monkeypatch.setattr(
        "sys.argv",
        ["enrich_brands", "--root", str(root), "--report", str(report_path)],
    )
    enrich_brands.main()

    assert report_path.exists()
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data[0]["wrote"] is False
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "sources=" in out


def test_cli_main_backfill_static_dry_run(monkeypatch, tmp_path, capsys):
    from app import enrich_brands

    def boom(*args, **kwargs):
        raise AssertionError("must not write during a dry-run CLI invocation")
    monkeypatch.setattr(firestore_repo, "update_brand_metadata", boom)

    report_path = tmp_path / "backfill.json"
    monkeypatch.setattr(
        "sys.argv",
        ["enrich_brands", "--backfill-static", "medvirtual", "--report", str(report_path)],
    )
    enrich_brands.main()

    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data[0]["wrote"] is False
    assert data[0]["brand_name"] == "MedVirtual"


def test_cli_main_writes_report_even_when_run_raises(monkeypatch, tmp_path):
    """A mid-run crash must still leave the report JSON on disk (whatever was
    gathered so far), then re-raise."""
    from app import enrich_brands

    def boom_root(*args, **kwargs):
        raise RuntimeError("scan exploded")
    monkeypatch.setattr(enrich_brands, "enrich_root", boom_root)

    report_path = tmp_path / "out.json"
    monkeypatch.setattr(
        "sys.argv",
        ["enrich_brands", "--root", str(tmp_path), "--report", str(report_path)],
    )
    with pytest.raises(RuntimeError, match="scan exploded"):
        enrich_brands.main()

    assert json.loads(report_path.read_text(encoding="utf-8")) == []


def test_cli_brand_flag_rejected_in_backfill_mode(monkeypatch, tmp_path, capsys):
    """--brand only filters --root scans; combining it with --backfill-static
    is a usage error, not silently ignored."""
    from app import enrich_brands

    monkeypatch.setattr(
        "sys.argv",
        ["enrich_brands", "--backfill-static", "medvirtual", "--brand", "MedVirtual"],
    )
    with pytest.raises(SystemExit):
        enrich_brands.main()
    assert "--brand only applies to --root mode" in capsys.readouterr().err
