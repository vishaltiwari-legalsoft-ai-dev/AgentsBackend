"""Tests for the Brand Reference Library (ingestion + retrieval rail).

Builds a tiny synthetic asset tree in a temp dir (real PNGs via Pillow), so the
suite has no dependency on the mock data under Data/ and runs fully offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from graphics_designer_agent import reference_library as rl


def _png(path: Path, size: tuple[int, int], color=(20, 80, 200)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")


@pytest.fixture()
def asset_tree(tmp_path: Path) -> Path:
    """A base dir laid out as <base>/<Brand>/<creative_type>/<file>."""
    brand = tmp_path / "Legal Soft"
    _png(brand / "social_story" / "recruiting_hook_join_our_team.png", (1080, 1920))
    _png(brand / "social_story" / "free_consultation_promo.png", (1080, 1920))
    _png(brand / "carousel" / "case_results_showcase.png", (1080, 1080))
    _png(brand / "brochure" / "practice_areas_brochure.png", (1240, 1754))
    # An unknown type folder must be skipped, not ingested.
    _png(brand / "mystery" / "whatever.png", (100, 100))
    return tmp_path


def test_taxonomy_has_three_types():
    keys = rl.creative_type_keys()
    assert {"social_story", "carousel", "brochure"} <= set(keys)
    assert rl.is_known_type("social_story")
    assert not rl.is_known_type("tiktok")


def test_aspect_and_orientation():
    assert rl.aspect_ratio_str(1080, 1920) == "9:16"
    assert rl.aspect_ratio_str(1080, 1080) == "1:1"
    assert rl.orientation_of(1080, 1920) == "portrait"
    assert rl.orientation_of(1920, 1080) == "landscape"
    assert rl.orientation_of(500, 500) == "square"


def test_brand_slug_matches_templated_ids():
    assert rl.brand_slug("Legal Soft") == "legalsoft"
    assert rl.brand_slug("Remote Attorneys") == "remoteattorneys"


def test_ingest_skips_unknown_type_and_understands(asset_tree: Path):
    records = rl.ingest_all(asset_tree)
    # 4 known-type files; the "mystery" folder is skipped.
    assert len(records) == 4
    types = {r.creative_type for r in records}
    assert types == {"social_story", "carousel", "brochure"}

    story = next(r for r in records if r.file_name == "recruiting_hook_join_our_team.png")
    assert story.aspect_ratio == "9:16"
    assert story.orientation == "portrait"
    assert story.format_match is True
    assert story.palette and story.palette[0].startswith("#")
    assert "recruiting" in story.tags and "social_story" in story.tags
    assert story.source == "deterministic"


def test_format_mismatch_flagged(tmp_path: Path):
    # A landscape image filed under social_story is the wrong format.
    _png(tmp_path / "Brand" / "social_story" / "wrong.png", (1920, 1080))
    rec = rl.ingest_all(tmp_path)[0]
    assert rec.orientation == "landscape"
    assert rec.format_match is False


def test_index_roundtrip(asset_tree: Path):
    records = rl.ingest_all(asset_tree)
    path = rl.write_index(asset_tree, records)
    assert path.name == rl.INDEX_FILENAME
    loaded = rl.load_index(asset_tree)
    assert len(loaded) == len(records)
    assert {r["id"] for r in loaded} == {r.id for r in records}


def test_load_index_missing_is_empty(tmp_path: Path):
    assert rl.load_index(tmp_path) == []


def test_retrieve_ranks_brief_match_first(asset_tree: Path):
    loaded = rl.load_index(asset_tree) or [r.to_dict() for r in rl.ingest_all(asset_tree)]
    hits = rl.retrieve(loaded, creative_type="social_story", brief="hiring recruiting team", k=3)
    assert hits[0]["file_name"] == "recruiting_hook_join_our_team.png"
    assert hits[0]["_score"] > hits[-1]["_score"]
    assert any("recruiting" in w for w in " ".join(hits[0]["_why"]).split())


def test_retrieve_filters_by_type_and_brand(asset_tree: Path):
    loaded = [r.to_dict() for r in rl.ingest_all(asset_tree)]
    only_carousel = rl.retrieve(loaded, creative_type="carousel", brief="", k=10)
    assert all(r["creative_type"] == "carousel" for r in only_carousel)

    legalsoft = rl.retrieve(loaded, brand_id="Legal Soft", brief="", k=10)
    assert all(r["brand_id"] == "legalsoft" for r in legalsoft)

    nobody = rl.retrieve(loaded, brand_id="Nonexistent Brand", brief="", k=10)
    assert nobody == []


def test_retrieve_empty_brief_still_returns_precedent(asset_tree: Path):
    loaded = [r.to_dict() for r in rl.ingest_all(asset_tree)]
    hits = rl.retrieve(loaded, creative_type="brochure", brief="", k=5)
    assert len(hits) == 1
    assert hits[0]["_why"]  # always explains itself


def test_summarize_for_prompt_block(asset_tree: Path):
    loaded = [r.to_dict() for r in rl.ingest_all(asset_tree)]
    hits = rl.retrieve(loaded, creative_type="social_story", brief="consultation", k=2)
    block = rl.summarize_for_prompt(hits)
    assert "reference creatives" in block.lower()
    assert "9:16" in block
    assert rl.summarize_for_prompt([]).startswith("No on-brand")



# --------------------------------------------------------------------------- #
# Stage 2 grounding (2026-09-25): browser-uploaded references (Firestore) are
# merged ahead of the Drive-synced JSON index. Backend-only: skips on the
# standalone agent interpreter.
# --------------------------------------------------------------------------- #

def _backend_imports():
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    pytest.importorskip("google.cloud.firestore")
    from app.services import firestore_repo
    from graphics_designer_agent import pipeline

    return firestore_repo, pipeline


def _uploaded_ref(pipeline) -> dict:
    return {
        "id": "acme-co__h1", "ref_id": "h1", "brand_id": "acme-co", "kind": "creative",
        "creative_type": pipeline.STUDIO_CREATIVE_TYPE,
        "gs_uri": "gs://b/reference_library/acme-co/h1.png", "object_path": "reference_library/acme-co/h1.png",
        "width": 1080, "height": 1920, "note": "Spring promo hook",
        "created_at": "2026-09-25T00:00:00+00:00", "deleted_at": None,
        # the legacy-record keys references_for_brand adds
        "file_name": "h1.png", "tags": ["spring", "promo", "hook"], "palette": [],
        "ingested_at": "2026-09-25T00:00:00+00:00", "source": "upload",
    }


def test_stage2_grounding_merges_uploaded_references_ahead_of_the_index(monkeypatch, asset_tree):
    firestore_repo, pipeline = _backend_imports()
    rl.write_index(asset_tree, rl.ingest_all(asset_tree))
    monkeypatch.setenv("GD_REFERENCE_DIR", str(asset_tree))
    uploaded = _uploaded_ref(pipeline)
    seen: dict = {}

    def fake_references_for_brand(brand_id, *, legacy_records=None):
        seen["brand_id"], seen["legacy"] = brand_id, legacy_records
        return [uploaded]

    monkeypatch.setattr(firestore_repo, "references_for_brand", fake_references_for_brand)

    records = pipeline._brand_reference_records("acme-co", rl)
    assert seen["brand_id"] == "acme-co" and len(seen["legacy"]) == 4  # the index was handed over
    assert records[0]["aspect_ratio"] == "9:16"            # derived from width/height
    assert records[0]["summary"] == "Spring promo hook"    # the note doubles as the summary

    run = {"brand_id": "acme-co", "config": {"tokens": {"headline": "Spring promo"}}}
    block = pipeline._reference_grounding(run)
    assert "h1.png" in block and "9:16" in block


def test_stage2_grounding_keeps_the_index_when_firestore_is_down(monkeypatch, asset_tree):
    firestore_repo, pipeline = _backend_imports()
    rl.write_index(asset_tree, rl.ingest_all(asset_tree))
    monkeypatch.setenv("GD_REFERENCE_DIR", str(asset_tree))

    def boom(brand_id, *, legacy_records=None):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(firestore_repo, "references_for_brand", boom)
    records = pipeline._brand_reference_records("legalsoft", rl)
    assert len(records) == 4                               # never fewer than before
    assert pipeline._brand_reference_records(None, rl) == records  # no brand: index only


# --------------------------------------------------------------------------- #
# Verification pass (senior-tester, 2026-09-25): the REAL merge through the
# pipeline — the earlier test fakes ``references_for_brand`` and so proves the
# hand-over, not the union.
# --------------------------------------------------------------------------- #

def _uploaded_doc(brand_id: str, ref_id: str, *, kind: str, creative_type=None) -> dict:
    """What ``firestore_repo.list_references`` returns for one uploaded file."""
    return {
        "id": f"{brand_id}__{ref_id}", "ref_id": ref_id, "brand_id": brand_id, "kind": kind,
        "creative_type": creative_type, "object_path": f"reference_library/{brand_id}/{ref_id}.png",
        "gs_uri": f"gs://b/reference_library/{brand_id}/{ref_id}.png", "content_type": "image/png",
        "width": 1080, "height": 1920, "note": "Spring promo hook", "uploaded_by": "a@b.com",
        "created_at": "2026-09-25T00:00:00+00:00", "deleted_at": None,
    }


def test_stage2_sees_uploaded_and_legacy_references_together(monkeypatch, asset_tree):
    """Both halves in one list: the Firestore doc (uploaded through the sheet)
    ahead of the four Drive-synced records for the same brand, nothing from
    another brand, and the uploaded one carrying the legacy keys the retriever
    reads. ``list_references`` is the only seam replaced — the merge is real."""
    firestore_repo, pipeline = _backend_imports()
    rl.write_index(asset_tree, rl.ingest_all(asset_tree))
    monkeypatch.setenv("GD_REFERENCE_DIR", str(asset_tree))
    docs = {
        "legalsoft": [_uploaded_doc("legalsoft", "h1", kind="creative", creative_type="social_story")],
        "acme-co": [_uploaded_doc("acme-co", "h2", kind="reference")],
    }
    monkeypatch.setattr(firestore_repo, "list_references",
                        lambda brand_id, limit=200: list(docs.get(brand_id, [])))

    records = pipeline._brand_reference_records("legalsoft", rl)
    assert [r.get("source") for r in records] == ["upload"] + ["deterministic"] * 4
    assert records[0]["file_name"] == "h1.png" and records[0]["aspect_ratio"] == "9:16"
    assert {r["brand_id"] for r in records} == {"legalsoft"}      # acme-co's upload stays out
    assert all(r.get("gs_uri") or r.get("abs_path") for r in records)

    # ...and the grounding block a Stage-2 prompt gets names the upload beside the index
    run = {"brand_id": "legalsoft", "config": {"tokens": {"headline": "Spring promo"}}}
    block = pipeline._reference_grounding(run)
    assert "h1.png" in block
    assert "recruiting_hook_join_our_team.png" in block or "free_consultation_promo.png" in block

    # a brand with only uploads (no Drive folder) is grounded on them alone
    only_uploads = pipeline._brand_reference_records("acme-co", rl)
    assert [r["file_name"] for r in only_uploads] == ["h2.png"]


def test_a_reference_uploaded_from_the_sheet_grounds_stage_two(monkeypatch, asset_tree):
    """A reference uploaded with kind='reference' (the sheet's default) and no
    creative_type — neither the studio type nor a style category — still
    grounds Stage 2 for its brand: uploads lead the primary hits whatever
    their type. The hyphenated self-serve slug must match too."""
    firestore_repo, pipeline = _backend_imports()
    monkeypatch.setenv("GD_REFERENCE_DIR", str(asset_tree / "empty"))   # no Drive index at all
    monkeypatch.setattr(firestore_repo, "list_references",
                        lambda brand_id, limit=200: [_uploaded_doc("acme-co", "h2", kind="reference")])
    run = {"brand_id": "acme-co", "config": {"tokens": {"headline": "Spring promo"}}}
    assert "h2.png" in pipeline._reference_grounding(run)


def test_uploads_lead_the_studio_hits_and_style_uploads_feed_the_style_pool(monkeypatch, asset_tree):
    firestore_repo, pipeline = _backend_imports()
    rl.write_index(asset_tree, rl.ingest_all(asset_tree))
    monkeypatch.setenv("GD_REFERENCE_DIR", str(asset_tree))
    docs = [
        _uploaded_doc("legalsoft", "mood", kind="reference"),                       # any type
        _uploaded_doc("legalsoft", "swatch", kind="reference", creative_type="brand_gradient"),
        _uploaded_doc("legalsoft", "story", kind="creative", creative_type="social_story"),
    ]
    monkeypatch.setattr(firestore_repo, "list_references", lambda brand_id, limit=200: list(docs))
    run = {"brand_id": "legalsoft", "config": {"tokens": {"headline": "Spring promo"}}}
    block = pipeline._reference_grounding(run)
    lines = [ln for ln in block.splitlines() if ln[:1].isdigit()]
    names = [ln.split("] ", 1)[1].split(" ")[0] for ln in lines]
    assert names[0] == "mood.png" and "story.png" in names                    # uploads lead, any type
    assert names[-1] == "swatch.png" and "[brand_gradient]" in block          # the style pool took it
    assert any(n in names for n in ("recruiting_hook_join_our_team.png", "free_consultation_promo.png"))
    assert "case_results_showcase.png" not in names                           # carousel: not studio, not style


def test_brand_ids_match_with_separators_ignored():
    assert rl.same_brand("acme-co", "acmeco") and rl.same_brand("Remote Attorneys", "remote_attorneys")
    assert not rl.same_brand("acme-co", "acme-co-2")
    records = [
        {"id": "a", "brand_id": "acmeco", "creative_type": "social_story", "file_name": "a.png"},
        {"id": "b", "brand_id": "acme-co", "creative_type": "social_story", "file_name": "b.png"},
        {"id": "c", "brand_id": "other", "creative_type": "social_story", "file_name": "c.png"},
        {"id": "d", "brand_id": "acme_co", "creative_type": "brand_gradient", "file_name": "d.png"},
    ]
    assert {r["id"] for r in rl.retrieve(records, brand_id="acme-co", k=5)} == {"a", "b", "d"}
    hits = rl.retrieve_for_generation(records, brand_id="acme-co", creative_type="social_story")
    assert [r["id"] for r in hits][-1] == "d" and {r["id"] for r in hits} == {"a", "b", "d"}
