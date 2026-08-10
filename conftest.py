# backend/conftest.py
"""Repo-root test guards — apply to EVERY test in this repo.

Two protections, born from a real incident (see the MR suite's conftest: a
targets test once wrote its fixture into the live ``mr_config/targets`` doc):

1. The agent offline flags default ON for the whole suite, so suites without
   their own conftest (e.g. ``app/routers/tests``) can never talk to prod.
2. ``firestore_repo._db`` is replaced with a loud failure. Tests that
   legitimately exercise Firestore paths already monkeypatch ``_db`` (or a
   higher-level function) themselves — that override wins over this fixture.
   Best-effort writers (usage events, run tracking) swallow the error by
   design, exactly as they would offline.
"""
import os

import pytest

os.environ.setdefault("MR_OFFLINE", "1")
os.environ.setdefault("SEO_OFFLINE", "1")
os.environ.setdefault("BLOG_OFFLINE", "1")
os.environ.setdefault("BROWSER_OFFLINE", "1")


@pytest.fixture(autouse=True)
def _no_prod_firestore(monkeypatch):
    try:
        from app.services import firestore_repo
    except ImportError:
        yield
        return

    def _blocked():
        raise RuntimeError(
            "Firestore access blocked in tests — monkeypatch firestore_repo._db "
            "(or the specific repo function) instead of talking to the live DB."
        )

    monkeypatch.setattr(firestore_repo, "_db", _blocked)
    yield
