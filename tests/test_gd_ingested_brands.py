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



# --------------------------------------------------------------------------- #
# Self-serve brand picker rows (2026-09-25) — pure shaping in routers/gd_brands
# --------------------------------------------------------------------------- #

def _user_doc(**over):
    doc = {
        "id": "acme-co", "name": "Acme Co", "slug": "acme-co", "source": "user",
        "archived_at": None, "logo_uri": "gs://b/brands/acme-co/logos/0123456789abcdef.png",
        "brand_metadata": {
            "primary_colors": ["#1746A2"],
            "enrichment": {"logo_files": ["gs://b/brands/acme-co/logos/0123456789abcdef.png"],
                           "font_files": [], "guideline_files": []},
        },
    }
    doc.update(over)
    return doc


def test_self_serve_summary_row(monkeypatch):
    from app.routers import gd_brands

    monkeypatch.setattr(gd_brands, "_view_url", lambda uri: f"signed:{uri}" if uri else None)
    assert gd_brands._summary_from_doc(_user_doc(), reference_count=3) == {
        "brand_id": "acme-co", "id": "acme-co", "name": "Acme Co", "slug": "acme-co",
        "source": "user", "editable": True,
        "logo_url": "signed:gs://b/brands/acme-co/logos/0123456789abcdef.png",
        "primary_colors": ["#1746A2"], "has_kit": True, "reference_count": 3,
    }
    bare = _user_doc(logo_uri=None, archived_at="2026-09-25T00:00:00+00:00",
                     brand_metadata={"primary_colors": [], "enrichment": {}})
    row = gd_brands._summary_from_doc(bare, reference_count=0)
    assert row["editable"] is False and row["has_kit"] is False and row["logo_url"] is None


def test_reference_url_is_always_a_string(monkeypatch):
    from app.routers import gd_brands

    monkeypatch.setattr(gd_brands, "_view_url", lambda uri: None)          # unsigned / no object
    row = gd_brands._reference({"ref_id": "abc", "gs_uri": "gs://b/x.png", "kind": "reference"})
    assert row["url"] == "" and row["ref_id"] == "abc" and row["note"] == ""
    monkeypatch.setattr(gd_brands, "_view_url", lambda uri: f"signed:{uri}")
    assert gd_brands._reference({"id": "legacy", "gs_uri": "gs://b/y.png"})["url"] == "signed:gs://b/y.png"


def test_cli_ingested_doc_is_a_read_only_picker_row(monkeypatch):
    """A doc the enrichment CLI wrote: uuid id, no ``source``, pack id in
    ``gd_spec.id``. The row is keyed by the pack id and never editable."""
    from app.routers import gd_brands

    monkeypatch.setattr(gd_brands, "_view_url", lambda uri: None)
    doc = {"id": "9717e502d6774c57a458771d1bd7c281", "brand_name": "Ingested Co",
           "brand_metadata": {"gd_spec": {"id": "ingested-co"}, "primary_colors": ["#111111"]}}
    row = gd_brands._summary_from_doc(doc, reference_count=1)
    assert row["brand_id"] == row["id"] == row["slug"] == "ingested-co"
    assert row["source"] == "builtin" and row["editable"] is False
    assert row["name"] == "Ingested Co" and row["reference_count"] == 1


def test_asset_rows_carry_the_object_path_not_the_bucket(monkeypatch):
    from app.routers import gd_brands

    monkeypatch.setattr(gd_brands, "_view_url", lambda uri: "https://signed")
    assert gd_brands._asset("gs://bucket/brands/acme-co/fonts/abc.ttf") == {
        "path": "brands/acme-co/fonts/abc.ttf", "url": "https://signed", "name": "abc.ttf"}


def test_upload_type_is_read_from_the_bytes():
    from app.routers.gd_brands import sniff_ext

    assert sniff_ext(b"\x89PNG\r\n\x1a\n" + b"\0" * 8) == "png"
    assert sniff_ext(b"\xff\xd8\xff\xe0" + b"\0" * 8) == "jpg"
    assert sniff_ext(b"RIFF\0\0\0\0WEBPVP8 ") == "webp"
    assert sniff_ext(b"%PDF-1.7\n") == "pdf"
    assert sniff_ext(b"OTTO" + b"\0" * 8) == "otf"
    assert sniff_ext(b"\x00\x01\x00\x00" + b"\0" * 8) == "ttf"
    assert sniff_ext(b'\xef\xbb\xbf<?xml version="1.0"?>\n<svg xmlns="x"/>') == "svg"
    assert sniff_ext(b"<svg xmlns='x'></svg>") == "svg"
    assert sniff_ext(b"GIF89a") is None          # not in any allow-list
    assert sniff_ext(b"<html><svg/></html>") is None
    assert sniff_ext(b"") is None
