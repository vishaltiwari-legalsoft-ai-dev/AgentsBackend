"""Per-user Gmail consent, the refresh-token vault, and the credentials that
come back out of it.

The mechanics follow ``seo_geo_agent.gsc_oauth`` — an HMAC-signed ``state``,
an httpx code exchange, ``google.oauth2.credentials.Credentials`` rebuilt from
the refresh token — with three deliberate differences:

* **The redirect URI is the frontend**, ``<app_public_url>/oauth/google``.
  Google sends the browser there; the console POSTs ``{code, state}`` to
  ``POST /api/inbox/oauth/complete`` with the user's own bearer. So there is
  no public callback route, and ``state`` is bound to the user id, expires in
  ten minutes, and is checked against the caller who completes it.
* **The refresh token is sealed** with Fernet under ``INBOX_TOKEN_KEY`` before
  it is stored, and nothing in this package or the router ever returns it.
  ``gsc_oauth`` keeps its token in the clear; that is not copied.
* **Its own OAuth client** (``INBOX_GOOGLE_CLIENT_ID`` / ``_SECRET``). The
  shared Web client's consent screen also signs in outside contractors, so
  it can never become an Internal app — and an Internal consent screen is
  what lets a Workspace member grant ``gmail.readonly`` without a verification
  review. A second GCP project owns this client.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.services.google_http import AUTH_TIMEOUT_SECONDS, _TimedRequest

from . import refuse_if_offline

logger = logging.getLogger("agentos.inbox.oauth")

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
#: Revoking a refresh token revokes the whole grant — what "disconnect" means.
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
#: Where Google sends the browser back: a FRONTEND page, appended to
#: ``settings.app_public_url``. The backend has no callback route.
REDIRECT_PATH = "/oauth/google"
#: Seconds a consent round-trip may take.
STATE_TTL_SECONDS = 600
EXCHANGE_TIMEOUT_SECONDS = 30
REVOKE_TIMEOUT_SECONDS = 10
#: Attempts at the revoke, including the first. It is idempotent, so a
#: transport blip or a 5xx is worth one more try; a 4xx is an answer.
REVOKE_ATTEMPTS = 2


class OAuthNotConfigured(RuntimeError):
    """``INBOX_GOOGLE_CLIENT_ID`` or ``INBOX_GOOGLE_CLIENT_SECRET`` is empty."""


class TokenKeyMissing(RuntimeError):
    """``INBOX_TOKEN_KEY`` is empty or not a Fernet key."""


class BadState(ValueError):
    """The ``state`` failed a check; the message is one plain sentence."""


class ExchangeFailed(RuntimeError):
    """Google refused the code, granted the wrong scope, or sent no refresh token."""


class RevokedGrant(RuntimeError):
    """The refresh answered ``invalid_grant``, or the sealed token cannot be
    opened with this server's key. Either way: connect again."""


class TokenRefreshFailed(RuntimeError):
    """The refresh failed for a reason a later fire may not see again."""


def _client() -> tuple[str, str]:
    client_id = settings.inbox_google_client_id
    client_secret = settings.inbox_google_client_secret
    if not client_id or not client_secret:
        raise OAuthNotConfigured(
            "Gmail connection is not configured on this server: "
            "INBOX_GOOGLE_CLIENT_ID and INBOX_GOOGLE_CLIENT_SECRET are needed."
        )
    return client_id, client_secret


def redirect_uri() -> str:
    return settings.app_public_url.rstrip("/") + REDIRECT_PATH


# --------------------------------------------------------------------------- #
# The state token: user id, issue time, a nonce, and a signature over all three
# --------------------------------------------------------------------------- #

def _same(a: str, b: str) -> bool:
    """Constant-time equality that cannot raise. ``hmac.compare_digest`` on two
    ``str`` raises ``TypeError`` when either holds a non-ASCII character, and a
    client-supplied ``state`` may hold anything — that must be a refusal, not
    a 500. Comparing the UTF-8 bytes is total."""
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_state(user_id: str, *, now: float | None = None) -> str:
    _, secret = _client()
    issued = int(time.time() if now is None else now)
    payload = f"{user_id}.{issued}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(payload, secret)}"


def read_state(state: str, *, user_id: str, now: float | None = None) -> None:
    """Every check the returned ``state`` must pass, or :class:`BadState`.

    The signature is checked before the binding so an attacker learns nothing
    about which user ids exist from the error; the messages are the same
    sentence for both.
    """
    _, secret = _client()
    try:
        bound_to, issued_text, nonce, signature = str(state or "").rsplit(".", 3)
        issued = int(issued_text)
    except ValueError as exc:
        raise BadState("The connect link is not valid — start again from the panel.") from exc
    expected = _sign(f"{bound_to}.{issued_text}.{nonce}", secret)
    if not _same(signature, expected):
        raise BadState("The connect link is not valid — start again from the panel.")
    if (time.time() if now is None else now) - issued > STATE_TTL_SECONDS:
        raise BadState("The connect window expired — start again from the panel.")
    if not _same(bound_to, user_id):
        raise BadState("This connect link was started by a different account.")


# --------------------------------------------------------------------------- #
# The consent round-trip
# --------------------------------------------------------------------------- #

def auth_url(user_id: str, *, login_hint: str = "") -> tuple[str, str]:
    """``(url, state)``. ``access_type=offline`` + ``prompt=consent`` is what
    makes Google return a refresh token, which the five-minute poll needs.

    ``login_hint`` is the caller's signed-in address, so Google pre-selects
    that account. It is only a hint — the connect step still compares the
    mailbox Google hands back with the caller (``pipeline.connect``)."""
    client_id, _ = _client()
    state = make_state(user_id)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    if login_hint:
        params["login_hint"] = login_hint
    params["state"] = state
    url = AUTH_ENDPOINT + "?" + urlencode(params)
    return url, state


@dataclass(frozen=True)
class Tokens:
    refresh_token: str
    access_token: str


def complete(user_id: str, *, code: str, state: str) -> Tokens:
    """Verify the state against the caller, then exchange the code.

    The address of the connected mailbox is NOT in the token response (the
    only scope asked for is Gmail's); the caller reads it from
    :func:`gmail_client.profile`, which it needs anyway for the history
    checkpoint.
    """
    read_state(state, user_id=user_id)
    refuse_if_offline("The Google token exchange")
    client_id, client_secret = _client()
    try:
        resp = httpx.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
            },
            timeout=EXCHANGE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise ExchangeFailed("Google could not be reached to finish the connection.") from exc
    if resp.status_code != 200:
        # An error body carries no token; the first 200 chars name the cause.
        logger.warning("a12 token exchange refused: HTTP %s %s", resp.status_code, resp.text[:200])
        raise ExchangeFailed("Google refused the sign-in code — start the connection again.")
    body = resp.json()
    granted = set(str(body.get("scope") or "").split())
    if SCOPE not in granted:
        raise ExchangeFailed(
            "The Google account did not grant read access to Gmail — connect again and allow it."
        )
    refresh_token = body.get("refresh_token")
    if not refresh_token:
        raise ExchangeFailed(
            "Google did not return a refresh token — remove AgentOS at "
            "myaccount.google.com/permissions and connect again."
        )
    return Tokens(refresh_token=str(refresh_token), access_token=str(body.get("access_token") or ""))


def revoke(refresh_token: str) -> bool:
    """Ask Google to revoke the grant behind ``refresh_token``. ``True`` only
    when Google answered 200; every other outcome — offline, unreachable, a
    refusal — is ``False`` and logged, never raised, because a disconnect
    clears the hub's own copy either way and must tell the panel the truth
    about Google's side. The token itself is never logged."""
    if not refresh_token:
        return False
    from . import offline

    if offline():
        logger.info("a12: grant revoke skipped while INBOX_OFFLINE=1")
        return False
    for attempt in range(REVOKE_ATTEMPTS):
        try:
            resp = httpx.post(
                REVOKE_ENDPOINT,
                data={"token": refresh_token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=REVOKE_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            logger.warning("a12 grant revoke could not reach Google (attempt %d): %s",
                           attempt + 1, type(exc).__name__)
            continue
        if resp.status_code == 200:
            return True
        if resp.status_code < 500:
            # 400 invalid_token: already dead on Google's side, or never valid.
            # Either way this call did not revoke anything it can vouch for.
            logger.warning("a12 grant revoke refused: HTTP %s", resp.status_code)
            return False
        logger.warning("a12 grant revoke failed: HTTP %s (attempt %d)", resp.status_code, attempt + 1)
    return False


# --------------------------------------------------------------------------- #
# Credentials from a stored grant
# --------------------------------------------------------------------------- #

def credentials(refresh_token: str, *, access_token: str | None = None):
    """``google.oauth2`` user credentials for the grant. Passing the access
    token from a fresh exchange saves one refresh on the first call."""
    from google.oauth2.credentials import Credentials

    client_id, client_secret = _client()
    return Credentials(
        token=access_token or None,
        refresh_token=refresh_token,
        token_uri=TOKEN_ENDPOINT,
        client_id=client_id,
        client_secret=client_secret,
        scopes=[SCOPE],
    )


def refresh(creds) -> None:
    """Mint an access token NOW, with our deadline, so a revoked grant is found
    before any Gmail call and can be recorded as exactly that."""
    if getattr(creds, "valid", False):
        return
    refuse_if_offline("The Google token refresh")
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request

    try:
        creds.refresh(_TimedRequest(Request(), AUTH_TIMEOUT_SECONDS))
    except RefreshError as exc:
        if "invalid_grant" in str(exc):
            raise RevokedGrant(
                "Google no longer honours this Gmail connection — connect again."
            ) from exc
        logger.warning("a12 token refresh failed: %s", exc)
        raise TokenRefreshFailed(
            "Google did not issue an access token; the next poll will try again."
        ) from exc


# --------------------------------------------------------------------------- #
# The vault: a refresh token is sealed before it is stored and opened only to
# build credentials. Nothing returns the plaintext to a caller of the API.
# --------------------------------------------------------------------------- #

def _fernet():
    from cryptography.fernet import Fernet

    key = settings.inbox_token_key
    if not key:
        raise TokenKeyMissing(
            "Gmail connection is not configured on this server: INBOX_TOKEN_KEY "
            "is needed to store the connection safely."
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise TokenKeyMissing(
            "INBOX_TOKEN_KEY is not a valid Fernet key — generate one with "
            "Fernet.generate_key()."
        ) from exc


def require_token_key() -> None:
    """Fail before Google is involved when the vault has no key."""
    _fernet()


def seal(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def open_(sealed: str) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(sealed.encode()).decode()
    except InvalidToken as exc:
        raise RevokedGrant(
            "The stored Gmail connection cannot be opened with this server's "
            "INBOX_TOKEN_KEY — connect again."
        ) from exc
