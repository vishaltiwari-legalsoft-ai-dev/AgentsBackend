"""The Drive sync must never destroy the reference tree it failed to replace.

``/ref-library/sync-drive`` used to ``rmtree`` the brand's subtree and only then
call Drive. Every failure mode of that call — API disabled, folder unshared, a
429, the paging cap — lands after the delete, and on a deployment without GCS
the local tree is the only copy. These pin the staged-then-swapped contract.

The endpoint is called directly rather than through the TestClient: the swap,
not the auth wiring, is what is under test (``require_admin`` is exercised by
the auth suite), and it keeps the fixture to a directory on disk.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.config import settings
from app.routers import reference_library as refrouter
from app.services import drive_source
from graphics_designer_agent import reference_library as rl

BRAND_SEG = drive_source._safe_segment(settings.gd_drive_brand_name)


@pytest.fixture
def library(tmp_path, monkeypatch):
    """An existing, indexed reference tree — the thing that must survive."""
    monkeypatch.setenv("GD_REFERENCE_DIR", str(tmp_path))
    existing = tmp_path / BRAND_SEG / "social_ad" / "keeper.png"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"the only copy")
    # The success path indexes what it downloaded; that rail has its own tests.
    monkeypatch.setattr(rl, "ingest_all", lambda base, use_llm=False: [])
    monkeypatch.setattr(rl, "mirror_to_gcs", lambda records: 0)
    monkeypatch.setattr(rl, "write_index", lambda base, records: None)
    return existing


def _sync():
    return refrouter.sync_drive(use_llm=False, folder_id="folder-1", _admin={"id": "admin"})


def test_a_failed_drive_download_leaves_the_existing_tree_intact(library, monkeypatch):
    def _boom(fid, dest, **kwargs):
        raise RuntimeError("Drive API has not been used in project 255561670915")

    monkeypatch.setattr(drive_source, "download_folder", _boom)

    with pytest.raises(HTTPException) as exc:
        _sync()
    assert exc.value.status_code == 502
    assert library.read_bytes() == b"the only copy", "the tree was deleted for a sync that failed"


def test_a_sync_that_downloads_nothing_leaves_the_existing_tree_intact(library, monkeypatch):
    """The 404 branch is the same hazard: an empty/unshared folder must not be
    read as "the brand has no references any more"."""
    monkeypatch.setattr(
        drive_source, "download_folder",
        lambda fid, dest, **kw: {"brand": "b", "downloaded": 0,
                                 "skipped_folders": ["Unmapped"], "by_type": {}},
    )
    with pytest.raises(HTTPException) as exc:
        _sync()
    assert exc.value.status_code == 404
    assert library.read_bytes() == b"the only copy"


def test_a_successful_sync_replaces_the_tree(library, monkeypatch, tmp_path):
    """The happy path still fully replaces the brand's subtree, and leaves no
    staging directory behind for ingest_all to mistake for a brand."""
    def _download(fid, dest, **kwargs):
        target = dest / BRAND_SEG / "social_ad" / "fresh.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"new")
        return {"brand": kwargs.get("brand_name"), "downloaded": 1,
                "skipped_folders": [], "by_type": {"social_ad": 1}}

    monkeypatch.setattr(drive_source, "download_folder", _download)

    out = _sync()
    assert out["downloaded"] == 1
    assert not library.exists()  # superseded
    assert (tmp_path / BRAND_SEG / "social_ad" / "fresh.png").read_bytes() == b"new"
    assert list(tmp_path.glob(".sync-*")) == []
