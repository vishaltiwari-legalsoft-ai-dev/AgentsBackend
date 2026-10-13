"""GA4 and Search Console clients must carry a stated deadline.

Both run in sync handlers on anyio's 40-slot worker threadpool, and both are
slow by nature (a GSC query pulls up to 5000 rows, a GA overview batches three
reports) — which is exactly why "slow" must not be allowed to become "forever".

The offline guard stays on: ``state.use_cloud`` is monkeypatched at its module
seam, as this repo's conftest prescribes, and ADC is stubbed. No socket is
opened — ``build`` reads the discovery document bundled inside
google-api-python-client.
"""
from __future__ import annotations

import pytest

from seo_geo_agent import sources, state


class _FakeCreds:
    valid = True
    token = "fake-token"


@pytest.fixture()
def cloud(monkeypatch):
    """Pretend we are in cloud mode, with ADC resolved to a fake."""
    import google.auth

    granted: list[tuple] = []

    def fake_default(scopes=None):
        granted.append(tuple(scopes or ()))
        return _FakeCreds(), "test-project"

    monkeypatch.setattr(google.auth, "default", fake_default)
    monkeypatch.setattr(state, "use_cloud", lambda: True)
    monkeypatch.setattr(sources.state, "use_cloud", lambda: True)
    return granted


def test_analytics_data_client_carries_a_timeout(cloud):
    svc = sources._ga_service("analyticsdata")
    assert svc._http.http.timeout == sources.GOOGLE_API_TIMEOUT_SECONDS


def test_analytics_admin_client_carries_a_timeout(cloud):
    svc = sources._ga_service("analyticsadmin")
    assert svc._http.http.timeout == sources.GOOGLE_API_TIMEOUT_SECONDS


def test_search_console_client_carries_a_timeout(cloud):
    svc = sources._gsc_service()
    assert svc._http.http.timeout == sources.GOOGLE_API_TIMEOUT_SECONDS


def test_timeout_is_finite_and_sane():
    assert 0 < sources.GOOGLE_API_TIMEOUT_SECONDS <= 120


def test_each_client_gets_its_own_transport(cloud):
    """httplib2 is not thread-safe and these run on worker threads."""
    a, b = sources._gsc_service(), sources._gsc_service()
    assert a._http.http is not b._http.http


def test_clients_ask_only_for_read_scopes(cloud):
    """Supplying our own transport means scopes are named here rather than
    derived from the discovery document — which previously handed this
    read-only module the *write* scopes (analytics.edit, webmasters) too."""
    sources._ga_service("analyticsdata")
    sources._gsc_service()
    requested = {scope for call in cloud for scope in call}
    assert requested == {sources.GA_READONLY_SCOPE, sources.GSC_READONLY_SCOPE}
    assert all("readonly" in scope for scope in requested)


def test_offline_mode_still_refuses_before_building_anything(monkeypatch):
    """The guard is untouched by the timeout work."""
    monkeypatch.setattr(sources.state, "use_cloud", lambda: False)
    with pytest.raises(sources.CredentialMissing):
        sources._ga_service("analyticsdata")
    with pytest.raises(sources.CredentialMissing):
        sources._gsc_service()


def test_auth_failure_still_degrades_rather_than_crashing(monkeypatch):
    """A build failure must surface as CredentialMissing, which the pipeline
    turns into a plain-language degradation note."""
    import google.auth

    monkeypatch.setattr(sources.state, "use_cloud", lambda: True)
    monkeypatch.setattr(
        google.auth, "default",
        lambda scopes=None: (_ for _ in ()).throw(RuntimeError("no ADC here")),
    )
    with pytest.raises(sources.CredentialMissing) as excinfo:
        sources._gsc_service()
    assert "no ADC here" in str(excinfo.value)


# ----------------- the brand's own Search Console grant -----------------

def test_a_connected_brands_client_carries_a_timeout_and_its_own_token(gsc_connect):
    """``gsc_oauth.service`` was the one Google client built with
    ``credentials=`` and no transport — the token refresh and every Search
    Console query on a connected brand could hang a worker thread. It now
    rides a timed ``http=``, opened from the SEALED grant."""
    from seo_geo_agent import gsc_oauth

    sealed = gsc_connect.fernet.encrypt(b"r1").decode()
    gsc_connect.store["b"] = {"property": "sc-domain:x.com", "refresh_token_enc": sealed}

    svc = gsc_oauth.service("b")
    assert svc._http.http.timeout == sources.GOOGLE_API_TIMEOUT_SECONDS
    assert svc._http.credentials.refresh_token == "r1"
    assert svc._http.credentials.scopes == [gsc_oauth.SCOPE]
    assert gsc_oauth.service("no-grant") is None  # the run falls back to the service account


def test_a_legacy_plaintext_grant_still_builds_a_bounded_client(gsc_connect):
    from seo_geo_agent import gsc_oauth

    gsc_connect.store["b"] = {"property": "sc-domain:x.com", "refresh_token": "old", "legacy": True}
    svc = gsc_oauth.service("b")
    assert svc._http.http.timeout == sources.GOOGLE_API_TIMEOUT_SECONDS
    assert svc._http.credentials.refresh_token == "old"
