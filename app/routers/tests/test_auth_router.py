"""Sign-in allowlist tests for /api/auth/google. Fully offline.

The backend is deployed --allow-unauthenticated, so this endpoint is the only
membership check in the system: a valid Google token proves who you are, never
that you belong here. Everything below pins that boundary — including the case
that matters most, an *unconfigured* allowlist rejecting rather than admitting.

Only the outer seams are faked: Google token verification and the Firestore
user upsert. The router, the settings properties and the gate itself run real.

Note what this module deliberately does *not* do: install a ``get_current_user``
override. This endpoint is how a caller becomes authenticated, so a pre-authed
caller would make the allowlist untestable. The shared harness in ``conftest.py``
installs a caller only when a suite asks for one, so nothing here is authed —
and its autouse guard still makes sure no sibling suite's override leaks in.
"""
from __future__ import annotations

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest

from app.config import settings
from app.routers import auth
from app.routers.tests.conftest import client
from app.services import firestore_repo

CREDENTIAL = "a-google-id-token-that-the-verifier-will-accept"


@pytest.fixture(autouse=True)
def _harness(monkeypatch):
    """Verified-Google-account harness with a known allowlist.

    ``created`` records every upsert so a test can assert that a refused
    sign-in never reached Firestore.
    """
    created: list[str] = []

    def fake_verify(credential: str) -> dict[str, str]:
        # Stands in for a *successful* verification — audience and
        # email_verified checks live in app.security and are exercised there.
        return {
            "sub": "google-sub-1",
            "email": fake_verify.email,
            "name": "Test User",
            "picture": "",
        }

    fake_verify.email = "someone@legalsoft.com"

    def fake_upsert(*, email: str, name: str, picture: str, google_sub: str) -> dict:
        created.append(email)
        return {"id": "u1", "email": email, "name": name, "picture": picture}

    monkeypatch.setattr(auth, "verify_google_id_token", fake_verify)
    monkeypatch.setattr(firestore_repo, "get_or_create_google_user", fake_upsert)
    monkeypatch.setattr(firestore_repo, "new_session_id", lambda: "sess-1")
    monkeypatch.setattr(settings, "jwt_secret", "test-only-signing-key-" + "0" * 32)
    monkeypatch.setattr(settings, "allowed_email_domains", "legalsoft.com")
    monkeypatch.setattr(settings, "allowed_emails", "")
    yield {"verify": fake_verify, "created": created}


def login(harness, email: str):
    harness["verify"].email = email
    return client.post("/api/auth/google", json={"credential": CREDENTIAL})


def test_allowed_domain_signs_in(_harness):
    resp = login(_harness, "colleague@legalsoft.com")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"]
    assert body["user"]["email"] == "colleague@legalsoft.com"
    assert _harness["created"] == ["colleague@legalsoft.com"]


def test_domain_match_ignores_case_and_padding(_harness, monkeypatch):
    monkeypatch.setattr(settings, "allowed_email_domains", " LegalSoft.com , acme.io ")
    assert login(_harness, "Colleague@LEGALSOFT.com").status_code == 200
    assert login(_harness, "partner@acme.io").status_code == 200


def test_exception_email_signs_in(_harness, monkeypatch):
    monkeypatch.setattr(settings, "allowed_emails", "contractor@outside.dev")
    # Matching is case-insensitive; the stored address keeps Google's casing.
    resp = login(_harness, "Contractor@Outside.dev")
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["email"].lower() == "contractor@outside.dev"


def test_stranger_is_refused_with_403(_harness):
    resp = login(_harness, "randomperson@gmail.com")
    assert resp.status_code == 403
    assert _harness["created"] == []  # no user document for a refused account


def test_refusal_message_does_not_leak_account_state(_harness):
    stranger = login(_harness, "never-seen@gmail.com").json()["detail"]
    other = login(_harness, "also-never-seen@example.org").json()["detail"]
    # Same message for every refusal — a caller cannot tell a de-provisioned
    # colleague from an account we have never heard of.
    assert stranger == other
    assert "never-seen" not in stranger
    assert "gmail" not in stranger.lower()


def test_subdomain_does_not_inherit_the_allowlist(_harness):
    assert login(_harness, "attacker@evil-legalsoft.com").status_code == 403
    assert login(_harness, "attacker@mail.legalsoft.com").status_code == 403


def test_empty_allowlist_fails_closed(_harness, monkeypatch):
    """Unset config must mean "no one", not "everyone"."""
    monkeypatch.setattr(settings, "allowed_email_domains", "")
    monkeypatch.setattr(settings, "allowed_emails", "")
    assert login(_harness, "colleague@legalsoft.com").status_code == 403
    assert login(_harness, "randomperson@gmail.com").status_code == 403
    assert _harness["created"] == []


def test_owner_is_never_locked_out(_harness, monkeypatch):
    """A Creator keeps access even with an empty/hostile allowlist — otherwise a
    bad ALLOWED_EMAIL_DOMAINS deploy locks the project owners out of the panel
    that fixes it."""
    monkeypatch.setattr(settings, "allowed_email_domains", "")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "creator_emails", "owner@elsewhere.test")
    resp = login(_harness, "Owner@Elsewhere.test")
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["is_creator"] is True


def _pristine_settings(monkeypatch):
    """Settings built from the shipped defaults only — no .env, no exported
    variable from whoever is running the suite."""
    from app.config import Settings

    for name in ("APP_ENV", "ALLOWED_EMAIL_DOMAINS", "ALLOWED_EMAILS"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)


def test_the_login_response_tells_the_console_who_is_a_geo_editor(_harness, monkeypatch):
    """Without this the six being onboarded and everyone else see the same
    controls, and a non-editor finds out by pressing a button and getting a 403.

    Asserted for all three roles at once, because the flag only helps if it
    tracks the others: a GEO editor is NOT an admin and NOT a creator, and the
    console renders three different surfaces off exactly these booleans.
    """
    monkeypatch.setattr(settings, "geo_editor_emails", "editor@legalsoft.com")
    monkeypatch.setattr(settings, "creator_emails", "")
    monkeypatch.setattr(settings, "admin_emails", "")

    editor = login(_harness, "editor@legalsoft.com").json()["user"]
    assert editor["is_geo_editor"] is True
    assert editor["is_admin"] is False and editor["is_creator"] is False

    plain = login(_harness, "colleague@legalsoft.com").json()["user"]
    assert plain["is_geo_editor"] is False

    # A Creator is one implicitly, so the console shows them the editor surface
    # without anybody having to list them twice in config.
    monkeypatch.setattr(settings, "creator_emails", "boss@legalsoft.com")
    boss = login(_harness, "boss@legalsoft.com").json()["user"]
    assert boss["is_geo_editor"] is True and boss["is_creator"] is True


def test_the_geo_editor_flag_is_derived_not_carried_in_the_token(_harness, monkeypatch):
    """The display hint must not become a second, staler source of truth.

    ``create_token`` stamps no ``geo_editor`` claim; the login response computes
    it from config at response time, the same as ``is_admin``/``is_creator``.
    Decoding the issued token and finding the role in it would mean a revoked
    editor kept the editor UI for the 7-day life of that token.
    """
    import jwt as pyjwt

    monkeypatch.setattr(settings, "geo_editor_emails", "editor@legalsoft.com")
    body = login(_harness, "editor@legalsoft.com").json()
    assert body["user"]["is_geo_editor"] is True

    claims = pyjwt.decode(body["token"], settings.jwt_secret, algorithms=["HS256"])
    assert "geo_editor" not in claims and "is_geo_editor" not in claims


#: The GEO editors on outside domains, admitted one address at a time.
EXTERNAL_GEO_EDITORS = {
    "lynie.t@aivirtual.com",
    "miguel@usimmigration.ai",
    "yans.suarez@medvirtual.ai",
}


def test_allowlist_defaults_are_closed(monkeypatch):
    defaults = _pristine_settings(monkeypatch)
    assert defaults.allowed_email_domain_set == {"legalsoft.com"}
    # Named individuals, not a fourth domain. The exception list growing is
    # expected — it is the mechanism for contractors and clients — so this
    # pins WHO is on it rather than that it is empty.
    assert defaults.allowed_email_set == EXTERNAL_GEO_EDITORS


def test_the_outside_domains_were_not_admitted_wholesale(monkeypatch):
    """The failure mode this change was one keystroke away from.

    Three GEO editors are at aivirtual.com, usimmigration.ai and medvirtual.ai.
    Adding those three DOMAINS would have been shorter and would have opened
    sign-in to every mailbox at three companies whose accounts nobody here
    provisions or de-provisions — on a service Cloud Run serves
    --allow-unauthenticated, where this list is the only door.
    """
    defaults = _pristine_settings(monkeypatch)
    assert defaults.allowed_email_domain_set == {"legalsoft.com"}
    for address in EXTERNAL_GEO_EDITORS:
        domain = address.rpartition("@")[2]
        assert domain not in defaults.allowed_email_domain_set


def test_a_colleague_of_an_allowlisted_external_editor_cannot_sign_in(
    _harness, monkeypatch,
):
    """The same statement at the door instead of in the config.

    The harness blanks ``allowed_emails``; this restores the SHIPPED default
    (read from a pristine ``Settings``, never retyped here) so the assertion is
    about what the service actually deploys with.
    """
    monkeypatch.setattr(
        settings, "allowed_emails", _pristine_settings(monkeypatch).allowed_emails,
    )
    assert login(_harness, "lynie.t@aivirtual.com").status_code == 200

    for stranger in ("someone.else@aivirtual.com", "ceo@usimmigration.ai",
                     "intern@medvirtual.ai", "billing@aivirtual.com"):
        assert login(_harness, stranger).status_code == 403, stranger
    # Refused before the upsert, so no junk user document either.
    assert _harness["created"] == ["lynie.t@aivirtual.com"]


def test_app_env_defaults_to_production(monkeypatch):
    """APP_ENV is set in no deploy config, so the default is what prod actually
    runs — and it must be the one that hides raw exception text."""
    assert _pristine_settings(monkeypatch).app_env == "production"
