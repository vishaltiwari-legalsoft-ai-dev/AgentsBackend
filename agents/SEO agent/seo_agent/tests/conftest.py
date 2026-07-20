"""Offline guard: SEO tests must never touch Firestore/GCS or the network."""
import os
import pytest


@pytest.fixture(autouse=True)
def _seo_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_RUNS_DIR", str(tmp_path / "seo_runs"))
