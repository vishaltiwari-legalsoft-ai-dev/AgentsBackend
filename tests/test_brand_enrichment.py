# backend/tests/test_brand_enrichment.py
from pathlib import Path

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


def test_profile_to_patch_source_ladder_flags(tmp_path):
    from app.services.brand_enrichment import profile_to_patch
    from app.services.brand_folder_scanner import BrandFolder
    from app.services.brand_kit_extractor import BrandKitProfile

    kit_pdf = tmp_path / "kit.pdf"
    kit_pdf.write_bytes(b"x")
    svg = tmp_path / "a.svg"
    svg.write_bytes(b"x")
    profile = BrandKitProfile(
        brand_name="Acme", colors=[], fonts=[],
        primary_colors=["#1A2B3C"], secondary_colors=[], accent_colors=[],
        font_family=None, tone_of_voice=None, palette={},
        confidence="high", provenance={"pages_scanned": 1},
    )
    folder = BrandFolder(brand_name="Acme", root=tmp_path, kit_pdf=kit_pdf,
                          svg_files=[svg], font_files=[], logo_candidates=[], asset_files=[])

    patch = profile_to_patch(profile, folder, now_iso="2026-07-09T00:00:00Z")
    assert patch["enrichment"]["source_ladder"] == {
        "kit_pdf": True, "svg": True, "font_files": False, "pixel": False,
    }


def test_enrich_root_dry_run_never_writes(monkeypatch, tmp_path):
    """Dry-run must never reach a Firestore write (find_brand_by_name reads
    are fine — they populate matched_existing for the report)."""
    from app.services import brand_enrichment
    db = _FakeDb()
    monkeypatch.setattr(firestore_repo, "_db", lambda: db)

    def boom(*args, **kwargs):
        raise AssertionError("must not be called during dry-run")
    monkeypatch.setattr(firestore_repo, "update_brand_metadata", boom)
    monkeypatch.setattr(firestore_repo, "upsert_brand", boom)

    reports = brand_enrichment.enrich_root(_brand_tree(tmp_path), dry_run=True,
                                            now_iso="2026-07-09T00:00:00Z")
    assert reports[0]["wrote"] is False
