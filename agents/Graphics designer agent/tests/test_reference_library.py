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
