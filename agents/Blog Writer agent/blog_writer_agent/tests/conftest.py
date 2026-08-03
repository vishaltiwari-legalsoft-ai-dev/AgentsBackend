"""Hard offline guard: these tests must never touch prod Firestore or paid APIs."""
from __future__ import annotations

import os
import pathlib
import sys

_AGENTS_DIR = pathlib.Path(__file__).resolve().parents[3]
for _root in (_AGENTS_DIR / "Blog Writer agent", _AGENTS_DIR / "SEO GEO agent"):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

os.environ["BLOG_OFFLINE"] = "1"

import pytest


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOG_OFFLINE", "1")
    monkeypatch.setenv("BLOG_LOCAL_DIR", str(tmp_path / "blog_state"))
    monkeypatch.delenv("SEO_SERPER_API_KEY", raising=False)
