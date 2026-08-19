"""The allowlist must hold for tokens already in the wild, not just at sign-in.

Wave 1 gated ``POST /api/auth/google``. That protects accounts signing in
*after* the gate and nobody else: a JWT minted before it stays valid for the
rest of its 7-day life, so "revoke access" had no effect for a week. These tests
pin the re-check on every authenticated request, and the two config-failure
modes that used to surface as a 500.

Fully offline — no Google, no Firestore, no network.
"""
from __future__ import annotations

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import security
from app.config import settings
from app.security import create_token, get_current_user

SECRET = "test-only-signing-key-" + "0" * 32


@pytest.fixture()
def guarded_client(monkeypatch):
    """A minimal app behind ``get_current_user``.

    Deliberately not one of the real routers: this pins the dependency itself,
    and the real routers are being edited by other agents in this same wave.
    """
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    monkeypatch.setattr(settings, "allowed_email_domains", "legalsoft.com")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "creator_emails", "")

    api = FastAPI()

    @api.get("/whoami")
    def whoami(user: dict = Depends(get_current_user)) -> dict:
        return {"email": user["email"]}

    return TestClient(api, raise_server_exceptions=False)


def call(client, token: str):
    return client.get("/whoami", headers={"Authorization": f"Bearer {token}"})


def test_allowlisted_token_still_works(guarded_client):
    token = create_token("u1", "colleague@legalsoft.com")
    resp = call(guarded_client, token)
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == "colleague@legalsoft.com"


def test_pre_allowlist_token_is_rejected(guarded_client):
    """The whole point: a *cryptographically valid* token for an account that
    is no longer allowed must not be honoured."""
    token = create_token("u2", "randomperson@gmail.com")
    resp = call(guarded_client, token)
    # 401, not 403 — the frontend's existing 401 handler clears the session.
    assert resp.status_code == 401, resp.text
    assert "authoris" in resp.json()["detail"].lower()


def test_revoked_domain_locks_out_an_existing_token(guarded_client, monkeypatch):
    """Removing a domain from the allowlist takes effect on the next request,
    not whenever the 7-day token happens to expire."""
    token = create_token("u3", "colleague@legalsoft.com")
    assert call(guarded_client, token).status_code == 200
    monkeypatch.setattr(settings, "allowed_email_domains", "")
    assert call(guarded_client, token).status_code == 401


def test_creator_bypass_matches_the_sign_in_rule(guarded_client, monkeypatch):
    """A project owner keeps access with an empty/hostile allowlist — the same
    carve-out ``auth.is_allowed_email`` applies at sign-in. Both paths must
    agree, which is why security.py delegates to it rather than copying it."""
    monkeypatch.setattr(settings, "allowed_email_domains", "")
    monkeypatch.setattr(settings, "creator_emails", "owner@elsewhere.test")
    token = create_token("u4", "Owner@Elsewhere.test")
    assert call(guarded_client, token).status_code == 200


def test_exception_address_is_honoured(guarded_client, monkeypatch):
    monkeypatch.setattr(settings, "allowed_emails", "contractor@outside.dev")
    token = create_token("u5", "Contractor@Outside.dev")
    assert call(guarded_client, token).status_code == 200


def test_the_check_is_the_sign_in_rule_itself(guarded_client, monkeypatch):
    """Not "behaves the same today" — literally the same function. Two copies of
    an access rule drift, and the copy that drifts grants access it shouldn't."""
    from app.routers import auth

    seen: list[str] = []
    monkeypatch.setattr(
        auth, "is_allowed_email", lambda email: seen.append(email) or True
    )
    assert call(guarded_client, create_token("u6", "someone@legalsoft.com")).status_code == 200
    assert seen == ["someone@legalsoft.com"]


def test_unevaluable_allowlist_fails_closed(guarded_client, monkeypatch):
    """If the rule cannot be evaluated, the answer is not "yes"."""
    def boom(email: str) -> bool:
        raise RuntimeError("allowlist backend exploded")

    monkeypatch.setattr(security, "_still_allowed", boom)
    resp = call(guarded_client, create_token("u7", "colleague@legalsoft.com"))
    assert resp.status_code == 503
    assert "allowlist backend exploded" not in resp.text  # internals stay in the log


def test_missing_jwt_secret_is_not_a_500(guarded_client, monkeypatch):
    """``settings.require`` raises RuntimeError, which inside a dependency read
    as "the server crashed" on every authenticated request. An unset secret is a
    deployment fact, and 503 is how you say that."""
    token = create_token("u8", "colleague@legalsoft.com")
    monkeypatch.setattr(settings, "jwt_secret", "")
    resp = call(guarded_client, token)
    assert resp.status_code == 503, resp.text
    assert "jwt_secret" not in resp.text.lower()  # config names stay server-side


def test_missing_google_client_id_is_not_a_500(monkeypatch):
    """Same bug class on the sign-in path."""
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "google_client_id", "")
    with pytest.raises(HTTPException) as excinfo:
        security.verify_google_id_token("any-credential")
    assert excinfo.value.status_code == 503


def test_google_cert_fetch_carries_a_deadline(monkeypatch):
    """The certificate fetch sits on the sign-in path and google-auth defaults
    to a 120s timeout it never overrides — two minutes of a held worker thread.

    Asserted at the seam that matters: what reaches google-auth's own transport
    when the caller (``verify_oauth2_token``) supplies no timeout at all.
    """
    from google.auth.transport import requests as google_requests

    assert 0 < security.GOOGLE_CERT_FETCH_TIMEOUT_SECONDS <= 30

    captured: dict = {}

    def fake_call(self, url, method="GET", body=None, headers=None, timeout=120, **kw):
        captured["timeout"] = timeout
        return None

    monkeypatch.setattr(google_requests.Request, "__call__", fake_call)
    security._TimedRequest()("https://www.googleapis.com/oauth2/v1/certs")
    assert captured["timeout"] == security.GOOGLE_CERT_FETCH_TIMEOUT_SECONDS
    assert captured["timeout"] != 120  # google-auth's default never applies here


def test_missing_and_malformed_credentials_still_401(guarded_client):
    assert guarded_client.get("/whoami").status_code == 401
    assert call(guarded_client, "not-a-jwt").status_code == 401
