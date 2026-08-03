"""Offline harness: every blog_writer test runs without Firestore or network."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOG_OFFLINE", "1")
    monkeypatch.setenv("BLOG_LOCAL_DIR", str(tmp_path / "blog_state"))
