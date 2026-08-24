"""The allowlist, asserted against the real application instead of a stand-in.

``tests/test_security_token_allowlist.py`` pins ``get_current_user`` behind a
minimal, purpose-built FastAPI app — deliberately, because in wave 1 the real
routers were being edited underneath it. That is no longer true, and a
dependency proven in isolation is not the same claim as "every authenticated
route in this service enforces it". A router added with its own auth shim, or a
``require_*`` guard that stops delegating, reopens the hole with that file still
green.

So this sweeps the real ``app.main.app``: no dependency override, real
``get_current_user``, real routers, real JWTs. A rejected token is refused
inside the dependency, before any handler body runs, so sweeping every route
costs nothing and touches no service.
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("MR_OFFLINE", "1")
os.environ.setdefault("SEO_OFFLINE", "1")

import app  # noqa: F401 - side effect: registers the agent roots on sys.path
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app as fastapi_app
from app.security import create_token, get_current_user, require_admin, require_creator

SECRET = "test-only-signing-key-" + "0" * 32

# Endpoints that are public by design. Anything else without get_current_user is
# a finding, so this list is short and every entry states why.
_PUBLIC_PATHS = {
    "/",                     # service banner: name + doc links, no data
    "/api/health",           # Cloud Run liveness — must answer without a token
    "/api/auth/google",      # the sign-in door itself
    # OAuth redirects: the provider drives the *browser* here, so there is no
    # Authorization header to read. Both are gated on a signed `state` instead
    # (seo_oauth.read_state / canva state), which is the standard substitute.
    "/api/seo-geo/oauth/callback",
    "/api/canva/callback",
    "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
}


@pytest.fixture(autouse=True)
def _no_egress(monkeypatch):
    """Hard stop on outbound network for this file.

    The allowlisted-token sweep below calls every authenticated GET, and some of
    them (``/api/mr/workbook``, ``/api/mr/overview``) read the live Google
    workbook. ``MR_OFFLINE`` now does gate the Sheets export fetchers (see
    tests/test_offline_guard_coverage.py), but this stays: it also covers the
    Drive and Sheets-API seams, and a second lock on egress costs nothing. A
    blocked route answers 502; this file only ever asserts "not 401", so that is
    the correct outcome and not a masked failure.
    """
    import httpx

    import google.auth
    from app.services import drive_source
    from marketing_research_agent.sources import sheets_source as ss

    def _blocked(*args, **kwargs):
        raise AssertionError("outbound network attempted from a test")

    for module, name in (
        (httpx, "get"), (httpx, "post"), (httpx, "request"), (httpx, "stream"),
        (google.auth, "default"),
        (ss, "_sheets_service"), (ss, "_default_fetcher"), (ss, "_default_xlsx_fetcher"),
        (drive_source, "build_drive_service"),
    ):
        monkeypatch.setattr(module, name, _blocked, raising=False)


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    monkeypatch.setattr(settings, "allowed_email_domains", "legalsoft.com")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "creator_emails", "")
    monkeypatch.setattr(settings, "admin_emails", "")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("SEO_LOCAL_DIR", str(tmp_path / "seo"))
    # No dependency override: enforcing the real dependency is the point.
    assert get_current_user not in fastapi_app.dependency_overrides


@pytest.fixture()
def client():
    return TestClient(fastapi_app, raise_server_exceptions=False)


def _flat_dependency_calls(dependant) -> set:
    calls = {dependant.call} if dependant.call else set()
    for sub in dependant.dependencies:
        calls |= _flat_dependency_calls(sub)
    return calls


def _authenticated_get_routes() -> list[APIRoute]:
    """Every parameterless GET that runs ``get_current_user``."""
    return [
        route for route in fastapi_app.routes
        if isinstance(route, APIRoute)
        and "GET" in route.methods
        and "{" not in route.path
        and get_current_user in _flat_dependency_calls(route.dependant)
    ]


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #

def test_the_sweep_actually_covers_the_service(client):
    """Non-vacuity, and the shape of the assertion below: if route discovery
    silently returned [], every allowlist claim here would pass on nothing."""
    paths = {route.path for route in _authenticated_get_routes()}
    assert len(paths) >= 20, sorted(paths)
    # One from each agent's router, so a whole router dropping out is visible.
    for expected in ("/api/mr/runs", "/api/geo/config", "/api/seo-geo/overview"):
        assert expected in paths, sorted(paths)


def test_no_route_on_the_real_app_honours_a_non_allowlisted_token(client):
    """A cryptographically valid token for a de-provisioned account is refused
    everywhere, not just at the door it was issued from."""
    token = create_token("u-stranger", "randomperson@gmail.com")
    headers = {"Authorization": f"Bearer {token}"}
    leaks = [
        route.path for route in _authenticated_get_routes()
        if client.get(route.path, headers=headers).status_code != 401
    ]
    assert leaks == [], f"non-allowlisted token was honoured on: {sorted(leaks)}"


def test_an_allowlisted_token_is_not_refused_by_the_same_sweep(client):
    """The other half — otherwise "reject everything" would pass the test above.

    Only 401 is checked: an offline handler may legitimately 4xx/5xx for its own
    reasons, but it must not report the caller as unauthenticated.
    """
    token = create_token("u-colleague", "colleague@legalsoft.com")
    headers = {"Authorization": f"Bearer {token}"}
    refused = [
        route.path for route in _authenticated_get_routes()
        if client.get(route.path, headers=headers).status_code == 401
    ]
    assert refused == [], f"a valid colleague was locked out of: {sorted(refused)}"


def test_removing_the_domain_locks_an_issued_token_out_of_every_route(client, monkeypatch):
    """Revocation must take effect on the next request, not in seven days."""
    token = create_token("u-colleague", "colleague@legalsoft.com")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/geo/config", headers=headers).status_code == 200

    monkeypatch.setattr(settings, "allowed_email_domains", "")
    still_in = [
        route.path for route in _authenticated_get_routes()
        if client.get(route.path, headers=headers).status_code != 401
    ]
    assert still_in == [], f"revoked account still served on: {sorted(still_in)}"


def test_every_route_is_either_authenticated_or_deliberately_public():
    """The structural law. A new router mounted without ``get_current_user``
    is not caught by any behavioural test — nothing knows to go look for it."""
    unguarded = {
        route.path for route in fastapi_app.routes
        if isinstance(route, APIRoute)
        and get_current_user not in _flat_dependency_calls(route.dependant)
        and route.path not in _PUBLIC_PATHS
    }
    # Cron endpoints authenticate with a shared key header instead of a JWT.
    unguarded = {path for path in unguarded if "/cron/" not in path}
    assert unguarded == set(), (
        "routes reachable without the allowlist — add Depends(get_current_user) "
        f"or justify them in _PUBLIC_PATHS: {sorted(unguarded)}"
    )


def test_the_role_guards_delegate_rather_than_re_implement(client):
    """``require_admin`` / ``require_creator`` must sit *on top of*
    ``get_current_user``. A guard that decoded the JWT itself would skip the
    per-request allowlist check and hand an escalated route to a revoked
    account — the exact bug wave 1 closed, one layer up."""
    import inspect

    for guard in (require_admin, require_creator):
        defaults = [
            param.default for param in inspect.signature(guard).parameters.values()
        ]
        depends_on = {getattr(d, "dependency", None) for d in defaults}
        assert get_current_user in depends_on, f"{guard.__name__} does not delegate"

    # And the same statement observed rather than introspected: a non-allowlisted
    # token is refused with 401 (not 403) on a Creator-only route, i.e. the
    # allowlist ran before the role check.
    token = create_token("u-stranger", "randomperson@gmail.com")
    resp = client.post(
        "/api/geo/brands/legalsoft/prompts/generate",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401, resp.text


def test_a_missing_or_junk_token_is_refused_on_the_real_routes(client):
    for headers in ({}, {"Authorization": "Bearer not-a-jwt"}, {"Authorization": "Bearer "}):
        assert client.get("/api/mr/runs", headers=headers).status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Was the same bug class, one field over — now fixed and pinned.
#
# `is_admin` / `is_creator` were stamped into the JWT at mint time and read back
# out of the payload by get_current_user, while the allowlist beside them was
# re-evaluated per request. Both are set-membership lookups on config, so there
# was no cost argument for the difference. Consequence: removing an address from
# CREATOR_EMAILS had no effect for up to jwt_expires_minutes (7 days), during
# which that token still passed require_creator — i.e. kept write access to
# Settings → Secrets.
#
# get_current_user now re-derives both flags from the email claim, beside the
# existing _still_allowed call. Was a strict xfail recording the defect; the
# ruling was to fix it, so these are plain assertions.
# --------------------------------------------------------------------------- #

def test_revoking_creator_takes_effect_on_the_next_request(client, monkeypatch):
    monkeypatch.setattr(settings, "creator_emails", "boss@legalsoft.com")
    token = create_token("u-boss", "boss@legalsoft.com")
    headers = {"Authorization": f"Bearer {token}"}
    # Creator-only route: reachable while the role holds.
    assert client.get("/api/admin/settings", headers=headers).status_code != 403

    monkeypatch.setattr(settings, "creator_emails", "")  # role revoked
    assert client.get("/api/admin/settings", headers=headers).status_code == 403, (
        "a revoked Creator still holds Creator rights for the life of the token"
    )


def test_granting_creator_takes_effect_without_a_new_sign_in(client, monkeypatch):
    """The other direction: the claim in the payload is not authoritative, so a
    token minted before the grant picks the role up on the next request."""
    monkeypatch.setattr(settings, "creator_emails", "")
    token = create_token("u-boss", "boss@legalsoft.com")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/admin/settings", headers=headers).status_code == 403

    monkeypatch.setattr(settings, "creator_emails", "boss@legalsoft.com")
    assert client.get("/api/admin/settings", headers=headers).status_code != 403


def test_a_forged_role_claim_is_ignored(client, monkeypatch):
    """A token is only as good as its signature — but the signing key is what a
    compromised or mis-scoped minting path would already hold. Roles now come
    from config, so a payload claiming creator=true buys nothing."""
    import jwt as pyjwt

    monkeypatch.setattr(settings, "creator_emails", "")
    monkeypatch.setattr(settings, "admin_emails", "")
    forged = pyjwt.encode(
        {"sub": "u-1", "email": "nobody@legalsoft.com", "admin": True,
         "creator": True, "exp": int(time.time()) + 3600},
        SECRET, algorithm="HS256",
    )
    headers = {"Authorization": f"Bearer {forged}"}
    assert client.get("/api/admin/settings", headers=headers).status_code == 403
