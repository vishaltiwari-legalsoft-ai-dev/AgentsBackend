"""find_brand_logo ranking (app layer). Needs the backend deps, so it skips on the
standalone agent-suite interpreter and runs under the backend venv."""

import pathlib
import sys

import pytest

# Put backend/ on the path so ``app`` is importable when run under the venv.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
pytest.importorskip("google.cloud.firestore")  # backend-only dependency

from app.services import firestore_repo as fr  # noqa: E402


def test_logo_score_orders_logo_then_svg_then_png_then_other():
    assert (fr._logo_score({"file_name": "Primary Logo.svg", "file_type": "image/svg+xml"})
            > fr._logo_score({"file_name": "hero.png", "file_type": "image/png"}))
    assert fr._logo_score({"file_name": "x.svg"}) > fr._logo_score({"file_name": "x.png"})
    assert fr._logo_score({"file_name": "x.png"}) > fr._logo_score({"file_name": "x.jpg"})


def test_is_image_asset_by_mime_or_extension():
    assert fr._is_image_asset({"file_type": "image/png"})
    assert fr._is_image_asset({"file_name": "logo.SVG", "file_type": ""})
    assert not fr._is_image_asset({"file_name": "notes.pdf", "file_type": "application/pdf"})


def test_find_brand_logo_picks_best_curated_image(monkeypatch):
    records = [
        {"file_name": "banner.jpg", "file_type": "image/jpeg", "file_url": "gs://b/1",
         "creative_metadata": {"author": "Marketing Team"}},
        {"file_name": "Brand Logo.svg", "file_type": "image/svg+xml", "file_url": "gs://b/2",
         "creative_metadata": {"author": "Marketing Team"}},
        {"file_name": "ai-gen.png", "file_type": "image/png", "file_url": "gs://b/3",
         "creative_metadata": {"author": "AgentOS"}},               # AI output → excluded
        {"file_name": "notes.pdf", "file_type": "application/pdf", "file_url": "gs://b/4",
         "creative_metadata": {"author": "Marketing Team"}},        # not an image → excluded
    ]
    monkeypatch.setattr(fr, "list_creatives_by_brand", lambda bid, limit=500: records)
    best = fr.find_brand_logo("brand-1")
    assert best is not None and best["file_url"] == "gs://b/2"


def test_find_brand_logo_none_when_no_brand_or_no_candidates(monkeypatch):
    assert fr.find_brand_logo("") is None
    monkeypatch.setattr(fr, "list_creatives_by_brand", lambda bid, limit=500: [])
    assert fr.find_brand_logo("brand-x") is None


# --------------------------------------------------------------------------- #
# Stage 4 logo resolution (2026-09-25): the brand doc's ``logo_uri`` wins over
# the creatives-collection guess.
# --------------------------------------------------------------------------- #

def _png_bytes() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), (20, 80, 200, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_brand_logo_record_prefers_the_docs_logo_uri(monkeypatch):
    from app.services import gd_brand_source as src

    def guess_must_not_run(_bid):
        raise AssertionError("the creatives guess ran although logo_uri was set")

    monkeypatch.setattr(fr, "get_brand", lambda bid: {
        "id": bid, "logo_uri": "gs://b/brands/acme-co/logos/0123456789abcdef.png"})
    monkeypatch.setattr(fr, "find_brand_logo", guess_must_not_run)
    assert src.brand_logo_record("acme-co") == {
        "file_url": "gs://b/brands/acme-co/logos/0123456789abcdef.png",
        "file_name": "0123456789abcdef.png",
        "file_type": "image/png",
        "source": "logo_uri",
    }


def test_brand_logo_record_falls_back_to_the_creatives_guess(monkeypatch):
    from app.services import gd_brand_source as src

    monkeypatch.setattr(fr, "get_brand", lambda bid: {"id": bid, "logo_uri": None})
    monkeypatch.setattr(fr, "find_brand_logo",
                        lambda bid: {"file_url": "gs://b/2", "file_name": "Brand Logo.svg"})
    assert src.brand_logo_record("x")["file_url"] == "gs://b/2"
    assert src.brand_logo_record(None) is None
    assert src.brand_logo_record("") is None


def test_brand_logo_record_survives_a_failed_doc_read(monkeypatch):
    from app.services import gd_brand_source as src

    def boom(_bid):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(fr, "get_brand", boom)
    monkeypatch.setattr(fr, "find_brand_logo", lambda bid: None)
    assert src.brand_logo_record("x") is None  # degraded to the guess, never raised


def test_stage4_composites_the_docs_logo(monkeypatch):
    """``pipeline.brand_logo_png`` goes through the same resolver, so an
    uploaded logo reaches the composite without a creatives-collection entry."""
    from app.services import gd_brand_source, storage
    from graphics_designer_agent import pipeline

    png = _png_bytes()
    monkeypatch.setattr(gd_brand_source, "brand_logo_record", lambda fid: {
        "file_url": "gs://b/brands/x/logos/h.png", "file_name": "h.png", "file_type": "image/png"})
    monkeypatch.setattr(storage, "download_bytes", lambda uri: png)
    out = pipeline.brand_logo_png("medvirtual")  # a pack that maps to a Firestore brand
    assert out is not None and out.startswith(b"\x89PNG")


# --------------------------------------------------------------------------- #
# Direct-to-GCS logo uploads (2026-10-09): SVG logos are parsed with
# defusedxml, refused when they can execute, embed or fetch anything, and
# rasterized to a 2048 px PNG that Stage 4 reads. The SVG itself is kept only
# as a download.
# --------------------------------------------------------------------------- #
from app.routers.tests.test_gd_elements_api import (  # noqa: E402,F401 - fixture import
    _KIT, brand_store, client, direct_upload, install_direct_gcs,
)
from app.services import upload_intake  # noqa: E402

_SVG_OK = (b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
           b'viewBox="0 0 200 100"><defs><linearGradient id="g"><stop offset="0" '
           b'stop-color="#1746A2"/></linearGradient></defs>'
           b'<rect width="200" height="100" fill="url(#g)"/>'
           b'<text x="10" y="50">Big Data: Inc</text></svg>')


def _svg(inner: bytes, attrs: bytes = b"") -> bytes:
    return (b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            b'width="10" height="10" ' + attrs + b">" + inner + b"</svg>")


_UNSAFE_SVGS = {
    "script": _svg(b"<script>alert(1)</script>"),
    "foreignObject": _svg(b"<foreignObject><div xmlns='http://www.w3.org/1999/xhtml'/></foreignObject>"),
    "image": _svg(b'<image href="#x" width="1" height="1"/>'),
    "external href": _svg(b'<use href="https://evil.example/x.svg#a"/>'),
    "external xlink:href": _svg(b'<a xlink:href="http://evil.example/"><rect width="1" height="1"/></a>'),
    "char-ref javascript": _svg(b'<a href="&#106;avascript:alert(1)"><rect width="1" height="1"/></a>'),
    "event handler": _svg(b'<rect width="1" height="1" onclick="alert(1)"/>'),
    "data uri in fill": _svg(b'<rect width="1" height="1" fill="url(data:image/png;base64,AAAA)"/>'),
    "external url()": _svg(b'<rect width="1" height="1" fill="url(https://evil.example/p.svg#g)"/>'),
    "css import": _svg(b'<style>@import "https://evil.example/x.css";</style>'),
    "entity": (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x "boom">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg">&x;</svg>'),
}


@pytest.mark.parametrize("name", sorted(_UNSAFE_SVGS))
def test_check_svg_refuses_anything_that_can_execute_embed_or_fetch(name):
    with pytest.raises(upload_intake.IntakeRejected) as caught:
        upload_intake.check_svg(_UNSAFE_SVGS[name], file_name="logo.svg")
    assert caught.value.status == 422 and caught.value.body["code"] == "unsafe_svg"


def test_check_svg_accepts_local_references_and_ordinary_text():
    info = upload_intake.check_svg(_SVG_OK)
    assert (info.width, info.height) == (200.0, 100.0)
    assert upload_intake.svg_raster_size(info) == (2048, 1024)
    # an extreme aspect ratio cannot ask the renderer for a 200,000 px canvas
    tall = upload_intake.SvgInfo(width=1.0, height=100000.0)
    assert upload_intake.svg_raster_size(tall) == (1, 2048)


@pytest.fixture()
def svg_brand(brand_store, monkeypatch):
    from app.main import app as fastapi_app
    from app.security import get_current_user

    monkeypatch.setitem(fastapi_app.dependency_overrides, get_current_user,
                        lambda: {"id": "u1", "email": "t@legalsoft.com"})
    gcs = install_direct_gcs(monkeypatch, store=brand_store.uploads)
    assert client.post("/api/gd/brands", json=_KIT).status_code == 201
    return gcs


def _fake_raster(monkeypatch):
    """cairosvg needs native libcairo (in the Cloud Run image, rarely on a dev
    box): stand in with a PNG of exactly the size the real call is asked for."""
    import io

    from PIL import Image

    asked = []

    def raster(data, info):
        w, h = upload_intake.svg_raster_size(info)
        asked.append((w, h))
        buf = io.BytesIO()
        Image.new("RGBA", (w, h), (23, 70, 162, 255)).save(buf, format="PNG")
        return buf.getvalue()

    monkeypatch.setattr(upload_intake, "rasterize_svg", raster)
    return asked


def test_an_svg_logo_is_rasterized_and_stage_4_reads_the_png(svg_brand, monkeypatch):
    import hashlib

    from app.services import gd_brand_source

    asked = _fake_raster(monkeypatch)
    _, r = direct_upload("/api/gd/brands/acme-co", "logo", _SVG_OK, svg_brand,
                         content_type="image/svg+xml", file_name="mark.svg")
    assert r.status_code == 200, r.text
    md5 = hashlib.md5(_SVG_OK).hexdigest()
    up = r.json()["upload"]
    assert asked == [(2048, 1024)] and up["kind"] == "svg"
    assert up["working"] == {"width": 2048, "height": 1024, "format": "png"}
    working = f"brands/acme-co/logos/{md5}-w2048.png"
    original = f"brands/acme-co/originals/{md5}.svg"
    assert svg_brand.store[original] == (_SVG_OK, "image/svg+xml")
    assert svg_brand.meta[original]["disposition"] == "attachment"
    assert "response-content-disposition=attachment" in up["original"]["download_url"]
    rec = gd_brand_source.brand_logo_record("acme-co")
    assert rec["file_url"] == f"gs://test-bucket/{working}" and rec["file_type"] == "image/png"


def test_an_unsafe_svg_logo_is_refused_and_its_upload_deleted(svg_brand, monkeypatch):
    _fake_raster(monkeypatch)
    for name in ("script", "external href"):
        _, r = direct_upload("/api/gd/brands/acme-co", "logo", _UNSAFE_SVGS[name], svg_brand,
                             content_type="image/svg+xml", file_name="evil.svg")
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "unsafe_svg" and r.json()["detail"]["file"] == "evil.svg"
    assert svg_brand.pending() == []
    assert not any("/originals/" in p or "/logos/" in p for p in svg_brand.store)


def test_an_svg_over_5_mb_is_refused_even_when_signed_as_octet_stream(svg_brand, monkeypatch):
    _fake_raster(monkeypatch)
    big = _svg(b"<!--" + b"x" * (5 * 1024 * 1024) + b"-->")
    _, r = direct_upload("/api/gd/brands/acme-co", "logo", big, svg_brand,
                         content_type="application/octet-stream", file_name="big.svg")
    assert r.status_code == 413 and r.json()["detail"]["code"] == "file_too_large"
    assert r.json()["detail"]["limit_bytes"] == 5 * 1024 * 1024


def test_no_svg_renderer_is_an_honest_503_that_keeps_the_upload(svg_brand, monkeypatch):
    def unavailable(_data, _info):
        raise upload_intake.SvgRendererUnavailable("no libcairo")

    monkeypatch.setattr(upload_intake, "rasterize_svg", unavailable)
    _, r = direct_upload("/api/gd/brands/acme-co", "logo", _SVG_OK, svg_brand,
                         content_type="image/svg+xml")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "svg_renderer_unavailable"
    assert len(svg_brand.pending()) == 1
