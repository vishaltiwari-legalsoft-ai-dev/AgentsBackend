# backend/conftest.py
"""Repo-root test guards — apply to EVERY test in this repo.

Five protections, born from real incidents. The MR suite's conftest records the
first one (a targets test wrote its fixture into the live ``mr_config/targets``
doc); the GD suite supplied the second (``test_mirror_to_gcs_noop_without_gcs``
asserts "no GCS in tests" but was uploading its fixtures to the live
reference-library bucket, because nothing stopped it):

1. The agent offline flags default ON for the whole suite, so suites without
   their own conftest (e.g. ``app/routers/tests``) can never talk to prod.
2. ``firestore_repo._db`` is replaced with a loud failure. Tests that
   legitimately exercise Firestore paths already monkeypatch ``_db`` (or a
   higher-level function) themselves — that override wins over this fixture.
   Best-effort writers (usage events, run tracking) swallow the error by
   design, exactly as they would offline.
3. The OpenRouter key reads empty, so every LLM path takes the documented "no
   key configured" fallback instead of spending real credits — and real network
   latency — on whoever happens to run the suite.
4. Cloud Storage reports itself unconfigured, so nothing mirrors to the live
   bucket.
5. The DataForSEO credential reads empty, so the GEO agent's two Google SERP
   engines report themselves unconfigured instead of buying live, billed SERP
   calls.

3, 4 and 5 follow the same rule as 2: a test that legitimately exercises those
paths monkeypatches its own module-level seam (``suggestions._get_llm``,
``qa_brain._analyze``, ``storage.is_configured``, …) and that override wins.
"""
import os

import pytest

os.environ.setdefault("MR_OFFLINE", "1")
os.environ.setdefault("SEO_OFFLINE", "1")
os.environ.setdefault("BLOG_OFFLINE", "1")

# Which agent each test belongs to, derived from its path. Keep the fragments
# lowercase — they are matched against a lowercased, forward-slashed path.
_AGENT_MARKERS = (
    ("agents/graphics designer agent", "gd"),
    ("agents/marketing research agent", "mr"),
    ("agents/seo geo agent", "seo"),
    ("agents/final geo agent", "geo"),
    ("agents/blog writer agent", "blog"),
)


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


@pytest.fixture(autouse=True)
def _no_live_openrouter(monkeypatch):
    """The OpenRouter key reads empty, which is the whole guard.

    Every network entry point in ``app.services.openrouter`` resolves the key
    through ``runtime_config.require`` first, so a blank key turns all of them
    into the raise that callers already expect ("any raise means the LLM path is
    unavailable" — ``suggestions._get_llm``). Callers that ask permission first
    (``providers._openrouter_key_configured``) see False and take their curated
    branch. One mechanism covers both shapes.

    Deliberately *not* stubbing ``get_llm`` itself: building a client opens no
    socket, and tests that pin per-agent model resolution supply their own key
    via a mocked ``app_config`` — that override still wins, exactly as with the
    Firestore guard above.
    """
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    try:
        from app.config import settings
    except ImportError:
        yield
        return

    monkeypatch.setattr(settings, "openrouter_api_key", "", raising=False)
    yield


@pytest.fixture(autouse=True)
def _no_live_dataforseo(monkeypatch):
    """Both halves of the DataForSEO credential read empty — half a Basic-auth
    credential is no credential, so the SERP engines go honestly "off".

    Guard #3's shape, and it exists for guard #3's reason. On 2026-09-04 the
    credential moved from ``os.environ`` (which only ``start-backend.ps1``
    populates) to a real ``Settings`` field, so an admin could rotate it
    without a redeploy — which also meant pydantic read it straight out of a
    developer's ``.env`` for every test. ``app/routers/tests`` has no conftest
    of its own and immediately made two live, billed calls to
    api.dataforseo.com on a plain suite run.

    A test that legitimately exercises the adapter stubs
    ``geo_engines.dataforseo_creds`` or ``httpx`` itself; that override wins.
    """
    for var in ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    try:
        from app.config import settings
    except ImportError:
        yield
        return

    monkeypatch.setattr(settings, "dataforseo_login", "", raising=False)
    monkeypatch.setattr(settings, "dataforseo_password", "", raising=False)
    yield


@pytest.fixture(autouse=True)
def _no_live_gcs(monkeypatch):
    """Cloud Storage reports itself unconfigured, so the mirror/upload paths
    take their documented no-op branch instead of writing to the real bucket."""
    try:
        from app.services import storage
    except ImportError:
        yield
        return

    monkeypatch.setattr(storage, "is_configured", lambda: False)
    yield


def pytest_collection_modifyitems(items):
    """Tag every test with the agent it belongs to, derived from its path.

    Lets a developer run only what they are working on — ``pytest -m seo``
    — or skip the slowest suite while iterating — ``pytest -m "not gd"``.
    Path-derived on purpose: new test files are tagged automatically, so there
    is nothing for anyone to remember to add.
    """
    for item in items:
        path = str(item.fspath).replace("\\", "/").lower()
        for fragment, marker in _AGENT_MARKERS:
            if fragment in path:
                item.add_marker(getattr(pytest.mark, marker))
                break
        else:
            item.add_marker(pytest.mark.core)
