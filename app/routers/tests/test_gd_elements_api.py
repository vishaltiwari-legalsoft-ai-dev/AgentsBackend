"""Integration tests for the Graphics Designer Element Library API (/api/gd).

Runs fully offline: the ``fs`` run-storage backend is used (default, pointed at
a tmp dir via ``GD_RUNS_DIR``) and the caller comes from the shared harness in
``conftest.py``, which also guarantees the override cannot outlive the test.
"""

import io
import json

import pytest

from app.routers.tests.conftest import client


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch, as_caller):
    monkeypatch.setenv("GD_RUNS_DIR", str(tmp_path))
    as_caller()


@pytest.fixture()
def a_run_id():
    r = client.post("/api/gd/runs", json={})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_gd_elements_catalog():
    r = client.get("/api/gd/elements")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "emoji" in body and "icons" in body and "stickers" in body
    assert isinstance(body["emoji"], list)
    assert isinstance(body["icons"], list)
    assert isinstance(body["stickers"], list)
    assert "max_elements" in body


def test_config_accepts_elements(a_run_id):
    r = client.post(
        f"/api/gd/runs/{a_run_id}/config",
        json={"elements": [{"kind": "emoji", "ref": "\U0001F600", "x": 0.5, "y": 0.5}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["config"]["elements"][0]["kind"] == "emoji"


def test_config_rejects_bad_element_kind(a_run_id):
    r = client.post(
        f"/api/gd/runs/{a_run_id}/config",
        json={"elements": [{"kind": "nope", "ref": "x"}]},
    )
    assert r.status_code == 400


def test_element_upload_png(a_run_id):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(buf, format="PNG")
    buf.seek(0)
    r = client.post(
        f"/api/gd/runs/{a_run_id}/elements/upload",
        files={"file": ("sprite.png", buf, "image/png")},
    )
    assert r.status_code == 200, r.text
    assert "ref" in r.json() and r.json()["ref"]


def test_element_upload_rejects_bad_content_type(a_run_id):
    r = client.post(
        f"/api/gd/runs/{a_run_id}/elements/upload",
        files={"file": ("sprite.txt", io.BytesIO(b"not an image"), "text/plain")},
    )
    assert r.status_code == 400


def _png_bytes(color):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), color).save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def test_element_upload_distinct_images_get_distinct_refs(a_run_id):
    """Regression: uploads used to be numbered by a never-populated
    ``run["uploads"]`` counter, so every upload computed attempt=1 and every
    image landed at the same artifact path — a second upload silently
    overwrote the first. Distinct image bytes must now produce distinct refs.
    """
    red = _png_bytes((255, 0, 0, 255))
    blue = _png_bytes((0, 0, 255, 255))

    r1 = client.post(
        f"/api/gd/runs/{a_run_id}/elements/upload",
        files={"file": ("red.png", io.BytesIO(red), "image/png")},
    )
    r2 = client.post(
        f"/api/gd/runs/{a_run_id}/elements/upload",
        files={"file": ("blue.png", io.BytesIO(blue), "image/png")},
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    ref1, ref2 = r1.json()["ref"], r2.json()["ref"]
    assert ref1 and ref2
    assert ref1 != ref2


def test_element_upload_same_bytes_dedupe_to_same_ref(a_run_id):
    """Re-uploading identical bytes should resolve to the same content-hash
    artifact path rather than growing a new (numbered) file each time."""
    red = _png_bytes((255, 0, 0, 255))

    r1 = client.post(
        f"/api/gd/runs/{a_run_id}/elements/upload",
        files={"file": ("red.png", io.BytesIO(red), "image/png")},
    )
    r2 = client.post(
        f"/api/gd/runs/{a_run_id}/elements/upload",
        files={"file": ("red-again.png", io.BytesIO(red), "image/png")},
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["ref"] == r2.json()["ref"]


# ── C1 regression: cross-run / arbitrary GCS object read ──────────────────────
# An "image" element's ``ref`` used to be accepted as any non-empty string, so a
# user could point it at another run's (or any SA-readable) ``gs://`` object and
# have the server fetch it with its own credentials at render time. The config
# endpoint must now reject an "image" element whose ``ref`` isn't owned by this
# run — a foreign ``gs://`` ref is unambiguous regardless of the active storage
# backend (fs here), since a legitimate ref never carries a ``gs://`` scheme
# from an fs-backed run.
def test_config_rejects_foreign_gs_ref_image_element(a_run_id):
    r = client.post(
        f"/api/gd/runs/{a_run_id}/config",
        json={"elements": [{
            "kind": "image",
            "ref": "gs://some-other-bucket/gd/deadbeef00cafe/stage-3-upload-x.png",
            "x": 0.5, "y": 0.5,
        }]},
    )
    assert r.status_code == 400, r.text
    # And it must NOT have been persisted onto the run's config.
    got = client.get(f"/api/gd/runs/{a_run_id}")
    assert got.json()["config"].get("elements") in (None, [])


def test_config_accepts_own_uploaded_image_element(a_run_id):
    """Legitimate path stays green: an uploaded artifact's own ref, used as an
    image element, is accepted and round-trips onto the run's config."""
    red = _png_bytes((255, 0, 0, 255))
    up = client.post(
        f"/api/gd/runs/{a_run_id}/elements/upload",
        files={"file": ("red.png", io.BytesIO(red), "image/png")},
    )
    assert up.status_code == 200, up.text
    ref = up.json()["ref"]

    r = client.post(
        f"/api/gd/runs/{a_run_id}/config",
        json={"elements": [{"kind": "image", "ref": ref, "x": 0.5, "y": 0.5}]},
    )
    assert r.status_code == 200, r.text
    els = r.json()["config"]["elements"]
    assert len(els) == 1 and els[0]["kind"] == "image" and els[0]["ref"] == ref



# =========================================================================== #
# Self-serve brands — /api/gd/brands (routers/gd_brands.py), 2026-09-25
#
# Drives the real store (``firestore_repo``'s transactions, cap and version
# bump) over the in-memory Firestore/GCS fake from tests/test_brand_enrichment,
# with dynamic brands ON so a brand created through the API is a real GD pack
# the run endpoints resolve. Nothing here reaches a bucket or a database.
# =========================================================================== #

MB = 1024 * 1024
_KIT = {"name": "Acme Co", "primary_colors": ["#1746a2"], "secondary_colors": ["#0f0f0f"],
        "fonts": ["Inter"], "tone_of_voice": "calm", "website": "https://acme.example"}
_TTF = b"\x00\x01\x00\x00" + b"\0" * 60
_OTF = b"OTTO" + b"\0" * 60
_PDF = b"%PDF-1.4\n%fake brand guidelines\n"


def _brand_png(w: int = 8, h: int = 8) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (20, 80, 200, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _upload(name: str, data: bytes, mime: str = "application/octet-stream"):
    return ("files", (name, io.BytesIO(data), mime))


def _create(**over):
    return client.post("/api/gd/brands", json={**_KIT, **over})


@pytest.fixture()
def brand_store(monkeypatch, tmp_path):
    """In-memory Firestore + GCS, deterministic signing, dynamic brands ON.

    The version and spec sources are installed explicitly (and the previous
    ones restored) so this holds whatever order the suite runs in — other
    modules detach the dynamic source in their teardown.
    """
    from tests.test_brand_enrichment import _FakeBucket, _MemDb, _MemTxn

    from app.config import settings
    from app.services import firestore_repo, gd_brand_source, storage
    from graphics_designer_agent import registry

    class _Db(_MemDb):
        def get_all(self, refs):
            return [ref.get() for ref in refs]

    db = _Db()
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
    monkeypatch.setattr(storage, "signed_url_for_gs_uri",
                        lambda uri, expires_in_hours=1: f"https://signed.test/{uri[len('gs://'):]}")
    monkeypatch.setattr(storage, "read_reference_index", lambda: None)   # no legacy index in GCS
    monkeypatch.setenv("GD_REFERENCE_DIR", str(tmp_path / "refs"))       # nor on disk
    monkeypatch.setenv("GD_DYNAMIC_BRANDS", "1")
    fonts_root = tmp_path / "fonts"
    monkeypatch.setattr(gd_brand_source, "_fonts_root", lambda: fonts_root)
    monkeypatch.setattr(gd_brand_source, "_download",
                        lambda uri, dest: dest.write_bytes(uploads[uri.split("/", 3)[3]][0]))

    prev_dynamic, prev_version = registry._DYNAMIC_SOURCE, registry._VERSION_SOURCE
    registry.register_dynamic_source(gd_brand_source.firestore_spec_source)
    registry.register_version_source(firestore_repo.brands_version)
    db.uploads, db.fonts_root = uploads, fonts_root
    yield db
    registry.register_dynamic_source(prev_dynamic)
    registry.register_version_source(prev_version)


def test_create_brand_then_the_picker_lists_it_beside_the_built_ins(brand_store):
    r = _create()
    assert r.status_code == 201, r.text
    b = r.json()["brand"]
    assert b["brand_id"] == b["slug"] == "acme-co"
    assert b["source"] == "user" and b["editable"] is True and b["archived_at"] is None
    assert b["created_by"] == "t@legalsoft.com"
    assert b["colors"] == {"primary": ["#1746A2"], "secondary": ["#0F0F0F"], "accent": []}
    assert b["fonts"] == ["Inter"] and b["tone_of_voice"] == "calm"
    assert b["website"] == "https://acme.example"
    assert b["has_kit"] is False and b["logo_url"] is None
    assert b["references"] == [] and b["reference_count"] == 0 and b["reference_cap"] == 200
    assert b["assets"] == {"logos": [], "fonts": [], "guidelines": []}
    spec = brand_store.data["brands"]["acme-co"]["brand_metadata"]["gd_spec"]
    assert spec["id"] == spec["firestore_brand_id"] == "acme-co"
    assert "#1746A2" in spec["palette"].values()          # the typed hex, untouched

    rows = client.get("/api/gd/brands").json()["brands"]
    by_id = {row["brand_id"]: row for row in rows}
    assert by_id["acme-co"]["editable"] is True and by_id["acme-co"]["reference_count"] == 0
    assert {"legalsoft", "medvirtual", "remote_attorneys"} <= set(by_id)
    assert by_id["legalsoft"] == by_id["legalsoft"] | {"source": "builtin", "editable": False,
                                                       "has_kit": True, "slug": "legalsoft"}
    assert all(row["id"] == row["brand_id"] for row in rows)  # pre-contract picker still reads it


def test_create_refuses_a_built_in_name_a_duplicate_and_bad_input(brand_store):
    r = _create(name="Legal Soft")
    assert r.status_code == 409 and r.json()["detail"] == "brand_exists"
    assert _create(primary_colors=["blue"]).status_code == 422
    assert _create(primary_colors=[]).status_code == 422
    assert _create(name="   ").status_code == 422
    assert _create(name="!!!").status_code == 422
    assert "brands" not in brand_store.data                # nothing was written
    assert _create().status_code == 201
    r = _create()
    assert r.status_code == 409 and r.json()["detail"] == "brand_exists"


def test_detail_404s_for_an_unknown_brand_and_built_ins_are_read_only(brand_store):
    assert client.get("/api/gd/brands/ghost").status_code == 404
    r = client.get("/api/gd/brands/medvirtual")
    assert r.status_code == 200, r.text
    b = r.json()["brand"]
    assert b["source"] == "builtin" and b["editable"] is False and b["reference_cap"] == 200
    assert b["colors"]["primary"] and b["fonts"]


def test_patch_rebuilds_the_spec_and_refuses_built_in_or_unknown(brand_store):
    _create()
    r = client.patch("/api/gd/brands/acme-co",
                     json={"primary_colors": ["#FF0000"], "name": "Acme Corporation"})
    assert r.status_code == 200, r.text
    b = r.json()["brand"]
    assert b["brand_id"] == "acme-co" and b["name"] == "Acme Corporation"
    assert b["colors"]["primary"] == ["#FF0000"] and b["colors"]["secondary"] == ["#0F0F0F"]
    spec = brand_store.data["brands"]["acme-co"]["brand_metadata"]["gd_spec"]
    assert spec["id"] == "acme-co" and "#FF0000" in spec["palette"].values()
    assert spec["name"] == "Acme Corporation"

    r = client.patch("/api/gd/brands/legalsoft", json={"fonts": ["X"]})
    assert r.status_code == 409 and r.json()["detail"] == "brand_not_editable"
    assert client.patch("/api/gd/brands/ghost", json={"fonts": ["X"]}).status_code == 404
    assert client.patch("/api/gd/brands/acme-co", json={"primary_colors": ["nope"]}).status_code == 422


def test_logo_upload_sets_logo_uri_and_refuses_wrong_type_or_size(brand_store):
    from app.services import storage

    _create()
    png = _brand_png()
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "logo"},
                    files=[_upload("logo.png", png, "image/png")])
    assert r.status_code == 200, r.text
    b = r.json()["brand"]
    h = storage.content_hash(png)
    path = f"brands/acme-co/logos/{h}.png"
    assert b["assets"]["logos"] == [{"path": path, "url": f"https://signed.test/test-bucket/{path}",
                                     "name": f"{h}.png"}]
    assert b["logo_url"] == f"https://signed.test/test-bucket/{path}" and b["has_kit"] is True
    assert brand_store.data["brands"]["acme-co"]["logo_uri"] == f"gs://test-bucket/{path}"

    # the extension is a claim; the bytes decide
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "logo"},
                    files=[_upload("logo.jpg", b"not an image at all", "image/jpeg")])
    assert r.status_code == 415 and r.json()["detail"] == "unsupported_file_type"
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "logo"},
                    files=[_upload("logo.png", _PDF, "image/png")])
    assert r.status_code == 415
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "logo"},
                    files=[_upload("big.png", png + b"\0" * (5 * MB + 1 - len(png)), "image/png")])
    assert r.status_code == 413 and r.json()["detail"] == "file_too_large"
    # a bad second file stops the whole request before any byte is stored
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "logo"},
                    files=[_upload("a.png", _brand_png(3, 3), "image/png"),
                           _upload("b.png", b"junk", "image/png")])
    assert r.status_code == 415
    assert set(brand_store.uploads) == {path}
    assert client.post("/api/gd/brands/acme-co/assets", data={"kind": "sticker"},
                       files=[_upload("x.png", png)]).status_code == 422
    assert client.post("/api/gd/brands/legalsoft/assets", data={"kind": "logo"},
                       files=[_upload("x.png", png)]).status_code == 409
    assert client.post("/api/gd/brands/ghost/assets", data={"kind": "logo"},
                       files=[_upload("x.png", png)]).status_code == 404


def _bomb_png() -> bytes:
    """9000x9000 1-bit PNG: ~28 KB on the wire, 81M pixels to decode."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("1", (9000, 9000), 1).save(buf, format="PNG")
    return buf.getvalue()


_SVG_WITH_DATA_IMAGE = (b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                        b'<image href="data:image/png;base64,iVBORw0KGgo="/></svg>')
_SVG_PLAIN = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
              b'<rect width="10" height="10" fill="#1746A2"/></svg>')


def test_logo_upload_refuses_decode_bombs_and_svgs_with_embedded_images(brand_store):
    _create()
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "logo"},
                    files=[_upload("wide.png", _bomb_png(), "image/png")])
    assert r.status_code == 415 and r.json()["detail"] == "image_too_large"
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "logo"},
                    files=[_upload("logo.svg", _SVG_WITH_DATA_IMAGE, "image/svg+xml")])
    assert r.status_code == 415 and r.json()["detail"] == "unsupported_file_type"
    # a bad second file stops the batch before the first byte is stored
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "logo"},
                    files=[_upload("ok.png", _brand_png(), "image/png"),
                           _upload("wide.png", _bomb_png(), "image/png")])
    assert r.status_code == 415 and r.json()["detail"] == "image_too_large"
    assert brand_store.uploads == {}
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "logo"},
                    files=[_upload("logo.svg", _SVG_PLAIN, "image/svg+xml")])
    assert r.status_code == 200, r.text
    assert len(r.json()["brand"]["assets"]["logos"]) == 1


def test_logo_count_is_capped_at_eight_per_brand(brand_store):
    _create()
    for i in range(8):
        r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "logo"},
                        files=[_upload(f"{i}.png", _brand_png(i + 1, 1), "image/png")])
        assert r.status_code == 200, r.text
    assert len(client.get("/api/gd/brands/acme-co").json()["brand"]["assets"]["logos"]) == 8
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "logo"},
                    files=[_upload("9.png", _brand_png(9, 1), "image/png")])
    assert r.status_code == 409 and r.json()["detail"] == "logo_limit_reached"
    assert len(brand_store.uploads) == 8
    # the cap is checked for the whole batch, before any file is read
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "logo"},
                    files=[_upload(f"n{i}.png", b"junk", "image/png") for i in range(9)])
    assert r.status_code == 409 and r.json()["detail"] == "logo_limit_reached"


def test_stage4_refuses_an_oversized_uploaded_logo(a_run_id):
    r = client.post(f"/api/gd/runs/{a_run_id}/stage4", data={"use_ai": "false"},
                    files=[("logo", ("wide.png", io.BytesIO(_bomb_png()), "image/png"))])
    assert r.status_code == 415 and r.json()["detail"] == "image_too_large"
    r = client.post(f"/api/gd/runs/{a_run_id}/stage4", data={"use_ai": "false"},
                    files=[("logo", ("logo.svg", io.BytesIO(_SVG_WITH_DATA_IMAGE), "image/svg+xml"))])
    assert r.status_code == 415


def test_font_upload_wires_the_pack_to_the_stored_hash_names(brand_store):
    from app.services import storage
    from graphics_designer_agent import registry

    _create()
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "font"},
                    files=[_upload("Inter-Bold.ttf", _TTF, "font/ttf"),
                           _upload("Inter-Regular.otf", _OTF, "font/otf")])
    assert r.status_code == 200, r.text
    assert [a["name"] for a in r.json()["brand"]["assets"]["fonts"]] == [
        f"{storage.content_hash(_TTF)}.ttf", f"{storage.content_hash(_OTF)}.otf"]
    meta = brand_store.data["brands"]["acme-co"]["brand_metadata"]
    stored = [u.rsplit("/", 1)[-1] for u in meta["enrichment"]["font_files"]]
    spec = meta["gd_spec"]
    assert [v["file"] for v in spec["font_variants"]] == stored
    assert [v["name"] for v in spec["font_variants"]] == ["Inter Bold", "Inter Regular"]
    assert spec["default_font"] == "Inter Bold" and spec["font_family"] == "Inter"

    # ...and the registry serves the brand with those faces, materialized by basename
    pack = registry.get_pack("acme-co")
    assert pack.font_names() == ["Inter Bold", "Inter Regular"]
    assert pack.default_font == "Inter Bold"
    assert (brand_store.fonts_root / "acme-co" / "fonts" / stored[0]).read_bytes() == _TTF

    # a colour change keeps the uploaded fonts in the rebuilt spec
    client.patch("/api/gd/brands/acme-co", json={"accent_colors": ["#00FF00"]})
    spec = brand_store.data["brands"]["acme-co"]["brand_metadata"]["gd_spec"]
    assert [v["file"] for v in spec["font_variants"]] == stored

    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "font"},
                    files=[_upload("big.ttf", _TTF + b"\0" * (2 * MB), "font/ttf")])
    assert r.status_code == 413

    # the per-brand cap (re-read: the PATCH replaced the stored brand_metadata dict)
    meta = brand_store.data["brands"]["acme-co"]["brand_metadata"]
    meta["enrichment"]["font_files"] = [f"gs://test-bucket/brands/acme-co/fonts/{i:016x}.ttf"
                                        for i in range(16)]
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "font"},
                    files=[_upload("One-More.ttf", _TTF + b"\1", "font/ttf")])
    assert r.status_code == 409 and r.json()["detail"] == "font_limit_reached"


def test_guidelines_are_one_pdf(brand_store):
    _create()
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "guidelines"},
                    files=[_upload("kit.pdf", _PDF, "application/pdf"),
                           _upload("kit2.pdf", _PDF + b"2", "application/pdf")])
    assert r.status_code == 422 and r.json()["detail"] == "too_many_files"
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "guidelines"},
                    files=[_upload("kit.png", _brand_png(), "image/png")])
    assert r.status_code == 415
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "guidelines"},
                    files=[_upload("kit.pdf", _PDF, "application/pdf")])
    assert r.status_code == 200, r.text
    assert len(r.json()["brand"]["assets"]["guidelines"]) == 1


def test_references_upload_count_delete_and_cap(brand_store):
    from app.services import storage

    _create()
    a, b = _brand_png(16, 32), _brand_png(32, 16)
    r = client.post("/api/gd/brands/acme-co/references",
                    data={"kind": "creative", "creative_type": "social_story", "note": "Spring promo"},
                    files=[_upload("a.png", a, "image/png"), _upload("b.png", b, "image/png")])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["reference_count"] == 2
    assert {ref["ref_id"] for ref in body["references"]} == {storage.content_hash(a), storage.content_hash(b)}
    first = body["references"][0]
    assert first["kind"] == "creative" and first["creative_type"] == "social_story"
    assert first["note"] == "Spring promo" and first["created_at"]
    assert first["url"].startswith("https://signed.test/test-bucket/reference_library/acme-co/")
    stored = brand_store.data["brand_references"][f"acme-co__{storage.content_hash(a)}"]
    assert (stored["width"], stored["height"]) == (16, 32)

    detail = client.get("/api/gd/brands/acme-co").json()["brand"]
    assert detail["reference_count"] == 2 and len(detail["references"]) == 2
    rows = {row["brand_id"]: row for row in client.get("/api/gd/brands").json()["brands"]}
    assert rows["acme-co"]["reference_count"] == 2

    assert client.delete(f"/api/gd/brands/acme-co/references/{storage.content_hash(a)}").status_code == 204
    assert client.get("/api/gd/brands/acme-co").json()["brand"]["reference_count"] == 1
    assert client.delete("/api/gd/brands/acme-co/references/nope").status_code == 404

    r = client.post("/api/gd/brands/acme-co/references",
                    files=[_upload("x.png", _PDF, "image/png")])
    assert r.status_code == 415
    r = client.post("/api/gd/brands/acme-co/references",
                    files=[_upload(f"{i}.png", _brand_png(i + 1, 1), "image/png") for i in range(11)])
    assert r.status_code == 422 and r.json()["detail"] == "too_many_files"
    assert client.post("/api/gd/brands/acme-co/references", data={"kind": "mood"},
                       files=[_upload("x.png", a)]).status_code == 422

    # the cap is refused for the whole batch before any upload
    uploads_before = set(brand_store.uploads)
    brand_store.data["brand_reference_counts"]["acme-co"]["active"] = 199
    r = client.post("/api/gd/brands/acme-co/references",
                    files=[_upload("c.png", _brand_png(2, 2)), _upload("d.png", _brand_png(3, 3))])
    assert r.status_code == 409 and r.json()["detail"] == "reference_cap_reached"
    assert set(brand_store.uploads) == uploads_before

    # built-ins take references; unknown brands do not
    r = client.post("/api/gd/brands/legalsoft/references", files=[_upload("ls.png", _brand_png(4, 4))])
    assert r.status_code == 201 and r.json()["reference_count"] == 1
    assert client.post("/api/gd/brands/ghost/references",
                       files=[_upload("g.png", _brand_png(5, 5))]).status_code == 404


def test_archive_is_admin_only_soft_and_hides_the_brand(brand_store, as_admin):
    _create()
    assert client.delete("/api/gd/brands/acme-co").status_code == 403   # a member may not
    as_admin()
    r = client.delete("/api/gd/brands/legalsoft")
    assert r.status_code == 409 and r.json()["detail"] == "brand_not_editable"
    assert client.delete("/api/gd/brands/ghost").status_code == 404
    assert client.delete("/api/gd/brands/acme-co").status_code == 204

    stored = brand_store.data["brands"]["acme-co"]
    assert stored["archived_at"]                                        # soft: the doc stays
    assert "acme-co" not in {row["brand_id"] for row in client.get("/api/gd/brands").json()["brands"]}
    detail = client.get("/api/gd/brands/acme-co")
    assert detail.status_code == 200 and detail.json()["brand"]["editable"] is False
    assert client.patch("/api/gd/brands/acme-co", json={"fonts": ["X"]}).status_code == 409
    assert client.post("/api/gd/brands/acme-co/assets", data={"kind": "logo"},
                       files=[_upload("l.png", _brand_png())]).status_code == 409
    assert client.post("/api/gd/brands/acme-co/references",
                       files=[_upload("r.png", _brand_png())]).status_code == 409
    assert client.delete("/api/gd/brands/acme-co").status_code == 409  # already archived
    # and the studio will not start a run for it any more
    assert client.post("/api/gd/runs", json={"brand_id": "acme-co"}).status_code == 404


def test_run_start_404s_for_an_unknown_brand_and_resolves_a_created_one(brand_store):
    r = client.post("/api/gd/runs", json={"brand_id": "ghost"})
    assert r.status_code == 404 and r.json()["detail"] == "brand_not_found"
    assert client.get("/api/gd/config", params={"brand": "ghost"}).status_code == 404
    assert client.get("/api/gd/prompts", params={"brand": "ghost"}).status_code == 404

    _create()
    r = client.post("/api/gd/runs", json={"brand_id": "acme-co"})
    assert r.status_code == 200, r.text
    assert r.json()["brand_id"] == "acme-co"
    assert client.get("/api/gd/config", params={"brand": "acme-co"}).status_code == 200
    assert client.post("/api/gd/runs", json={}).status_code == 200      # no brand: Legal Soft


# --------------------------------------------------------------------------- #
# Verification pass (senior-tester, 2026-09-25): the gaps the build's own tests
# left open — the second Cloud Run instance, the pack the registry really
# serves after a PATCH, the uploaded font reaching the editor, the bytes-not-
# extension rule with an executable, and the pre-contract picker keys.
# --------------------------------------------------------------------------- #

def test_a_brand_created_on_another_instance_is_served_here_once_the_version_moves(brand_store, monkeypatch):
    """Instance B built its registry; instance A then creates and later
    archives a brand through the STORE alone (no ``registry.refresh()`` on B,
    which is exactly what a different process cannot call). B must pick both
    changes up through ``meta/brands.version`` — and not one request earlier
    than the throttle allows, so a per-request Firestore read never sneaks in."""
    from app.services import firestore_repo
    from graphics_designer_agent import registry

    registry.list_packs()                                # B is built at version 0
    assert registry._loaded_version == 0

    doc = firestore_repo.create_brand(
        "Acme Co", {"primary_colors": ["#1746A2"], "gd_spec": _spec("Acme Co", "acme-co")},
        created_by="a@legalsoft.com")
    assert doc["id"] == "acme-co" and firestore_repo.brands_version() == 1

    # Inside the check window B still serves what it built: the run answers 404.
    assert client.post("/api/gd/runs", json={"brand_id": "acme-co"}).status_code == 404
    assert registry._loaded_version == 0

    monkeypatch.setattr(registry, "_version_checked_at", None)   # the window elapsed
    r = client.post("/api/gd/runs", json={"brand_id": "acme-co"})
    assert r.status_code == 200, r.text
    assert r.json()["brand_id"] == "acme-co"
    assert registry._loaded_version == 1
    assert "acme-co" in {row["brand_id"] for row in client.get("/api/gd/brands").json()["brands"]}

    # A archives it; B, after the window, refuses a new run and drops it from the picker.
    firestore_repo.archive_brand("acme-co")
    monkeypatch.setattr(registry, "_version_checked_at", None)
    assert client.post("/api/gd/runs", json={"brand_id": "acme-co"}).status_code == 404
    assert registry._loaded_version == 2
    assert "acme-co" not in {row["brand_id"] for row in client.get("/api/gd/brands").json()["brands"]}


def _spec(name: str, slug: str) -> dict:
    from app.services.gd_spec_builder import build_self_serve_spec

    return build_self_serve_spec(name, slug, primary_colors=["#1746A2"])


def test_patch_colours_change_the_pack_the_registry_serves(brand_store):
    """The stored spec changing is not the point; the pack a run resolves is."""
    from graphics_designer_agent import registry

    _create()
    before = registry.get_pack("acme-co")
    assert "#1746A2" in before.brand_gradient_hexes
    assert "#FF0000" not in before.brand_gradient_hexes

    r = client.patch("/api/gd/brands/acme-co", json={"primary_colors": ["#FF0000"]})
    assert r.status_code == 200, r.text
    after = registry.get_pack("acme-co")
    assert "#FF0000" in after.brand_gradient_hexes
    assert "#1746A2" not in after.brand_gradient_hexes
    assert "#FF0000" in after.locked_colors["gradient"] or "#FF0000" in after.brand_gradient_hexes
    # and the studio's config for the brand reads from the same pack
    cfg = client.get("/api/gd/config", params={"brand": "acme-co"}).json()
    assert cfg["brand_id"] == "acme-co"
    assert "#1746A2" not in json.dumps(cfg).upper()
    assert "#FF0000" in json.dumps(cfg).upper()


def test_uploaded_font_is_served_to_the_editor_canvas_by_its_display_name(brand_store, monkeypatch):
    """The editor asks ``/gd/fonts/{name}?brand=`` and the router reads
    ``pack.fonts_dir / pack.font_file(name)`` — the same loader Stage 3 uses.
    The bytes that come back must be the uploaded ones, found under the
    content-hash basename, not the Be Vietnam fallback.

    In production ``gd_brand_source._fonts_root()`` IS ``templated_brands._BRANDS_DIR``
    (materialize-to and load-from are one directory); the fixture moved the
    first to a temp dir so the suite never writes into the tree, so the second
    is moved with it here — otherwise the pack looks in the repo for a file the
    test wrote to temp."""
    from graphics_designer_agent import registry, templated_brands

    monkeypatch.setattr(templated_brands, "_BRANDS_DIR", brand_store.fonts_root)
    registry.refresh()
    _create()
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "font"},
                    files=[_upload("Inter-Bold.ttf", _TTF, "font/ttf")])
    assert r.status_code == 200, r.text
    pack = registry.get_pack("acme-co")
    assert pack.fonts_dir == brand_store.fonts_root / "acme-co" / "fonts"
    assert (pack.fonts_dir / pack.font_file("Inter Bold")).read_bytes() == _TTF
    r = client.get("/api/gd/fonts/Inter Bold", params={"brand": "acme-co"})
    assert r.status_code == 200, r.text
    assert r.content == _TTF
    assert client.get("/api/gd/fonts/Be Vietnam Bold", params={"brand": "acme-co"}).status_code == 404


def test_an_executable_renamed_to_an_image_or_font_is_refused_and_nothing_is_stored(brand_store):
    exe = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff" + b"\0" * 64
    _create()
    for kind, name, mime in (("logo", "logo.png", "image/png"), ("font", "Inter.ttf", "font/ttf"),
                             ("guidelines", "kit.pdf", "application/pdf")):
        r = client.post("/api/gd/brands/acme-co/assets", data={"kind": kind},
                        files=[_upload(name, exe, mime)])
        assert r.status_code == 415, (kind, r.text)
        assert r.json()["detail"] == "unsupported_file_type"
    r = client.post("/api/gd/brands/acme-co/references", files=[_upload("ref.png", exe, "image/png")])
    assert r.status_code == 415 and r.json()["detail"] == "unsupported_file_type"
    # right bytes, wrong slot: a real PNG offered as a font is refused too
    r = client.post("/api/gd/brands/acme-co/assets", data={"kind": "font"},
                    files=[_upload("mark.ttf", _brand_png(), "font/ttf")])
    assert r.status_code == 415
    assert brand_store.uploads == {}
    meta = brand_store.data["brands"]["acme-co"]["brand_metadata"]
    assert meta["enrichment"]["logo_files"] == meta["enrichment"]["font_files"] == []
    assert brand_store.data["brands"]["acme-co"]["logo_uri"] is None


def _ingest_via_cli(brand_store, name: str, doc_id: str, **doc_over) -> str:
    """What ``brand_enrichment`` leaves behind: a uuid-keyed doc, no ``source``,
    ``gd_spec`` with the pack id and ``firestore_brand_id`` = the doc id."""
    from app.services.gd_spec_builder import _slug, build_self_serve_spec

    spec = build_self_serve_spec(name, _slug(name), primary_colors=["#123456"])
    spec["firestore_brand_id"] = doc_id
    brand_store.data.setdefault("brands", {})[doc_id] = {
        "brand_name": name, "brand_metadata": {"gd_spec": spec, "primary_colors": ["#123456"]},
        **doc_over}
    return spec["id"]


def test_picker_lists_cli_ingested_brands_read_only(brand_store, monkeypatch):
    """A brand the enrichment CLI ingested resolves as a pack (run start
    works) and so must be in the picker — read-only. Docs with no ``gd_spec``
    and archived docs stay out."""
    from app.services import firestore_repo
    from graphics_designer_agent import registry

    _create()
    pid = _ingest_via_cli(brand_store, "Ingested Co", "c0ffee00c0ffee00c0ffee00c0ffee00")
    _ingest_via_cli(brand_store, "Gone Co", "aaaa0000aaaa0000aaaa0000aaaa0000", archived_at="2026-09-25T00:00:00+00:00")
    brand_store.data["brands"]["bbbb0000bbbb0000bbbb0000bbbb0000"] = {"brand_name": "Bare", "brand_metadata": {}}
    # Legal Soft's own ingested doc (the built-in pack's firestore id) must not double the built-in row
    _ingest_via_cli(brand_store, "Legal Soft", registry.get_pack("legalsoft").firestore_brand_id)
    monkeypatch.setattr(firestore_repo, "_brands_cache", None)   # the docs above bypassed the store
    registry.refresh()

    rows = client.get("/api/gd/brands").json()["brands"]
    by_id = {row["brand_id"]: row for row in rows}
    assert pid == "ingested-co" and pid in by_id
    assert by_id[pid] == by_id[pid] | {"id": pid, "slug": pid, "name": "Ingested Co", "source": "builtin",
                                       "editable": False, "primary_colors": ["#123456"]}
    assert "gone-co" not in by_id and "bare" not in by_id
    assert "bbbb0000bbbb0000bbbb0000bbbb0000" not in by_id and "c0ffee00c0ffee00c0ffee00c0ffee00" not in by_id
    assert by_id["legalsoft"]["source"] == "builtin" and len(rows) == len(by_id)
    assert by_id["acme-co"]["editable"] is True
    ids = [row["brand_id"] for row in rows]
    assert ids.index("acme-co") < ids.index(pid) < ids.index("legalsoft")   # user, ingested, built-in
    assert len(ids) == len(set(ids))                                          # a built-in is never doubled

    detail = client.get(f"/api/gd/brands/{pid}").json()["brand"]
    assert detail["editable"] is False and detail["colors"]["primary"] == ["#123456"]
    r = client.patch(f"/api/gd/brands/{pid}", json={"name": "Renamed"})
    assert r.status_code == 409 and r.json()["detail"] == "brand_not_editable"
    assert client.post(f"/api/gd/brands/{pid}/assets", data={"kind": "logo"},
                       files=[_upload("x.png", _brand_png())]).status_code == 409
    # references are keyed by the studio id, which is not this doc's id: refused, not mis-filed
    r = client.post(f"/api/gd/brands/{pid}/references", files=[_upload("ref.png", _brand_png(6, 6))])
    assert r.status_code == 409 and r.json()["detail"] == "brand_not_editable"
    assert "brand_references" not in brand_store.data
    assert client.get("/api/gd/brands/gone-co").status_code == 404
    r = client.post("/api/gd/runs", json={"brand_id": pid})
    assert r.status_code == 200, r.text


def test_picker_reply_keeps_the_pre_contract_keys(brand_store):
    """The picker deployed before this backend reads ``id`` per row and a
    top-level ``default``; both must still be there, and ``default`` must name
    a row that exists."""
    from graphics_designer_agent import registry

    _create()
    body = client.get("/api/gd/brands").json()
    assert body["default"] == registry.DEFAULT_BRAND_ID == "legalsoft"
    ids = [row["brand_id"] for row in body["brands"]]
    assert body["default"] in ids
    assert all(row["id"] == row["brand_id"] for row in body["brands"])
    assert all(isinstance(row["name"], str) and row["name"] for row in body["brands"])
    assert ids.index("acme-co") < ids.index("legalsoft")   # self-serve first, then built-ins
