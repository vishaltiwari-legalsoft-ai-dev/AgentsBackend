"""Setup-screen brand strip: only enriched/gd_spec brands appear, counts
degrade to 0 on missing fields, and helper failures never raise."""
from __future__ import annotations

import app  # noqa: F401 - registers agent roots on sys.path
from app.routers.graphics_designer import ingested_brand_entries


def _brand(bid, name, meta):
    return {"id": bid, "brand_name": name, "brand_metadata": meta}


def test_only_ingested_brands_listed():
    brands = [
        _brand("b1", "Ingested Co", {"enrichment": {"font_files": ["a.otf"], "logo_files": ["l.png", "m.png"]},
                                     "primary_colors": ["#111111", "#222222"]}),
        _brand("b2", "Spec Only", {"gd_spec": {"id": "spec-only"}}),
        _brand("b3", "Bare Brand", {}),
        _brand("b4", "No Meta", None),
    ]
    out = ingested_brand_entries(brands, logo_url_for=lambda _id: None, reference_count=lambda _n: None)
    assert [e["id"] for e in out] == ["b1", "b2"]
    assert out[0]["counts"] == {"fonts": 1, "logos": 2}
    assert out[0]["primary_colors"] == ["#111111", "#222222"]
    assert out[1]["counts"] == {"fonts": 0, "logos": 0}


def test_reference_count_included_only_when_known():
    brands = [_brand("b1", "Ingested Co", {"enrichment": {}})]
    with_refs = ingested_brand_entries(brands, logo_url_for=lambda _id: None, reference_count=lambda _n: 7)
    without = ingested_brand_entries(brands, logo_url_for=lambda _id: None, reference_count=lambda _n: None)
    assert with_refs[0]["counts"]["reference_assets"] == 7
    assert "reference_assets" not in without[0]["counts"]


def test_helper_failures_degrade_not_raise():
    def boom(_x):
        raise RuntimeError("no gcs")
    brands = [_brand("b1", "Ingested Co", {"enrichment": {"logo_files": ["l.png"]}})]
    out = ingested_brand_entries(brands, logo_url_for=boom, reference_count=boom)
    assert out[0]["logo_url"] is None
    assert "reference_assets" not in out[0]["counts"]
