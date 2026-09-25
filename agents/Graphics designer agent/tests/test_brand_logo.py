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
