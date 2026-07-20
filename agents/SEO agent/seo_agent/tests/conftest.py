"""Offline guard: SEO tests must never touch Firestore/GCS or the network."""
import os
import pytest

from seo_agent import store


@pytest.fixture(autouse=True)
def _seo_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_RUNS_DIR", str(tmp_path / "seo_runs"))
    # The load_config() TTL cache (I5) is module-global; each test gets a fresh
    # SEO_RUNS_DIR, so a cached value from a previous test must not leak in.
    store._config_cache = None
    yield
    store._config_cache = None
