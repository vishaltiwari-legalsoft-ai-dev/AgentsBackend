import pytest


@pytest.fixture(autouse=True)
def _offline_by_default(monkeypatch):
    """No MR unit test may touch real Firestore.

    settings.gcp_project_id is populated from the developer's .env, so any store
    whose cloud gate reads it will happily connect to PROD from a test run — a
    targets test once wrote its fixture value into the live mr_config/targets doc
    that the whole desk reads. Default every test offline; tests that exercise a
    cloud path opt back in explicitly, either by monkeypatching the module's
    _use_cloud/_doc or by delenv-ing MR_OFFLINE (both override this fixture).
    """
    monkeypatch.setenv("MR_OFFLINE", "1")
