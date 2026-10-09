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


# =========================================================================== #
# Cloud storage across instances (B1). GD_STORAGE_BACKEND=cloud with an
# in-memory Firestore + GCS standing in for the shared stores. A run is started
# on "instance A", then every later request is served by an instance with a
# fresh, empty disk and fresh process-local caches - which is what Cloud Run
# does on scale-out, scale-down and every deploy. Nothing may come from disk.
# =========================================================================== #

OTHER_TENANT = {"id": "gd-cloud-stranger", "email": "s@legalsoft.com", "is_admin": False,
                "is_creator": False, "session_id": "", "timezone": "UTC"}


class _FakeGcs:
    """The slice of ``google.cloud.storage.Client`` the storage service uses.
    Signing is refused outright: artifact URLs must never be signed GCS links."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def bucket(self, bucket_name):
        objects = self.objects

        class _Blob:
            def __init__(self, path):
                self.key = f"{bucket_name}/{path}"

            def upload_from_string(self, data, content_type=None, timeout=None, **_kw):
                objects[self.key] = bytes(data)

            def download_as_bytes(self, timeout=None, **_kw):
                from google.api_core.exceptions import NotFound

                if self.key not in objects:
                    raise NotFound(self.key)
                return objects[self.key]

            def exists(self, timeout=None, **_kw):
                return self.key in objects

            def generate_signed_url(self, **_kw):
                raise AssertionError("artifact URLs must be API proxy paths, never signed GCS URLs")

        class _Bucket:
            def blob(self, path):
                return _Blob(path)

        return _Bucket()


@pytest.fixture()
def cloud_instances(monkeypatch, tmp_path):
    import copy
    import types

    from tests.test_brand_enrichment import _MemCol, _MemDb, _MemDoc

    from app.config import settings
    from app.services import firestore_repo, storage
    from graphics_designer_agent import registry
    from graphics_designer_agent import runs as gd_runs
    from graphics_designer_agent.creative import runs as cr_runs

    class _Doc(_MemDoc):
        def set(self, data, merge=False):   # a real store keeps a copy, never the live dict
            super().set(copy.deepcopy(data), merge=merge)

    class _Col(_MemCol):
        def document(self, doc_id):
            return _Doc(self._db, self._col, doc_id)

    class _Db(_MemDb):
        def collection(self, name):
            return _Col(self, name)

    db, gcs = _Db(), _FakeGcs()
    monkeypatch.setattr(firestore_repo, "_db", lambda: db)
    monkeypatch.setattr(settings, "gcp_project_id", "test-project", raising=False)
    monkeypatch.setattr(settings, "gcs_bucket_name", "test-bucket", raising=False)
    monkeypatch.setattr(storage, "_storage", lambda: gcs)
    monkeypatch.setattr(storage, "is_configured", lambda: True)   # over the root guard
    monkeypatch.setattr(storage, "read_reference_index", lambda: None)
    monkeypatch.setattr(gd_runs, "GD_STORAGE_BACKEND", "cloud")
    monkeypatch.setattr(cr_runs, "GD_STORAGE_BACKEND", "cloud")
    monkeypatch.setenv("GD_IMAGE_PROVIDER", "mock")
    monkeypatch.setenv("GD_REFERENCE_DIR", str(tmp_path / "no-refs"))
    disks: list = []

    def boot():
        """A new instance: its own empty disk, nothing cached in-process."""
        disk = tmp_path / f"instance-{len(disks)}"
        disks.append(disk)
        monkeypatch.setattr(gd_runs, "RUNS_ROOT", disk / "gd")
        monkeypatch.setattr(cr_runs, "CREATIVE_RUNS_ROOT", disk / "creative")
        monkeypatch.setattr(firestore_repo, "_brands_cache", None)
        monkeypatch.setattr(firestore_repo, "_brands_version_seen", None)
        registry.refresh()

    boot()
    return types.SimpleNamespace(db=db, gcs=gcs, boot=boot, disks=disks)


def _urls(payload) -> list[str]:
    """Every ``url`` value anywhere in a JSON payload."""
    found: list[str] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k == "url" and isinstance(v, str):
                found.append(v)
            else:
                found += _urls(v)
    elif isinstance(payload, list):
        for v in payload:
            found += _urls(v)
    return found


def _disk_files(instances) -> list:
    return [p for d in instances.disks if d.exists() for p in d.rglob("*") if p.is_file()]


def test_a_cloud_run_is_served_by_any_instance_from_shared_storage(cloud_instances, as_caller):
    gcs = cloud_instances.gcs

    # -- instance A: start the run, generate + approve Stage 1, upload a subject
    rid = client.post("/api/gd/runs", json={}).json()["id"]
    r = client.post(f"/api/gd/runs/{rid}/generate", json={"stage": 1, "variant": "A"})
    assert r.status_code == 200, r.text
    assert r.json()["attempt"]["artifact"] == "stage-1-A-1.png"
    assert client.post(f"/api/gd/runs/{rid}/approve", json={"stage": 1}).status_code == 200
    r = client.post(f"/api/gd/runs/{rid}/subject/upload",
                    files={"file": ("me.png", io.BytesIO(_png_bytes((200, 30, 30, 255))), "image/png")})
    assert r.status_code == 200, r.text
    subject_ref = r.json()["ref"]
    assert "/" not in subject_ref and not subject_ref.startswith("gs:")
    assert client.post(f"/api/gd/runs/{rid}/config",
                       json={"subject_asset_ref": subject_ref}).status_code == 200

    # -- instance B: fresh disk, nothing cached
    cloud_instances.boot()
    r = client.get(f"/api/gd/runs/{rid}")
    assert r.status_code == 200, r.text
    run = r.json()
    approved_url = run["stages"]["1"]["approved"]["url"]
    assert approved_url == f"/api/gd/runs/{rid}/artifact/stage-1-A-1.png"
    # Every URL handed to the browser is a relative API path - the relay prefixes
    # "/backend" to it, so an absolute or gs:// URL could never load.
    assert _urls(run) and all(u.startswith(f"/api/gd/runs/{rid}/artifact/") for u in _urls(run))
    img = client.get(approved_url)
    assert img.status_code == 200
    assert img.content == gcs.objects[f"test-bucket/generated/gd/{rid}/stage-1-A-1.png"]
    # The lab frontend builds this path itself from config.subject_asset_ref.
    assert client.get(f"/api/gd/runs/{rid}/artifact/{subject_ref}").status_code == 200

    # Stage 2 chains the Stage-1 image (and the uploaded subject) from storage.
    r = client.post(f"/api/gd/runs/{rid}/generate", json={"stage": 2, "variant": "UPLOAD"})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/gd/runs/{rid}/generate", json={"stage": 2, "variant": "A"})
    assert r.status_code == 200, r.text
    assert client.post(f"/api/gd/runs/{rid}/approve", json={"stage": 2}).status_code == 200

    # -- instance C: Stage 3 reads the approved Stage-2 base from storage
    cloud_instances.boot()
    cfg = client.get(f"/api/gd/runs/{rid}").json()["config"]
    subs = [{**s, "approved": True} for s in cfg["subheadings"]]
    r = client.post(f"/api/gd/runs/{rid}/config", json={
        "subheadings": subs,
        "token_approvals": {t: {"approved": True} for t in ("headline", "highlight", "cta")}})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/gd/runs/{rid}/generate", json={"stage": 3})
    assert r.status_code == 200, r.text
    assert client.post(f"/api/gd/runs/{rid}/approve", json={"stage": 3}).status_code == 200

    # -- instance D: Stage 4 composites the logo onto the Stage-3 image from storage
    cloud_instances.boot()
    r = client.post(f"/api/gd/runs/{rid}/stage4", data={"use_ai": "false"},
                    files=[("logo", ("logo.png", io.BytesIO(_png_bytes((0, 0, 0, 255))), "image/png"))])
    assert r.status_code == 200, r.text
    assert client.get(r.json()["attempt"]["url"]).status_code == 200

    # Honest edges of the proxy: missing -> 404, an fs-style path -> 400,
    # another tenant -> the usual indistinguishable 404.
    assert client.get(f"/api/gd/runs/{rid}/artifact/stage-9-Z-1.png").status_code == 404
    assert client.get(f"/api/gd/runs/{rid}/artifact/stage-1/A-1.png").status_code == 400
    as_caller(OTHER_TENANT)
    r = client.get(approved_url)
    assert r.status_code == 404 and r.json()["detail"] == "Run not found"

    # Nothing touched any instance disk; everything sits in this run partition.
    assert not _disk_files(cloud_instances)
    assert gcs.objects and all(k.startswith(f"test-bucket/generated/gd/{rid}/") for k in gcs.objects)


def test_a_cloud_creative_run_is_polled_and_downloaded_from_any_instance(cloud_instances):
    gcs = cloud_instances.gcs
    rid = client.post("/api/creative/runs", json={"creative_type": "blog", "brief": "tips"}).json()["id"]
    assert client.post(f"/api/creative/runs/{rid}/plan",
                       json={"count": 2, "use_llm": False}).status_code == 200
    assert client.post(f"/api/creative/runs/{rid}/plan/approve").status_code == 200
    r = client.post(f"/api/creative/runs/{rid}/generate")
    assert r.status_code == 200, r.text

    cloud_instances.boot()   # the 2 s poller and the downloads land elsewhere
    r = client.get(f"/api/creative/runs/{rid}")
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["progress"]["state"] == "done" and run["artifacts"]
    for art in run["artifacts"]:
        assert art["url"] == f"/api/creative/runs/{rid}/artifact/{art['name']}"
        assert art["ai"] is False and art["fallback_reason"]   # mock provider: placeholders
        body = client.get(art["url"])
        assert body.status_code == 200
        assert body.content == gcs.objects[f"test-bucket/generated/creative/{rid}/{art['name']}"]
    assert not _disk_files(cloud_instances)


def test_upload_and_stage4_handlers_are_sync_so_blocking_io_stays_off_the_event_loop():
    """B1(b): in cloud mode these handlers make Firestore/GCS calls and decode
    images; as ``async def`` all of that ran on the event loop."""
    import inspect

    from app.routers import graphics_designer as gd

    for handler in (gd.stage4_endpoint, gd.gd_subject_upload, gd.gd_element_upload):
        assert not inspect.iscoroutinefunction(handler), handler.__name__


# =========================================================================== #
# Direct-to-GCS uploads (2026-10-09) — sign, browser PUT, finalize.
#
# ``_DirectGcs`` behaves like the bucket for this flow: the browser PUT is
# refused unless it carries exactly the signed headers (content type, length
# range, write-once), metadata reports GCS's own MD5, and rewrite / ranged
# read / stream / delete work. It writes into the same ``uploads`` dict the
# brand store fake uses, so font materialization reads the same objects.
# The GD agent suites import ``install_direct_gcs`` / ``direct_upload`` from
# here. Large and adversarial files are generated per test, never committed.
# =========================================================================== #

class _DirectGcs:
    def __init__(self, store=None, bucket="test-bucket"):
        self.store: dict = {} if store is None else store   # path -> (bytes, content type)
        self.meta: dict = {}                                 # path -> {generation, disposition}
        self.signed: dict = {}                               # path -> signing kwargs
        self.bucket_name = bucket
        self._generation = 0

    # -- the browser ---------------------------------------------------------
    def browser_put(self, signed: dict, data: bytes, headers: dict | None = None) -> int:
        """PUT ``data`` to a signed URL the way GCS checks it: 403 when the
        headers are not exactly the signed ones, 400 outside the length
        range, 412 when the object already exists (write-once)."""
        path = signed["upload_url"].split(f"/{self.bucket_name}/", 1)[1].split("?", 1)[0]
        kw = self.signed[path]
        assert kw["method"] == "PUT" and kw["version"] == "v4"
        sent = signed["headers"] if headers is None else headers
        if sent != {"Content-Type": kw["content_type"], **kw["headers"]}:
            return 403
        low, high = map(int, sent["x-goog-content-length-range"].split(","))
        if not low <= len(data) <= high:
            return 400
        if sent.get("x-goog-if-generation-match") == "0" and path in self.store:
            return 412
        self._write(path, data, sent["Content-Type"])
        return 200

    def pending(self) -> list[str]:
        return sorted(p for p in self.store if p.startswith("uploads/pending/")
                      and not p.endswith(".receipt.json"))

    # -- the client library surface ------------------------------------------
    def _write(self, path, data, content_type, disposition=None):
        self._generation += 1
        self.store[path] = (bytes(data), content_type)
        self.meta[path] = {"generation": self._generation, "disposition": disposition}

    def bucket(self, name):
        assert name == self.bucket_name, name
        return _DirectBucket(self)


class _DirectBucket:
    def __init__(self, gcs):
        self.gcs = gcs

    def blob(self, path, **_kw):
        return _DirectBlob(self.gcs, path)

    def get_blob(self, path, timeout=None, **_kw):
        return _DirectBlob(self.gcs, path) if path in self.gcs.store else None


class _DirectBlob:
    def __init__(self, gcs, path):
        import base64
        import hashlib

        self.gcs, self.name = gcs, path
        self.content_type = None
        self.content_disposition = None
        if path in gcs.store:
            data, self.content_type = gcs.store[path]
            self.size = len(data)
            self.md5_hash = base64.b64encode(hashlib.md5(data).digest()).decode()
            self.generation = gcs.meta.get(path, {}).get("generation", 1)

    def _data(self, if_generation_match=None):
        from google.api_core.exceptions import NotFound, PreconditionFailed

        if self.name not in self.gcs.store:
            raise NotFound(self.name)
        generation = self.gcs.meta.get(self.name, {}).get("generation", 1)
        if if_generation_match is not None and generation != if_generation_match:
            raise PreconditionFailed(self.name)
        return self.gcs.store[self.name][0]

    def generate_signed_url(self, *, version, expiration, method, content_type=None, headers=None,
                            response_disposition=None, **_kw):
        self.gcs.signed[self.name] = {"version": version, "expiration": expiration, "method": method,
                                      "content_type": content_type, "headers": dict(headers or {}),
                                      "response_disposition": response_disposition}
        query = f"X-Goog-Signature=fake&X-Goog-Method={method}"
        if response_disposition:
            query += "&response-content-disposition=" + response_disposition.replace(" ", "%20")
        return f"https://storage.test/{self.gcs.bucket_name}/{self.name}?{query}"

    def upload_from_string(self, data, content_type=None, timeout=None, **_kw):
        self.gcs._write(self.name, data, content_type)

    def download_as_bytes(self, start=None, end=None, if_generation_match=None, timeout=None, **_kw):
        data = self._data(if_generation_match)
        if start is None:
            return data
        return data[start:(end + 1) if end is not None else None]

    def open(self, mode="rb", chunk_size=None, if_generation_match=None, timeout=None, **_kw):
        assert mode == "rb"
        return io.BytesIO(self._data(if_generation_match))

    def rewrite(self, source, token=None, if_source_generation_match=None, if_generation_match=None,
                timeout=None, **_kw):
        from google.api_core.exceptions import PreconditionFailed

        data = source._data(if_source_generation_match)
        if if_generation_match == 0 and self.name in self.gcs.store:
            raise PreconditionFailed(self.name)
        self.gcs._write(self.name, data, self.content_type, self.content_disposition)
        return None, len(data), len(data)

    def delete(self, timeout=None, **_kw):
        from google.api_core.exceptions import NotFound

        if self.name not in self.gcs.store:
            raise NotFound(self.name)
        del self.gcs.store[self.name]
        self.gcs.meta.pop(self.name, None)


def install_direct_gcs(monkeypatch, store: dict | None = None) -> _DirectGcs:
    """Direct uploads ON over a ``_DirectGcs`` (and nothing real)."""
    from app.config import settings
    from app.services import storage

    gcs = _DirectGcs(store)
    monkeypatch.setattr(storage, "_storage", lambda: gcs)
    monkeypatch.setattr(storage, "_signing_kwargs", lambda: {})
    monkeypatch.setattr(storage, "is_configured", lambda: True)
    monkeypatch.setattr(settings, "gcs_bucket_name", "test-bucket", raising=False)
    monkeypatch.setattr(settings, "jwt_secret", "test-jwt-secret-for-tickets", raising=False)
    monkeypatch.setenv("GD_DIRECT_UPLOADS", "1")
    return gcs


def direct_upload(base: str, surface: str, data: bytes, gcs: _DirectGcs, *,
                  content_type: str = "application/octet-stream", file_name: str = "file.bin",
                  finalize: bool = True, http=None, **extra):
    """sign -> browser PUT -> finalize. Returns ``(signed, finalize_response)``."""
    http = http or client
    r = http.post(f"{base}/uploads", json={"surface": surface, "content_type": content_type,
                                           "size": len(data), "file_name": file_name})
    assert r.status_code == 200, r.text
    signed = r.json()
    assert gcs.browser_put(signed, data) == 200
    if not finalize:
        return signed, None
    return signed, http.post(f"{base}/uploads/finalize",
                             json={"ticket": signed["ticket"], "file_name": file_name, **extra})


def _md5(data: bytes) -> str:
    import hashlib

    return hashlib.md5(data).hexdigest()


def _real_ttf(family: str = "Acme Sans", style: str = "Bold") -> bytes:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    pen = TTGlyphPen(None)
    pen.moveTo((0, 0))
    pen.lineTo((500, 700))
    pen.lineTo((1000, 0))
    pen.closePath()
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef", "A"])
    fb.setupCharacterMap({0x41: "A"})
    fb.setupGlyf({".notdef": TTGlyphPen(None).glyph(), "A": pen.glyph()})
    fb.setupHorizontalMetrics({".notdef": (1000, 0), "A": (1000, 0)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": family, "styleName": style})
    fb.setupOS2()
    fb.setupPost()
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


def _pdf(pages: int) -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _jpeg(w: int, h: int, color=(200, 40, 40), mode: str = "RGB", **save) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new(mode, (w, h), color).save(buf, format="JPEG", **save)
    return buf.getvalue()


@pytest.fixture()
def direct(brand_store, monkeypatch):
    gcs = install_direct_gcs(monkeypatch, store=brand_store.uploads)
    assert _create().status_code == 201
    return gcs


BRAND = "/api/gd/brands/acme-co"


def test_direct_upload_routes_answer_503_while_the_flag_is_off(direct, monkeypatch):
    monkeypatch.delenv("GD_DIRECT_UPLOADS", raising=False)
    r = client.post(f"{BRAND}/uploads", json={"surface": "logo", "content_type": "image/png"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "direct_uploads_disabled"
    r = client.post(f"{BRAND}/uploads/finalize", json={"ticket": "x.y"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "direct_uploads_disabled"
    monkeypatch.setenv("GD_DIRECT_UPLOADS", "0")      # anything but 1/true/yes/on is off
    rid = client.post("/api/gd/runs", json={}).json()["id"]
    r = client.post(f"/api/gd/runs/{rid}/uploads", json={"surface": "subject"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "direct_uploads_disabled"
    r = client.post(f"/api/gd/runs/{rid}/uploads/finalize", json={"ticket": "x.y"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "direct_uploads_disabled"
    assert direct.signed == {}


def test_sign_contract_is_a_write_once_size_ranged_put_for_a_server_named_object(direct):
    r = client.post(f"{BRAND}/uploads", json={"surface": "logo", "content_type": "image/png",
                                               "size": 1234, "file_name": "../../etc/passwd.png"})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["method"] == "PUT" and s["surface"] == "logo" and s["max_bytes"] == 50 * MB
    assert s["headers"] == {"Content-Type": "image/png",
                            "x-goog-content-length-range": f"1,{50 * MB}",
                            "x-goog-if-generation-match": "0"}
    (path,) = direct.signed
    assert path.startswith("uploads/pending/logo/acme-co/u1/") and "passwd" not in path
    assert direct.signed[path]["expiration"].total_seconds() == 600
    assert s["ticket"] and s["expires_at"] and s["ticket_expires_at"]
    # GCS refuses a PUT that changes a signed header, is empty, or overwrites
    assert direct.browser_put(s, b"x", headers={**s["headers"], "Content-Type": "text/html"}) == 403
    assert direct.browser_put(s, b"") == 400
    assert direct.browser_put(s, _brand_png()) == 200
    assert direct.browser_put(s, _brand_png(3, 3)) == 412
    # an SVG is signed with the SVG cap; a declared HEIC is refused before any upload
    r = client.post(f"{BRAND}/uploads", json={"surface": "logo", "content_type": "image/svg+xml"})
    assert r.json()["max_bytes"] == 5 * MB
    r = client.post(f"{BRAND}/uploads", json={"surface": "reference", "content_type": "image/heic",
                                               "file_name": "IMG_0001.HEIC"})
    assert r.status_code == 415
    d = r.json()["detail"]
    assert d["code"] == "unsupported_file_type" and d["got"] == "heic" and "JPEG/PNG" in d["message"]
    assert d["accepted"] == ["png", "jpeg", "webp", "tiff"] and d["file"] == "IMG_0001.HEIC"
    r = client.post(f"{BRAND}/uploads", json={"surface": "reference", "content_type": "image/png",
                                               "size": 50 * MB + 1})
    assert r.status_code == 413 and r.json()["detail"]["code"] == "file_too_large"


def test_logo_finalize_keeps_the_original_and_gd_reads_the_working_copy(direct):
    from app.services import firestore_repo, gd_brand_source

    png = _brand_png(64, 32)
    md5 = _md5(png)
    _, r = direct_upload(BRAND, "logo", png, direct, content_type="image/png",
                         file_name="Acme Logo.png")
    assert r.status_code == 200, r.text
    up = r.json()["upload"]
    assert up["status"] == "stored" and up["already_finalized"] is False and up["kind"] == "png"
    assert up["file"] == "Acme Logo.png" and up["content_id"] == md5
    assert up["original"]["bytes"] == len(png)
    assert (up["original"]["width"], up["original"]["height"]) == (64, 32)
    assert up["working"] == {"width": 64, "height": 32, "format": "png"}
    assert "response-content-disposition=attachment" in up["original"]["download_url"]

    working = f"brands/acme-co/logos/{md5}-w4096.png"
    original = f"brands/acme-co/originals/{md5}.png"
    (logo,) = r.json()["brand"]["assets"]["logos"]
    assert logo["path"] == working and logo["original"]["bytes"] == len(png)
    assert firestore_repo.get_brand("acme-co")["logo_uri"] == f"gs://test-bucket/{working}"
    # the original is a byte-identical server-side copy, stored as an attachment
    assert direct.store[original][0] == png
    assert direct.meta[original]["disposition"] == "attachment"
    assert direct.store[working][1] == "image/png"
    # Stage 4 resolves the brand's logo to the WORKING copy
    assert gd_brand_source.brand_logo_record("acme-co")["file_url"] == f"gs://test-bucket/{working}"
    assert direct.pending() == []


def test_finalize_twice_is_a_no_op_that_answers_the_same(direct):
    png = _brand_png(20, 20)
    signed, first = direct_upload(BRAND, "logo", png, direct, content_type="image/png")
    assert first.status_code == 200, first.text
    objects = set(direct.store)
    again = client.post(f"{BRAND}/uploads/finalize", json={"ticket": signed["ticket"]})
    assert again.status_code == 200, again.text
    assert again.json()["upload"]["already_finalized"] is True
    assert again.json()["upload"]["content_id"] == first.json()["upload"]["content_id"]
    assert again.json()["brand"]["assets"] == first.json()["brand"]["assets"]
    assert set(direct.store) == objects
    # the SAME bytes through a second ticket land on the same ids: still one logo
    _, third = direct_upload(BRAND, "logo", png, direct, content_type="image/png")
    assert third.status_code == 200, third.text
    assert len(third.json()["brand"]["assets"]["logos"]) == 1


def test_ticket_problems_are_refused_and_touch_nothing(direct, as_caller):
    from app.services import gd_direct_uploads, upload_intake

    signed, _ = direct_upload(BRAND, "logo", _brand_png(), direct, content_type="image/png",
                              finalize=False)
    ticket = signed["ticket"]
    (pending,) = direct.pending()

    def finalize(tok, base=BRAND):
        return client.post(f"{base}/uploads/finalize", json={"ticket": tok})

    # tampered: point the claims at someone else's object, keep the signature
    body, sig = ticket.split(".")
    claims = json.loads(upload_intake._unb64(body))
    claims["object"] = "uploads/pending/logo/acme-co/someone-else/victim"
    forged = upload_intake._b64(json.dumps(claims).encode()) + "." + sig
    r = finalize(forged)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "upload_ticket_invalid"
    assert finalize("garbage").json()["detail"]["code"] == "upload_ticket_invalid"
    # expired: a genuinely signed ticket whose hour is over
    expired, _ = upload_intake.mint_ticket(
        gd_direct_uploads._ticket_key(), sub="u1", surface="logo", target="acme-co",
        object_name=pending, cap=50 * MB, now=0)
    r = finalize(expired)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "upload_ticket_expired"
    # wrong target: a ticket for acme-co cannot finalize into another brand
    assert _create(name="Beta Co").status_code == 201
    r = finalize(ticket, base="/api/gd/brands/beta-co")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "upload_ticket_wrong_target"
    # a brand ticket never finalizes on the run route
    rid = client.post("/api/gd/runs", json={}).json()["id"]
    r = client.post(f"/api/gd/runs/{rid}/uploads/finalize", json={"ticket": ticket})
    assert r.status_code == 403
    # wrong user: another member cannot finalize someone else's upload
    as_caller({"id": "u2", "email": "other@legalsoft.com"})
    r = finalize(ticket)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "upload_ticket_wrong_user"
    as_caller()
    assert direct.pending() == [pending]          # none of the above touched it
    assert not any("/originals/" in p for p in direct.store)
    assert finalize(ticket).status_code == 200


def test_rights_revoked_before_finalize_refuse_it_and_delete_the_pending_object(direct, brand_store):
    signed, _ = direct_upload(BRAND, "logo", _brand_png(), direct, content_type="image/png",
                              finalize=False)
    assert len(direct.pending()) == 1
    brand_store.data["brands"]["acme-co"]["archived_at"] = "2026-10-09T00:00:00+00:00"
    r = client.post(f"{BRAND}/uploads/finalize", json={"ticket": signed["ticket"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "brand_not_editable"
    assert direct.pending() == []
    assert not any("/originals/" in p for p in direct.store)


def test_built_in_packs_take_references_only(direct):
    r = client.post("/api/gd/brands/legalsoft/uploads", json={"surface": "logo"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "brand_not_editable"
    jpg = _jpeg(300, 200)
    _, r = direct_upload("/api/gd/brands/legalsoft", "reference", jpg, direct,
                         content_type="image/jpeg", note="launch post", kind="reference")
    assert r.status_code == 200, r.text
    ref = r.json()["references"][0]
    assert ref["ref_id"] == _md5(jpg) and ref["kind"] == "reference" and ref["note"] == "launch post"
    assert r.json()["reference_count"] == 1
    assert ref["original"]["bytes"] == len(jpg)
    assert direct.store[f"reference_library/legalsoft/{_md5(jpg)}-w4096.jpg"][1] == "image/jpeg"


def test_reference_working_copy_is_a_jpeg_that_generation_reads(direct):
    from graphics_designer_agent import reference_library as rl

    from app.services import firestore_repo

    png = _brand_png(400, 300)
    _, r = direct_upload(BRAND, "reference", png, direct, content_type="image/png",
                         creative_type="social_story", note="Spring promo")
    assert r.status_code == 200, r.text
    assert r.json()["upload"]["working"]["format"] == "jpeg"
    assert r.json()["references"][0]["creative_type"] == "social_story"
    recs = firestore_repo.references_for_brand("acme-co", legacy_records=[])
    assert len(recs) == 1 and recs[0]["file_name"] == f"{_md5(png)}-w4096.jpg"
    data, mime = rl.load_reference_bytes(recs[0])
    assert mime == "image/jpeg" and data[:3] == b"\xff\xd8\xff"


def test_reference_cap_is_checked_before_upload(direct, brand_store):
    brand_store.data.setdefault("brand_reference_counts", {})["acme-co"] = {"active": 200}
    r = client.post(f"{BRAND}/uploads", json={"surface": "reference", "content_type": "image/png"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "reference_cap_reached"


def test_logo_cap_of_eight_holds_at_sign_time(direct, brand_store):
    meta = brand_store.data["brands"]["acme-co"]["brand_metadata"]
    meta["enrichment"]["logo_files"] = [f"gs://test-bucket/brands/acme-co/logos/{i:032x}.png"
                                        for i in range(8)]
    r = client.post(f"{BRAND}/uploads", json={"surface": "logo", "content_type": "image/png"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "logo_limit_reached"


def test_guidelines_pdf_is_page_counted_and_offered_only_as_a_download(direct):
    pdf = _pdf(3)
    _, r = direct_upload(BRAND, "guidelines", pdf, direct, content_type="application/pdf",
                         file_name="Brand Book.pdf")
    assert r.status_code == 200, r.text
    up = r.json()["upload"]
    assert up["original"]["pages"] == 3 and up["working"] is None and up["kind"] == "pdf"
    (g,) = r.json()["brand"]["assets"]["guidelines"]
    assert g["path"] == f"brands/acme-co/originals/{_md5(pdf)}.pdf"
    assert "response-content-disposition=attachment" in g["url"]


def test_pdf_with_a_bad_tail_or_too_many_pages_is_refused_and_deleted(direct):
    truncated = _pdf(1).replace(b"%%EOF", b"")
    _, r = direct_upload(BRAND, "guidelines", truncated, direct, content_type="application/pdf",
                         file_name="cut.pdf")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "pdf_truncated"
    assert r.json()["detail"]["file"] == "cut.pdf"
    _, r = direct_upload(BRAND, "guidelines", _pdf(301), direct, content_type="application/pdf")
    assert r.status_code == 422
    d = r.json()["detail"]
    assert (d["code"], d["pages"], d["limit_pages"]) == ("pdf_too_many_pages", 301, 300)
    # a PNG renamed .pdf is a type refusal, not a PDF refusal
    _, r = direct_upload(BRAND, "guidelines", _brand_png(), direct, content_type="application/pdf")
    assert r.status_code == 415 and r.json()["detail"]["got"] == "png"
    assert direct.pending() == []
    assert not any("/originals/" in p for p in direct.store)


def test_a_real_font_is_wired_into_the_pack_and_a_fake_one_is_refused(direct, brand_store):
    from graphics_designer_agent import registry

    ttf = _real_ttf()
    _, r = direct_upload(BRAND, "font", ttf, direct, content_type="font/ttf",
                         file_name="AcmeSans-Bold.ttf")
    assert r.status_code == 200, r.text
    name = f"{_md5(ttf)}.ttf"
    meta = brand_store.data["brands"]["acme-co"]["brand_metadata"]
    assert meta["enrichment"]["font_files"] == [f"gs://test-bucket/brands/acme-co/originals/{name}"]
    assert [v["file"] for v in meta["gd_spec"]["font_variants"]] == [name]
    pack = registry.get_pack("acme-co")
    assert len(pack.font_names()) == 1 and pack.font_names()[0].endswith("Bold")
    assert (brand_store.fonts_root / "acme-co" / "fonts" / name).read_bytes() == ttf
    (f,) = r.json()["brand"]["assets"]["fonts"]
    assert "response-content-disposition=attachment" in f["url"]

    _, r = direct_upload(BRAND, "font", _TTF, direct, content_type="font/ttf", file_name="fake.ttf")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "font_unreadable"
    _, r = direct_upload(BRAND, "font", b"wOFF" + b"\0" * 60, direct, file_name="web.woff")
    assert r.status_code == 415 and r.json()["detail"]["got"] == "woff"
    assert direct.pending() == []


def test_a_busy_decode_slot_answers_503_retry_after_and_keeps_the_upload(direct, monkeypatch):
    from app.services import upload_intake

    signed, _ = direct_upload(BRAND, "logo", _brand_png(), direct, content_type="image/png",
                              finalize=False)
    monkeypatch.setattr(upload_intake, "DECODE_SLOT_TIMEOUT_SECONDS", 0.05)
    assert upload_intake._DECODE_SLOT.acquire(timeout=1)
    try:
        r = client.post(f"{BRAND}/uploads/finalize", json={"ticket": signed["ticket"]})
    finally:
        upload_intake._DECODE_SLOT.release()
    assert r.status_code == 503 and r.json()["detail"]["code"] == "upload_busy"
    assert r.headers["Retry-After"] == str(upload_intake.DECODE_RETRY_AFTER_SECONDS)
    assert len(direct.pending()) == 1               # not a rejection: the retry works
    r = client.post(f"{BRAND}/uploads/finalize", json={"ticket": signed["ticket"]})
    assert r.status_code == 200, r.text


def test_storage_fault_mid_finalize_is_a_503_and_keeps_the_upload(direct, monkeypatch):
    from app.services import storage

    signed, _ = direct_upload(BRAND, "logo", _brand_png(), direct, content_type="image/png",
                              finalize=False)

    def boom(*_a, **_k):
        raise RuntimeError("GCS upload failed: 503 backend error")

    monkeypatch.setattr(storage, "put_object", boom)
    r = client.post(f"{BRAND}/uploads/finalize", json={"ticket": signed["ticket"]})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "upload_storage_unavailable"
    assert "backend error" not in r.text            # internals stay in the log
    assert len(direct.pending()) == 1
