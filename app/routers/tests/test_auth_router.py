"""Sign-in allowlist tests for /api/auth/google. Fully offline.

The backend is deployed --allow-unauthenticated, so this endpoint is the only
membership check in the system: a valid Google token proves who you are, never
that you belong here. Everything below pins that boundary — including the case
that matters most, an *unconfigured* allowlist rejecting rather than admitting.

Only the outer seams are faked: Google token verification and the Firestore
user upsert. The router, the settings properties and the gate itself run real.
"""
from __future__ import annotations

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app as fastapi_app
from app.routers import auth
from app.services import firestore_repo

client = TestClient(fastapi_app)

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


def test_allowlist_defaults_are_closed(monkeypatch):
    defaults = _pristine_settings(monkeypatch)
    assert defaults.allowed_email_domain_set == {"legalsoft.com"}
    assert defaults.allowed_email_set == set()


def test_app_env_defaults_to_production(monkeypatch):
    """APP_ENV is set in no deploy config, so the default is what prod actually
    runs — and it must be the one that hides raw exception text."""
    assert _pristine_settings(monkeypatch).app_env == "production"
