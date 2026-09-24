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
    monkeypatch.setattr(enrich_brands, "enrich_iter", boom_root)

    report_path = tmp_path / "out.json"
    monkeypatch.setattr(
        "sys.argv",
        ["enrich_brands", "--root", str(tmp_path), "--report", str(report_path)],
    )
    with pytest.raises(RuntimeError, match="scan exploded"):
        enrich_brands.main()

    assert json.loads(report_path.read_text(encoding="utf-8")) == []


# --------------------------------------------------------------------------- #
# Task 9 — GD spec builder integration (patch["gd_spec"])
# --------------------------------------------------------------------------- #

def _thin_brand_tree(tmp_path: Path) -> Path:
    """Build <root>/Thin Co/Brand Kit/kit.pdf with a single hex color — fewer
    than 3, so build_gd_spec must return None (not generatable)."""
    from reportlab.pdfgen import canvas

    kit_dir = tmp_path / "root" / "Thin Co" / "Brand Kit"
    kit_dir.mkdir(parents=True)
    pdf = kit_dir / "Thin Brand Guide.pdf"
    c = canvas.Canvas(str(pdf))
    c.drawString(72, 700, "Primary #112233")
    c.save()
    return tmp_path / "root"


def test_enrich_root_dry_run_patch_contains_gd_spec_with_null_firestore_id(
        monkeypatch, tmp_path):
    from app.services import brand_enrichment
    db = _FakeDb()
    monkeypatch.setattr(firestore_repo, "_db", lambda: db)

    reports = brand_enrichment.enrich_root(_brand_tree(tmp_path), dry_run=True,
                                            now_iso="2026-07-09T00:00:00Z")
    spec = reports[0]["patch"]["gd_spec"]
    assert spec is not None
    assert spec["id"] == "acme-health"
    assert spec["firestore_brand_id"] is None       # brand_id unknown during dry-run
    assert db.brands == {}                          # still zero Firestore calls


def test_enrich_root_live_writes_gd_spec_with_allocated_brand_id(monkeypatch, tmp_path):
    from app.services import brand_enrichment
    db = _FakeDb()
    monkeypatch.setattr(firestore_repo, "_db", lambda: db)
    monkeypatch.setattr(firestore_repo, "find_brand_by_name", lambda name: None)
    monkeypatch.setattr(firestore_repo, "upsert_brand",
                         lambda n, m: {"id": "new1", "brand_name": n})
    updated = {}
    monkeypatch.setattr(
        firestore_repo, "update_brand_metadata",
        lambda i, p: updated.update({"brand_id": i, "patch": p}) or {})

    reports = brand_enrichment.enrich_root(_brand_tree(tmp_path), dry_run=False,
                                            now_iso="2026-07-09T00:00:00Z")
    assert reports[0]["wrote"] is True
    # the write that actually reached Firestore carries the allocated id
    assert updated["patch"]["gd_spec"]["firestore_brand_id"] == "new1"
    # and the returned report reflects the same patched spec (same dict)
    assert reports[0]["patch"]["gd_spec"]["firestore_brand_id"] == "new1"


def test_enrich_root_patch_omits_gd_spec_when_fewer_than_three_colors(monkeypatch, tmp_path):
    from app.services import brand_enrichment

    reports = brand_enrichment.enrich_root(_thin_brand_tree(tmp_path), dry_run=True,
                                            now_iso="2026-07-09T00:00:00Z")
    assert "gd_spec" not in reports[0]["patch"]


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


# --------------------------------------------------------------------------- #
# Final-review Finding 1 — --brand must scope the RUN, not just the report:
# only_brand filters scan_root's folders BEFORE any enrichment runs, so an
# unrequested brand is never profiled, matched, or written — live or dry.
# --------------------------------------------------------------------------- #

def _two_brand_tree(tmp_path: Path) -> Path:
    """<root>/Acme Health/... (Task 7 fixture) plus a second, unrelated
    <root>/Beta Co/... brand folder — both independently extractable."""
    root = _brand_tree(tmp_path)
    beta_dir = root / "Beta Co" / "Brand Kit"
    beta_dir.mkdir(parents=True)
    _make_kit_pdf(beta_dir / "Beta Brand Guidelines.pdf")
    return root


def test_enrich_root_only_brand_runs_exactly_matching_folder(monkeypatch, tmp_path):
    """(a) only_brand scopes the run to exactly the matching folder —
    case-insensitively — never the others."""
    from app.services import brand_enrichment

    root = _two_brand_tree(tmp_path)
    reports = brand_enrichment.enrich_root(root, dry_run=True, only_brand="acme health",
                                            now_iso="2026-07-09T00:00:00Z")
    assert [r["brand_name"] for r in reports] == ["Acme Health"]


def test_enrich_root_only_brand_live_never_writes_other_brands(monkeypatch, tmp_path):
    """(b) Booby-trap: in live (--write) mode, only_brand must scope the RUN
    itself — Firestore calls must never fire for the non-matching brand,
    not merely be excluded from the printed report."""
    from app.services import brand_enrichment

    root = _two_brand_tree(tmp_path)
    monkeypatch.setattr(firestore_repo, "find_brand_by_name", lambda n: None)

    def guarded_upsert(name, meta):
        if name != "Acme Health":
            raise AssertionError(f"must not enrich unrequested brand {name!r}")
        return {"id": "new1", "brand_name": name}
    monkeypatch.setattr(firestore_repo, "upsert_brand", guarded_upsert)

    updated = {}

    def guarded_update(brand_id, patch):
        if brand_id != "new1":
            raise AssertionError(f"must not write unrequested brand id {brand_id!r}")
        updated.update({"brand_id": brand_id, "patch": patch})
        return {}
    monkeypatch.setattr(firestore_repo, "update_brand_metadata", guarded_update)

    reports = brand_enrichment.enrich_root(root, dry_run=False, only_brand="Acme Health",
                                            now_iso="2026-07-09T00:00:00Z")
    assert [r["brand_name"] for r in reports] == ["Acme Health"]
    assert reports[0]["wrote"] is True
    assert updated["brand_id"] == "new1"


def test_cli_brand_no_match_exits_2_with_no_enrichment(monkeypatch, tmp_path, capsys):
    """(c) A --brand with no matching folder must exit 2, list the available
    folder names, and never call into enrichment at all — not even a dry run
    of the other brands (no report of other brands, no writes)."""
    from app import enrich_brands
    from app.services import brand_enrichment

    root = _two_brand_tree(tmp_path)

    def boom(*args, **kwargs):
        raise AssertionError("must not enrich when --brand has no matching folder")
    monkeypatch.setattr(brand_enrichment, "_enrich_one", boom)

    report_path = tmp_path / "out.json"
    monkeypatch.setattr(
        "sys.argv",
        ["enrich_brands", "--root", str(root), "--brand", "Nonexistent Co",
         "--report", str(report_path)],
    )
    with pytest.raises(SystemExit) as exc_info:
        enrich_brands.main()
    assert exc_info.value.code == 2

    out = capsys.readouterr().out
    assert "Acme Health" in out and "Beta Co" in out       # available folders listed
    assert json.loads(report_path.read_text(encoding="utf-8")) == []  # no report entries


# --------------------------------------------------------------------------- #
# Final-review Finding 5 — a mid-batch crash must not empty the report: the
# CLI now iterates the streaming `enrich_iter` generator, accumulating into
# the list it writes in `finally`, so brands already yielded before a crash
# stay on disk.
# --------------------------------------------------------------------------- #

def test_cli_main_partial_crash_preserves_earlier_brand_entries(monkeypatch, tmp_path):
    from app import enrich_brands
    from app.services import brand_enrichment

    root = _two_brand_tree(tmp_path)  # scan_root sorts: "Acme Health" then "Beta Co"
    real_enrich_one = brand_enrichment._enrich_one

    def flaky(folder, **kwargs):
        if folder.brand_name == "Beta Co":
            raise RuntimeError("beta exploded")
        return real_enrich_one(folder, **kwargs)
    monkeypatch.setattr(brand_enrichment, "_enrich_one", flaky)

    report_path = tmp_path / "out.json"
    monkeypatch.setattr(
        "sys.argv",
        ["enrich_brands", "--root", str(root), "--report", str(report_path)],
    )
    with pytest.raises(RuntimeError, match="beta exploded"):
        enrich_brands.main()

    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert [r["brand_name"] for r in data] == ["Acme Health"]  # first brand preserved


def test_enrich_iter_reraises_after_yielding_prior_entries(monkeypatch, tmp_path):
    """Same guarantee one level down: `enrich_iter` itself yields the first
    brand's entry before the second brand's crash propagates — `enrich_root`
    (a thin `list(enrich_iter(...))` wrapper) therefore cannot silently
    return `[]` on a mid-batch crash either."""
    from app.services import brand_enrichment

    root = _two_brand_tree(tmp_path)
    it = brand_enrichment.enrich_iter(root, dry_run=True, now_iso="2026-07-09T00:00:00Z")
    first = next(it)
    assert first["brand_name"] == "Acme Health"

    def flaky(folder, **kwargs):
        raise RuntimeError("beta exploded")
    monkeypatch.setattr(brand_enrichment, "_enrich_one", flaky)

    with pytest.raises(RuntimeError, match="beta exploded"):
        next(it)


# --------------------------------------------------------------------------- #
# Self-serve GD brands (2026-09-25) — firestore_repo brand store + storage paths
#
# A multi-collection in-memory Firestore with a buffered transaction: writes
# land only when the callback returns, so a raise mid-transaction writes
# nothing — the property the real transaction gives the code.
# --------------------------------------------------------------------------- #

from google.cloud import firestore as _gfs  # noqa: E402

from app.services import storage  # noqa: E402


def _apply_fields(cur: dict, data: dict) -> None:
    for k, v in data.items():
        if isinstance(v, _gfs.Increment):
            cur[k] = (cur.get(k) or 0) + v.value
        elif isinstance(v, dict) and isinstance(cur.get(k), dict):
            _apply_fields(cur[k], v)
        else:
            cur[k] = v


class _MemSnap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return None if self._data is None else json.loads(json.dumps(self._data))


class _MemDoc:
    def __init__(self, db, col, doc_id):
        self._db, self._col, self.id = db, col, doc_id

    def _store(self):
        return self._db.data.setdefault(self._col, {})

    def get(self, transaction=None):
        return _MemSnap(self.id, self._db.data.get(self._col, {}).get(self.id))

    def set(self, data, merge=False):
        if merge:
            _apply_fields(self._store().setdefault(self.id, {}), data)
        else:
            fresh: dict = {}
            _apply_fields(fresh, data)
            self._store()[self.id] = fresh

    def update(self, fields):
        if self.id not in self._store():
            raise KeyError(f"update on missing doc {self._col}/{self.id}")
        self._store()[self.id].update(fields)


class _MemQuery:
    def __init__(self, db, col, filters=(), order=None, limit_n=None):
        self._db, self._col = db, col
        self._filters, self._order, self._limit = tuple(filters), order, limit_n

    def where(self, filter):
        # ``FieldFilter(f, "==", None)`` becomes the unary IS_NULL operator
        if filter.op_string == _gfs.FieldFilter("x", "==", None).op_string:
            value = None
        else:
            assert filter.op_string == "==", filter.op_string
            value = filter.value
        return _MemQuery(self._db, self._col,
                         self._filters + ((filter.field_path, value),),
                         self._order, self._limit)

    def order_by(self, field, direction=None):
        return _MemQuery(self._db, self._col, self._filters,
                         (field, direction == _gfs.Query.DESCENDING), self._limit)

    def limit(self, n):
        return _MemQuery(self._db, self._col, self._filters, self._order, n)

    def stream(self, transaction=None):
        if self._db.composite_missing and self._order and len(self._filters) > 1:
            raise RuntimeError("400 The query requires an index")
        self._db.streams.append((self._col, self._filters, self._order))
        rows = [(i, d) for i, d in self._db.data.get(self._col, {}).items()
                if all(d.get(f) == v for f, v in self._filters)]
        if self._order:
            field, desc = self._order
            rows.sort(key=lambda r: r[1].get(field) or "", reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return [_MemSnap(i, d) for i, d in rows]


class _MemCol(_MemQuery):
    def document(self, doc_id):
        return _MemDoc(self._db, self._col, doc_id)


class _MemDb:
    def __init__(self):
        self.data: dict[str, dict[str, dict]] = {}
        self.streams: list = []
        self.composite_missing = False

    def collection(self, name):
        return _MemCol(self, name)


class _MemTxn:
    def __init__(self):
        self.ops: list = []

    def set(self, ref, data, merge=False):
        self.ops.append(lambda: ref.set(data, merge=merge))

    def update(self, ref, fields):
        self.ops.append(lambda: ref.update(fields))


class _FakeBucket:
    def __init__(self, uploads):
        self._uploads = uploads

    def blob(self, path):
        uploads = self._uploads

        class _Blob:
            def upload_from_string(self, data, content_type=None, timeout=None):
                uploads[path] = (data, content_type)
        return _Blob()


@pytest.fixture()
def brand_store(monkeypatch):
    """In-memory Firestore + GCS for the self-serve brand store."""
    from app.config import settings

    db = _MemDb()
    uploads: dict = {}

    def _transact(fn):
        txn = _MemTxn()
        result = fn(txn)
        for op in txn.ops:
            op()
        return result

    monkeypatch.setattr(firestore_repo, "_db", lambda: db)
    monkeypatch.setattr(firestore_repo, "_transact", _transact)
    monkeypatch.setattr(firestore_repo, "_brands_cache", None)
    monkeypatch.setattr(firestore_repo, "_references_index_missing_at", None)
    monkeypatch.setattr(storage, "is_configured", lambda: True)
    monkeypatch.setattr(settings, "gcs_bucket_name", "test-bucket", raising=False)
    monkeypatch.setattr(storage, "_storage",
                        lambda: type("C", (), {"bucket": lambda self, n: _FakeBucket(uploads)})())
    db.uploads = uploads
    return db


_KIT = {"primary_colors": ["#1a2b3c"], "secondary_colors": ["#24B9CE"],
        "accent_colors": [], "fonts": ["Inter Bold"], "tone_of_voice": "calm"}


def _version(db) -> int:
    return db.data.get("meta", {}).get("brands", {}).get("version", 0)


def test_create_brand_writes_slug_id_and_the_pipeline_metadata_shape(brand_store):
    doc = firestore_repo.create_brand(
        "Acme Co", _KIT | {"gd_spec": {"id": "wrong", "palette": {}}},
        created_by="Vishal@Example.com")

    stored = brand_store.data["brands"]["acme-co"]
    assert doc["id"] == "acme-co" and stored["slug"] == "acme-co"
    assert stored["name"] == stored["brand_name"] == "Acme Co"
    assert stored["brand_name_lower"] == "acme co"
    assert stored["source"] == "user" and stored["archived_at"] is None
    assert stored["created_by"] == "vishal@example.com" and stored["logo_uri"] is None
    meta = stored["brand_metadata"]
    assert meta["primary_colors"] == ["#1A2B3C"] and meta["fonts"] == ["Inter Bold"]
    assert meta["enrichment"] == {"palette": {}, "logo_files": [], "font_files": [],
                                  "guideline_files": []}
    # the pack id, the Firestore id and the reference brand_id are one string
    assert meta["gd_spec"]["id"] == meta["gd_spec"]["firestore_brand_id"] == "acme-co"
    assert _version(brand_store) == 1


@pytest.mark.parametrize("name", ["Legal Soft", "LegalSoft", "MedVirtual",
                                  "Remote Attorneys", "remote_attorneys"])
def test_create_brand_refuses_a_built_in_pack(brand_store, name):
    with pytest.raises(firestore_repo.BrandExists):
        firestore_repo.create_brand(name, {}, created_by="a@b.com")
    assert "brands" not in brand_store.data and _version(brand_store) == 0


def test_create_brand_refuses_an_existing_slug_or_a_legacy_doc_with_the_same_name(brand_store):
    firestore_repo.create_brand("Acme Co", {}, created_by="a@b.com")
    with pytest.raises(firestore_repo.BrandExists):
        firestore_repo.create_brand("acme  co!", {}, created_by="c@d.com")

    brand_store.data["brands"]["9f00uuid"] = {"brand_name": "Globex",
                                              "brand_name_lower": "globex"}
    with pytest.raises(firestore_repo.BrandExists):
        firestore_repo.create_brand("GLOBEX", {}, created_by="c@d.com")
    assert set(brand_store.data["brands"]) == {"acme-co", "9f00uuid"}
    assert _version(brand_store) == 1


@pytest.mark.parametrize("name,kit", [
    ("!!!", {}),
    ("Acme", {"primary_colors": ["blue"]}),
    ("Acme", {"logo": "x.png"}),
    ("Acme", {"fonts": "Inter"}),
])
def test_create_brand_refuses_bad_input_before_writing(brand_store, name, kit):
    with pytest.raises(ValueError):
        firestore_repo.create_brand(name, kit, created_by="a@b.com")
    assert "brands" not in brand_store.data


def test_update_brand_merges_and_keeps_the_slug(brand_store):
    firestore_repo.create_brand("Acme Co", _KIT, created_by="a@b.com")
    firestore_repo.update_brand("acme-co", {
        "name": "Acme Corporation", "accent_colors": ["#fff"],
        "enrichment": {"palette": {"primary": "#1A2B3C"}},
        "gd_spec": {"id": "evil", "palette": {"primary": "#1A2B3C"}},
    })
    stored = brand_store.data["brands"]["acme-co"]
    meta = stored["brand_metadata"]
    assert stored["name"] == stored["brand_name"] == "Acme Corporation"
    assert meta["primary_colors"] == ["#1A2B3C"]            # untouched key kept
    assert meta["accent_colors"] == ["#FFF"]                 # patched key replaced
    assert meta["enrichment"]["palette"] == {"primary": "#1A2B3C"}
    assert meta["enrichment"]["logo_files"] == []            # enrichment merged, not replaced
    assert meta["gd_spec"]["id"] == "acme-co"                # pack id pinned to the slug
    assert _version(brand_store) == 2


def test_update_and_archive_refuse_archived_legacy_and_unknown_brands(brand_store):
    firestore_repo.create_brand("Acme Co", {}, created_by="a@b.com")
    brand_store.data["brands"]["9f00uuid"] = {"brand_name": "Legacy"}
    with pytest.raises(firestore_repo.BrandNotEditable):
        firestore_repo.update_brand("9f00uuid", {"fonts": ["X"]})
    with pytest.raises(firestore_repo.BrandNotFound):
        firestore_repo.update_brand("nope", {"fonts": ["X"]})

    firestore_repo.archive_brand("acme-co")
    with pytest.raises(firestore_repo.BrandNotEditable):
        firestore_repo.update_brand("acme-co", {"fonts": ["X"]})
    with pytest.raises(firestore_repo.BrandNotEditable):
        firestore_repo.archive_brand("acme-co")
    assert "acme-co" in brand_store.data["brands"]           # soft: never deleted


def test_list_brands_hides_archived_unless_asked(brand_store):
    firestore_repo.create_brand("Acme Co", {}, created_by="a@b.com")
    firestore_repo.create_brand("Beta", {}, created_by="a@b.com")
    brand_store.data["brands"]["9f00uuid"] = {"brand_name": "Legacy"}  # no archived_at
    firestore_repo.archive_brand("beta")

    assert {b["id"] for b in firestore_repo.list_brands()} == {"acme-co", "9f00uuid"}
    assert {b["id"] for b in firestore_repo.list_brands(include_archived=True)} == {
        "acme-co", "beta", "9f00uuid"}


def test_brand_asset_paths_are_content_hashed_and_typed():
    data = b"\x89PNG logo bytes"
    h = storage.content_hash(data)
    assert len(h) == 16
    assert storage.brand_asset_object_path("acme-co", "logo", data, ".PNG") == \
        f"brands/acme-co/logos/{h}.png"
    assert storage.brand_asset_object_path("acme-co", "font", data, "ttf") == \
        f"brands/acme-co/fonts/{h}.ttf"
    assert storage.reference_object_path("acme-co", data, "jpg") == \
        f"reference_library/acme-co/{h}.jpg"
    with pytest.raises(ValueError):
        storage.brand_asset_object_path("acme-co", "logo", data, "exe")
    with pytest.raises(ValueError):
        storage.brand_asset_object_path("acme-co", "video", data, "png")


def test_put_brand_asset_refuses_when_storage_is_unconfigured():
    # conftest's GCS guard reports unconfigured: nothing may reach a bucket
    with pytest.raises(RuntimeError, match="not configured"):
        storage.put_brand_asset("acme-co", "logo", b"x", "png")


def test_add_brand_asset_records_uri_and_logo_uri_once(brand_store):
    firestore_repo.create_brand("Acme Co", {}, created_by="a@b.com")
    logo = firestore_repo.add_brand_asset("acme-co", "logo", b"logo-bytes", "png")
    again = firestore_repo.add_brand_asset("acme-co", "logo", b"logo-bytes", "png")
    firestore_repo.add_brand_asset("acme-co", "font", b"font-bytes", "ttf")
    firestore_repo.add_brand_asset("acme-co", "guidelines", b"pdf-bytes", "pdf")

    h = storage.content_hash(b"logo-bytes")
    assert logo["uri"] == again["uri"] == f"gs://test-bucket/brands/acme-co/logos/{h}.png"
    assets = firestore_repo.list_brand_assets("acme-co")
    assert assets["logo_uri"] == logo["uri"]
    assert assets["logo_files"] == [logo["uri"]]             # same bytes = one entry
    assert len(assets["font_files"]) == 1 and len(assets["guideline_files"]) == 1
    assert brand_store.uploads[f"brands/acme-co/logos/{h}.png"][1] == "image/png"


def test_add_brand_asset_refuses_before_uploading_to_an_archived_brand(brand_store):
    firestore_repo.create_brand("Acme Co", {}, created_by="a@b.com")
    firestore_repo.archive_brand("acme-co")
    with pytest.raises(firestore_repo.BrandNotEditable):
        firestore_repo.add_brand_asset("acme-co", "logo", b"x", "png")
    assert brand_store.uploads == {}


def test_add_reference_writes_one_doc_per_reference_and_is_idempotent(brand_store):
    firestore_repo.create_brand("Acme Co", {}, created_by="a@b.com")
    ref = firestore_repo.add_reference(
        "acme-co", data=b"img-1", ext="png", kind="creative", uploaded_by="A@B.com",
        width=1080, height=1080, note="Spring promo")
    firestore_repo.add_reference(
        "acme-co", data=b"img-1", ext="png", kind="creative", uploaded_by="x@y.com")

    h = storage.content_hash(b"img-1")
    assert ref["ref_id"] == h and ref["id"] == f"acme-co__{h}"
    assert ref["object_path"] == f"reference_library/acme-co/{h}.png"
    assert ref["gs_uri"] == f"gs://test-bucket/reference_library/acme-co/{h}.png"
    assert ref["content_type"] == "image/png" and ref["deleted_at"] is None
    assert ref["uploaded_by"] == "a@b.com" and (ref["width"], ref["height"]) == (1080, 1080)
    assert list(brand_store.data["brand_references"]) == [f"acme-co__{h}"]
    assert brand_store.data["brand_reference_counts"]["acme-co"]["active"] == 1


def test_add_reference_enforces_the_cap_before_uploading(brand_store):
    firestore_repo.create_brand("Acme Co", {}, created_by="a@b.com")
    brand_store.data["brand_reference_counts"] = {"acme-co": {"active": 200}}
    with pytest.raises(firestore_repo.ReferenceCapReached):
        firestore_repo.add_reference("acme-co", data=b"one-more", ext="png",
                                     kind="reference", uploaded_by="a@b.com")
    assert brand_store.uploads == {} and "brand_references" not in brand_store.data


def test_soft_delete_frees_a_slot_hides_the_reference_and_readding_revives_it(brand_store):
    firestore_repo.create_brand("Acme Co", {}, created_by="a@b.com")
    ref = firestore_repo.add_reference("acme-co", data=b"img-1", ext="png",
                                       kind="creative", uploaded_by="a@b.com")
    firestore_repo.soft_delete_reference("acme-co", ref["ref_id"])
    firestore_repo.soft_delete_reference("acme-co", ref["ref_id"])   # no-op, no double decrement

    assert brand_store.data["brand_reference_counts"]["acme-co"]["active"] == 0
    assert firestore_repo.list_references("acme-co") == []
    assert brand_store.data["brand_references"][ref["id"]]["deleted_at"]  # kept, not deleted
    with pytest.raises(firestore_repo.BrandNotFound):
        firestore_repo.soft_delete_reference("other-brand", ref["ref_id"])

    firestore_repo.add_reference("acme-co", data=b"img-1", ext="png",
                                 kind="creative", uploaded_by="a@b.com")
    assert [r["id"] for r in firestore_repo.list_references("acme-co")] == [ref["id"]]
    assert brand_store.data["brand_reference_counts"]["acme-co"]["active"] == 1


def test_references_attach_to_built_in_packs_but_not_unknown_brands(brand_store):
    firestore_repo.add_reference("legalsoft", data=b"ls", ext="jpg",
                                 kind="reference", uploaded_by="a@b.com")
    assert len(firestore_repo.list_references("legalsoft")) == 1
    with pytest.raises(firestore_repo.BrandNotFound):
        firestore_repo.add_reference("ghost", data=b"g", ext="png",
                                     kind="reference", uploaded_by="a@b.com")


def test_list_references_is_newest_first_on_the_indexed_query(brand_store):
    brand_store.data["brand_references"] = {
        "acme-co__a": {"brand_id": "acme-co", "created_at": "2026-09-01", "deleted_at": None},
        "acme-co__b": {"brand_id": "acme-co", "created_at": "2026-09-03", "deleted_at": None},
        "acme-co__c": {"brand_id": "acme-co", "created_at": "2026-09-02", "deleted_at": "x"},
        "other__d": {"brand_id": "other", "created_at": "2026-09-04", "deleted_at": None},
    }
    assert [r["id"] for r in firestore_repo.list_references("acme-co")] == [
        "acme-co__b", "acme-co__a"]
    col, filters, order = brand_store.streams[-1]
    assert col == "brand_references"
    assert filters == (("brand_id", "acme-co"), ("deleted_at", None))
    assert order == ("created_at", True)


def test_list_references_falls_back_in_process_when_the_index_is_missing(brand_store):
    brand_store.composite_missing = True
    brand_store.data["brand_references"] = {
        "acme-co__a": {"brand_id": "acme-co", "created_at": "2026-09-01", "deleted_at": None},
        "acme-co__b": {"brand_id": "acme-co", "created_at": "2026-09-03", "deleted_at": None},
        "acme-co__c": {"brand_id": "acme-co", "created_at": "2026-09-05", "deleted_at": "x"},
    }
    assert [r["id"] for r in firestore_repo.list_references("acme-co")] == [
        "acme-co__b", "acme-co__a"]


def test_references_for_brand_merges_uploads_with_the_legacy_index(brand_store):
    firestore_repo.add_reference("remote_attorneys", data=b"ra", ext="png",
                                 kind="creative", uploaded_by="a@b.com",
                                 creative_type="social_post", note="Hiring push")
    legacy = [
        {"id": "l1", "brand_id": "remoteattorneys", "gs_uri": "gs://b/reference_library/x.png"},
        {"id": "l2", "brand_id": "legalsoft", "gs_uri": "gs://b/reference_library/y.png"},
    ]
    refs = firestore_repo.references_for_brand("remote_attorneys", legacy_records=legacy)

    assert [r.get("source") for r in refs] == ["upload", None]
    assert refs[1]["id"] == "l1"                           # separator-free match, untouched
    upload = refs[0]
    assert upload["creative_type"] == "social_post"
    assert upload["file_name"].endswith(".png") and upload["gs_uri"].startswith("gs://")
    assert "hiring" in upload["tags"]


def test_references_for_brand_reads_no_legacy_index_when_storage_is_off(brand_store, monkeypatch):
    monkeypatch.setattr(storage, "is_configured", lambda: False)
    assert firestore_repo.references_for_brand("acme-co") == []
