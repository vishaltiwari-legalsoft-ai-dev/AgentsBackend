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
