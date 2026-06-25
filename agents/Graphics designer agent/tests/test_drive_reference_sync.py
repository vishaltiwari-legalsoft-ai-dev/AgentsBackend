"""Tests for the Google Drive → Reference Library sync + retrieval grounding.

All offline: the Drive API is replaced by a fake service, and grounding is
exercised against a temp index. No GCP, no network.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from PIL import Image

# Put backend/ on the path so the ``app`` package (drive_source, storage) is
# importable when this runs under the backend venv.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from graphics_designer_agent import reference_library as rl  # noqa: E402


# --------------------------------------------------------------------------- #
# New taxonomy: folder aliases + reference-only categories
# --------------------------------------------------------------------------- #

def test_reference_categories_are_ingestible_not_generation_types():
    assert rl.is_reference_category("brand_gradient")
    assert rl.is_reference_category("newsletter")
    # Reference-only categories are NOT generation creative types.
    assert not rl.is_known_type("brand_gradient")
    assert not rl.is_known_type("newsletter")
    # But both are ingestible.
    assert rl.is_ingestible_type("brand_gradient")
    assert rl.is_ingestible_type("carousel")


def test_resolve_folder_type_handles_drive_names():
    assert rl.resolve_folder_type("Carousel") == "carousel"
    assert rl.resolve_folder_type("Story") == "social_story"
    assert rl.resolve_folder_type("LS Gradients") == "brand_gradient"
    assert rl.resolve_folder_type("Newsletter Graphics") == "newsletter"
    # Truly unmapped folders resolve to None (caller skips + logs).
    assert rl.resolve_folder_type("Random Junk") is None


# --------------------------------------------------------------------------- #
# Ingestion now indexes the new categories
# --------------------------------------------------------------------------- #

def _png(path: Path, size=(1080, 1080), color=(10, 30, 120)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")


@pytest.fixture()
def drive_like_tree(tmp_path: Path) -> Path:
    brand = tmp_path / "Legal Soft"
    _png(brand / "social_story" / "recruiting_hook.png", (1080, 1920))
    _png(brand / "carousel" / "case_results.png", (1080, 1080))
    _png(brand / "brand_gradient" / "signature_blue.png", (1600, 900))
    _png(brand / "newsletter" / "weekly_update.png", (1200, 1500))
    return tmp_path


def test_ingest_indexes_reference_categories(drive_like_tree: Path):
    records = rl.ingest_all(drive_like_tree)
    types = {r.creative_type for r in records}
    assert types == {"social_story", "carousel", "brand_gradient", "newsletter"}
    grad = next(r for r in records if r.creative_type == "brand_gradient")
    # A style swatch has no expected orientation, so it always "fits" its format.
    assert grad.format_match is True
    assert grad.gs_uri == ""  # no GCS in tests → not mirrored


def test_index_roundtrip_preserves_gs_uri(drive_like_tree: Path):
    records = rl.ingest_all(drive_like_tree)
    records[0].gs_uri = "gs://bucket/reference_library/x.png"
    rl.write_index(drive_like_tree, records)
    loaded = rl.load_index(drive_like_tree)
    by_id = {r["id"]: r for r in loaded}
    assert by_id[records[0].id]["gs_uri"] == "gs://bucket/reference_library/x.png"


def test_mirror_to_gcs_noop_without_gcs(drive_like_tree: Path):
    records = rl.ingest_all(drive_like_tree)
    assert rl.mirror_to_gcs(records) == 0
    assert all(r.gs_uri == "" for r in records)


def test_retrieve_for_generation_blends_style_refs(drive_like_tree: Path):
    records = [r.to_dict() for r in rl.ingest_all(drive_like_tree)]
    hits = rl.retrieve_for_generation(
        records, brand_id="Legal Soft", creative_type="social_story",
        brief="recruiting team", k=3, style_k=2,
    )
    kinds = [h["creative_type"] for h in hits]
    assert "social_story" in kinds
    # A signature gradient is pulled in as supplementary style precedent.
    assert "brand_gradient" in kinds


# --------------------------------------------------------------------------- #
# Drive download — fully mocked service
# --------------------------------------------------------------------------- #

class _Exec:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return self._data


class _FakeFiles:
    """Mimics the subset of the Drive v3 ``files()`` API we call."""

    def __init__(self, tree):
        self.tree = tree  # parent_id -> [child dicts]

    def list(self, q, **_kw):
        parent = re.search(r"'([^']+)' in parents", q).group(1)
        return _Exec({"files": self.tree.get(parent, []), "nextPageToken": None})

    def get_media(self, fileId, **_kw):
        return fileId  # token; _download_file is monkeypatched in tests


class _FakeService:
    def __init__(self, tree):
        self._files = _FakeFiles(tree)

    def files(self):
        return self._files


_FOLDER = "application/vnd.google-apps.folder"


def test_download_folder_maps_subfolders(tmp_path: Path, monkeypatch):
    from app.services import drive_source

    tree = {
        "ROOT": [
            {"id": "f_car", "name": "Carousel", "mimeType": _FOLDER},
            {"id": "f_story", "name": "Story", "mimeType": _FOLDER},
            {"id": "f_grad", "name": "LS Gradients", "mimeType": _FOLDER},
            {"id": "f_news", "name": "Newsletter Graphics", "mimeType": _FOLDER},
            {"id": "f_junk", "name": "Random", "mimeType": _FOLDER},
            {"id": "loose", "name": "stray.png", "mimeType": "image/png"},
        ],
        "f_car": [{"id": "c1", "name": "promo.png", "mimeType": "image/png"}],
        "f_story": [{"id": "s1", "name": "hook.png", "mimeType": "image/png"}],
        "f_grad": [{"id": "g1", "name": "blue.png", "mimeType": "image/png"}],
        "f_news": [{"id": "n1", "name": "update.png", "mimeType": "image/png"}],
        "f_junk": [{"id": "j1", "name": "noise.png", "mimeType": "image/png"}],
    }

    # Real PNG bytes so a later ingest can understand them.
    from io import BytesIO

    def _bytes_for(_service, file_id):
        buf = BytesIO()
        Image.new("RGB", (64, 64), (1, 2, 3)).save(buf, "PNG")
        return buf.getvalue()

    monkeypatch.setattr(drive_source, "_download_file", _bytes_for)

    summary = drive_source.download_folder(
        "ROOT", tmp_path, brand_name="Legal Soft", service=_FakeService(tree)
    )

    assert summary["downloaded"] == 4  # car/story/grad/news; junk + loose skipped
    assert "Random" in summary["skipped_folders"]
    assert summary["by_type"] == {
        "carousel": 1, "social_story": 1, "brand_gradient": 1, "newsletter": 1,
    }
    # Files landed in the layout the ingester expects.
    assert (tmp_path / "Legal Soft" / "carousel" / "promo.png").is_file()
    assert (tmp_path / "Legal Soft" / "social_story" / "hook.png").is_file()
    # And the whole thing ingests cleanly.
    records = rl.ingest_all(tmp_path)
    assert {r.creative_type for r in records} == {
        "carousel", "social_story", "brand_gradient", "newsletter",
    }


# --------------------------------------------------------------------------- #
# Generation grounding — Stage-2 prompt picks up reference precedent
# --------------------------------------------------------------------------- #

def test_stage2_prompt_grounds_on_references(tmp_path: Path, monkeypatch):
    from graphics_designer_agent import pipeline
    from graphics_designer_agent.runs import create_run

    # Point the library at a temp dir and index one Legal Soft social story.
    monkeypatch.setenv("GD_REFERENCE_DIR", str(tmp_path))
    _png(tmp_path / "Legal Soft" / "social_story" / "recruiting_hook.png", (1080, 1920))
    rl.write_index(tmp_path, rl.ingest_all(tmp_path))

    run = create_run("user-ref")  # defaults to the Legal Soft pack (brand_id "legalsoft")
    run["config"]["tokens"] = {"headline": "Join our recruiting team", "cta": "Apply now"}

    built = pipeline.build_prompt(run, 2, "D")
    assert "reference creatives" in built["text"].lower()
    assert "9:16" in built["text"]  # the story's aspect ratio surfaces in the block


def test_stage2_prompt_unchanged_without_index(tmp_path: Path, monkeypatch):
    from graphics_designer_agent import pipeline
    from graphics_designer_agent.runs import create_run

    monkeypatch.setenv("GD_REFERENCE_DIR", str(tmp_path))  # empty dir → no index
    run = create_run("user-ref-2")
    built = pipeline.build_prompt(run, 2, "D")
    assert "reference creatives" not in built["text"].lower()
