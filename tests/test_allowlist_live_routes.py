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
from app.routers.auth import is_allowed_email
from app.security import (
    create_token, get_current_user, require_admin, require_creator,
    require_geo_editor,
)

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
    # Blanked like the other two: this file asserts what the guards do, so the
    # six real GEO editors shipped as the default must not silently satisfy a
    # role check a test meant to see refused.
    monkeypatch.setattr(settings, "geo_editor_emails", "")
    # And the scope list for the same reason, in the other direction: the eight
    # addresses shipped as the default must not silently REFUSE a sweep that
    # expects a route to be served. None of the identities below is on that
    # list today, which is exactly the kind of thing that stops being true
    # quietly. The scope itself is proven in
    # ``app/routers/tests/test_route_tenancy_conformance.py``.
    monkeypatch.setattr(settings, "geo_only_emails", "")
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

    for guard in (require_admin, require_creator, require_geo_editor):
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


# --------------------------------------------------------------------------- #
# The GEO editor role — same three properties as the two flags above, asserted
# on the same terms. It is the newest role and the one most likely to be
# reached for next, so pinning it here rather than trusting the pattern held is
# the cheap half of the work.
# --------------------------------------------------------------------------- #

GEO_EDITOR_ROUTE = "/api/geo/brands/legalsoft/rescan"


def _rescan(client, headers):
    return client.post(GEO_EDITOR_ROUTE, headers=headers, json={"days": 7})


def test_revoking_a_geo_editor_takes_effect_on_the_next_request(client, monkeypatch):
    monkeypatch.setattr(settings, "geo_editor_emails", "editor@legalsoft.com")
    token = create_token("u-ed", "editor@legalsoft.com")
    headers = {"Authorization": f"Bearer {token}"}
    assert _rescan(client, headers).status_code != 403

    monkeypatch.setattr(settings, "geo_editor_emails", "")   # role revoked
    assert _rescan(client, headers).status_code == 403, (
        "a revoked GEO editor still holds the role for the life of the token"
    )


def test_granting_the_geo_editor_role_needs_no_new_sign_in(client, monkeypatch):
    monkeypatch.setattr(settings, "geo_editor_emails", "")
    token = create_token("u-ed", "editor@legalsoft.com")
    headers = {"Authorization": f"Bearer {token}"}
    assert _rescan(client, headers).status_code == 403

    monkeypatch.setattr(settings, "geo_editor_emails", "editor@legalsoft.com")
    assert _rescan(client, headers).status_code != 403


def test_a_forged_geo_editor_claim_is_ignored(client, monkeypatch):
    """``create_token`` stamps no ``geo_editor`` claim, and nothing reads one.

    Asserted rather than assumed: a token carrying the claim anyway — which is
    what a future "just add it to the JWT like the others" change would mint —
    must buy nothing, because the flag is derived from config per request.
    """
    import jwt as pyjwt

    monkeypatch.setattr(settings, "geo_editor_emails", "")
    monkeypatch.setattr(settings, "creator_emails", "")
    forged = pyjwt.encode(
        {"sub": "u-1", "email": "nobody@legalsoft.com", "geo_editor": True,
         "is_geo_editor": True, "creator": True,
         "exp": int(time.time()) + 3600},
        SECRET, algorithm="HS256",
    )
    assert _rescan(client, {"Authorization": f"Bearer {forged}"}).status_code == 403


def test_a_geo_editor_gets_no_creator_or_admin_reach(client, monkeypatch):
    """The role's entire justification: it is NOT a step onto the admin ladder.

    Six people needed to administer one agent. Making them Creators would have
    handed over Settings → Secrets, the admin database viewer, model config and
    every other agent — so if the narrow role carried any of that, the change
    would have bought nothing and cost the same.
    """
    monkeypatch.setattr(settings, "geo_editor_emails", "editor@legalsoft.com")
    monkeypatch.setattr(settings, "creator_emails", "")
    monkeypatch.setattr(settings, "admin_emails", "")
    headers = {"Authorization": f"Bearer {create_token('u-ed', 'editor@legalsoft.com')}"}

    assert _rescan(client, headers).status_code != 403        # its own agent: yes
    for path in ("/api/admin/settings", "/api/admin/users", "/api/admin/analytics",
                 "/api/admin/db/collections", "/api/cron/jobs"):
        assert client.get(path, headers=headers).status_code == 403, path


# --------------------------------------------------------------------------- #
# The GEO-only SCOPE. The role tests above are about what an account may ADD;
# these are about what it may not reach at all, which is the opposite direction
# and a different failure mode. Route-by-route proof of the wall lives in
# ``app/routers/tests/test_route_tenancy_conformance.py`` — over the whole
# ledger, because five sampled admin routes is what let /api/mr/* through for
# two months. What belongs HERE is the same three properties every other role
# in this file is held to: revocable now, not mintable, and behind the door.
# --------------------------------------------------------------------------- #

#: Reached by the GEO-only journey; used as the "still works" probe.
IN_SCOPE = "/api/geo/config"
#: Not reached by it, and the exposure that prompted the change.
OUT_OF_SCOPE = "/api/mr/workbook"


def _mr(client, headers):
    return client.get(OUT_OF_SCOPE, headers=headers)


def test_scoping_an_account_down_takes_effect_on_the_next_request(client, monkeypatch):
    monkeypatch.setattr(settings, "geo_only_emails", "")
    headers = {"Authorization": f"Bearer {create_token('u-ext', 'ext@legalsoft.com')}"}
    assert _mr(client, headers).status_code != 403

    monkeypatch.setattr(settings, "geo_only_emails", "ext@legalsoft.com")
    assert _mr(client, headers).status_code == 403, (
        "an account scoped to GEO still reaches the marketing workbook for the "
        "life of its token"
    )
    # and the workspace they WERE brought in for is untouched
    assert client.get(IN_SCOPE, headers=headers).status_code == 200


def test_widening_an_account_back_out_needs_no_new_sign_in(client, monkeypatch):
    monkeypatch.setattr(settings, "geo_only_emails", "ext@legalsoft.com")
    headers = {"Authorization": f"Bearer {create_token('u-ext', 'ext@legalsoft.com')}"}
    assert _mr(client, headers).status_code == 403

    monkeypatch.setattr(settings, "geo_only_emails", "")
    assert _mr(client, headers).status_code != 403


def test_a_forged_scope_claim_is_ignored(client, monkeypatch):
    """``create_token`` stamps no scope, and nothing reads one.

    The interesting forgery here is the *negative* one — a token asserting it is
    NOT geo-only — because that is the claim that would buy reach. Derived from
    config per request, so it buys nothing.
    """
    import jwt as pyjwt

    monkeypatch.setattr(settings, "geo_only_emails", "ext@legalsoft.com")
    forged = pyjwt.encode(
        {"sub": "u-ext", "email": "ext@legalsoft.com", "is_geo_only": False,
         "geo_only": False, "scope": "*", "exp": int(time.time()) + 3600},
        SECRET, algorithm="HS256",
    )
    assert _mr(client, {"Authorization": f"Bearer {forged}"}).status_code == 403


def test_a_creator_or_admin_is_never_scoped_down_by_the_list(client, monkeypatch):
    """A typo in GEO_ONLY_EMAILS must not lock an owner out of their own panel.

    The exemption is unconditional and checked before the list lookup
    (``security.is_geo_only``), so being named there is simply inert for the two
    roles that administer the deployment.
    """
    monkeypatch.setattr(settings, "geo_only_emails", "boss@legalsoft.com")
    monkeypatch.setattr(settings, "creator_emails", "boss@legalsoft.com")
    headers = {"Authorization": f"Bearer {create_token('u-boss', 'boss@legalsoft.com')}"}
    assert _mr(client, headers).status_code != 403

    monkeypatch.setattr(settings, "creator_emails", "")
    monkeypatch.setattr(settings, "admin_emails", "boss@legalsoft.com")
    assert _mr(client, headers).status_code != 403

    # …and with neither role the same address IS scoped, so the test above is
    # about the exemption rather than about a list that never applied.
    monkeypatch.setattr(settings, "admin_emails", "")
    assert _mr(client, headers).status_code == 403


def test_a_de_provisioned_geo_only_account_is_refused_at_the_door_first(client, monkeypatch):
    """Membership outranks scope, and says so with 401 rather than 403.

    Four of the eight are on outside domains, so off-boarding one is a removal
    from ALLOWED_EMAILS — and it has to lock them out entirely, not merely keep
    them inside the GEO panel. 401 is also what the console's session handler
    reads as "sign out", which is the right end state for a token that should
    no longer exist.
    """
    monkeypatch.setattr(settings, "geo_only_emails", "outsider@aivirtual.com")
    monkeypatch.setattr(settings, "allowed_emails", "outsider@aivirtual.com")
    headers = {"Authorization": f"Bearer {create_token('u-x', 'outsider@aivirtual.com')}"}
    assert client.get(IN_SCOPE, headers=headers).status_code == 200
    assert _mr(client, headers).status_code == 403

    monkeypatch.setattr(settings, "allowed_emails", "")   # off-boarded
    assert client.get(IN_SCOPE, headers=headers).status_code == 401
    assert _mr(client, headers).status_code == 401


#: The eight people the scope was built for, by full address. Four are
#: @legalsoft.com — the domain ALLOWED_EMAIL_DOMAINS admits wholesale — which is
#: why the list is addresses and never domains: a domain rule would have scoped
#: the entire company to the GEO panel.
THE_EIGHT = (
    "nino.b@legalsoft.com",
    "marian.p@legalsoft.com",
    "mahmoud.e@legalsoft.com",
    "michael.tayco@legalsoft.com",
    "lynie.t@aivirtual.com",
    "miguel@usimmigration.ai",
    "yans.suarez@medvirtual.ai",
    "franceska@aianswering.ai",
)


def test_the_eight_are_scoped_by_the_shipped_default_with_no_env_change(monkeypatch):
    """Read off the CLASS default, not off ``settings``.

    A deployment that never sets GEO_ONLY_EMAILS must still scope these eight,
    because "we will set the env var" is the step that gets skipped — and the
    skip is silent and fails open. ``model_fields`` is the value baked into the
    image, so a developer's ``.env`` cannot make this pass.
    """
    from app.config import Settings
    from app.security import is_geo_only

    # The sign-in lists come from the same place and for the same reason: the
    # module fixture blanks them to assert guards in isolation, and this one
    # test is specifically about what ships.
    for field in ("geo_only_emails", "allowed_emails", "allowed_email_domains"):
        monkeypatch.setattr(
            settings, field, str(Settings.model_fields[field].default)
        )
    monkeypatch.setattr(settings, "creator_emails", "")
    monkeypatch.setattr(settings, "admin_emails", "")

    for email in THE_EIGHT:
        assert is_geo_only(email), f"{email} is not scoped by the shipped default"
        # …and the scope is not a substitute for the sign-in allowlist: every one
        # of them must still be admitted at the door, or they are simply locked
        # out and "GEO panel only" means "nothing at all".
        assert is_allowed_email(email), f"{email} cannot sign in at all"

    # Non-vacuity, and the boundary: a colleague on the same domain as four of
    # them keeps the whole workspace.
    assert not is_geo_only("colleague@legalsoft.com")


def test_the_scope_wall_does_not_authenticate_anything(client, monkeypatch):
    """It is attached to every router, public ones included, so the thing to
    prove is that it made nothing MORE restrictive for anonymous callers.

    ``deny_outside_geo`` resolves its principal through ``optional_principal``,
    which returns None rather than raising, precisely so that the sign-in door
    and the liveness probe stay reachable without a token.
    """
    monkeypatch.setattr(settings, "geo_only_emails", "ext@legalsoft.com")
    assert client.get("/api/health").status_code == 200
    # the door still answers on its own terms (422 for a malformed body), not 401
    assert client.post("/api/auth/google", json={}).status_code == 422
    # and an unusable token is still the route's own 401, never a scope 403
    assert client.get(IN_SCOPE, headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401


# --------------------------------------------------------------------------- #
# The schema itself — the one "route" the ledger structurally cannot see.
#
# ``/openapi.json``, ``/docs`` and ``/redoc`` are not ``APIRoute`` objects, so
# ``test_route_tenancy_conformance._live_routes`` skips them and the audience
# wall in ``app/scopes.py`` never sees them either: they are mounted by FastAPI
# itself, outside every router. They were live and completely unauthenticated in
# production — a plain GET returned 162 paths with every parameter and response
# model, to anyone, on a service Cloud Run serves --allow-unauthenticated.
#
# So they are pinned here instead, where ``_PUBLIC_PATHS`` above already lists
# them: on the decision, which is pure and testable, and on the app that came
# out of it.
# --------------------------------------------------------------------------- #

def test_the_docs_are_off_everywhere_but_local_development():
    from app.main import doc_urls

    for env in ("production", "staging", "", "Development", "dev"):
        assert doc_urls(env) == {"docs_url": None, "redoc_url": None, "openapi_url": None}, env
    assert doc_urls("development") == {
        "docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json",
    }


def test_the_live_app_took_that_decision():
    """The decision applied, not just available.

    Asserted as a correspondence rather than a fixed answer because the suite
    runs in both worlds: a developer's ``.env`` carries APP_ENV=development and
    CI has none, which defaults ``app_env`` to production. Pinning "off" would
    be green on the machine that matters least. What must hold in both is that
    the app agrees with ``doc_urls`` for the environment it actually booted in.
    """
    from app.main import app as live_app, doc_urls

    expected = doc_urls(settings.app_env)
    assert {
        "docs_url": live_app.docs_url,
        "redoc_url": live_app.redoc_url,
        "openapi_url": live_app.openapi_url,
    } == expected
    if settings.app_env != "development":
        # and the routes are genuinely gone, not merely unlinked
        served = {getattr(r, "path", None) for r in live_app.routes}
        assert not served & {"/openapi.json", "/docs", "/redoc"}


def test_a_de_provisioned_geo_editor_is_refused_at_the_allowlist_first(client, monkeypatch):
    """Role and membership are separate revocations, and membership wins.

    One of the six is on an outside domain. Dropping them from ALLOWED_EMAILS
    must lock them out entirely — 401, at the door — even while their address
    is still sitting in GEO_EDITOR_EMAILS, which is the state a half-finished
    off-boarding leaves behind.
    """
    monkeypatch.setattr(settings, "geo_editor_emails", "outsider@aivirtual.com")
    monkeypatch.setattr(settings, "allowed_emails", "outsider@aivirtual.com")
    headers = {"Authorization": f"Bearer {create_token('u-x', 'outsider@aivirtual.com')}"}
    assert _rescan(client, headers).status_code != 403

    monkeypatch.setattr(settings, "allowed_emails", "")   # off-boarded
    assert _rescan(client, headers).status_code == 401
