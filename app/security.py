"""Authentication: Google ID-token verification + app JWT issuing/verification.

Login is Google-only (Google Identity Services). The frontend obtains a Google
ID token, posts it here, we verify it against our Google Web Client ID, then
issue our own JWT that the SPA stores and sends as a Bearer token.
"""

from __future__ import annotations

import logging
import time

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from app.config import settings

logger = logging.getLogger("agentos.auth")
_bearer = HTTPBearer(auto_error=False)

# Deadline for fetching Google's signing certificates during ID-token
# verification. This sits on the *sign-in path*, in a sync handler, i.e. on one
# of anyio's 40 worker threads — and google-auth's transport defaults to a 120s
# timeout it never overrides. The certs are a small static JSON from Google's
# edge; 10s is already a generous outlier, and failing fast lets the user retry
# instead of holding a thread for two minutes.
GOOGLE_CERT_FETCH_TIMEOUT_SECONDS = 10


class _TimedRequest(google_requests.Request):
    """google-auth transport with our deadline instead of its 120s default.

    ``verify_oauth2_token`` calls the transport without a timeout, so overriding
    the default in ``__call__`` is the only seam that reaches the cert fetch.
    """

    def __call__(  # type: ignore[override]
        self, url, method="GET", body=None, headers=None,
        timeout=GOOGLE_CERT_FETCH_TIMEOUT_SECONDS, **kwargs,
    ):
        return super().__call__(
            url, method=method, body=body, headers=headers, timeout=timeout, **kwargs
        )


def _require_config(field: str) -> str:
    """``settings.require`` mapped to an honest HTTP status.

    ``require`` raises ``RuntimeError``, which inside a dependency or handler
    becomes a 500 — "the server crashed" — when the truth is "this deployment
    is missing a setting". 503 says that without leaking which one to the
    caller; the name goes to the log, where operators can act on it.
    """
    try:
        return settings.require(field)
    except RuntimeError as exc:
        logger.error("auth is not configured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured on this server.",
        ) from exc


def verify_google_id_token(credential: str) -> dict[str, str]:
    """Verify a Google ID token and return key profile claims.

    Raises HTTP 401 if the token is invalid or its audience does not match our
    configured Google Web Client ID; 503 if this deployment has no client id.
    """
    client_id = _require_config("google_client_id")
    try:
        claims = google_id_token.verify_oauth2_token(
            credential,
            _TimedRequest(),
            client_id,
            clock_skew_in_seconds=60,  # tolerate minor server/Google clock drift
        )
    except ValueError as exc:
        logger.warning("Google ID token verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid Google credential: {exc}",
        ) from exc

    email = claims.get("email")
    if not email or not claims.get("email_verified", False):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google account email is not verified",
        )
    return {
        "sub": claims.get("sub", ""),
        "email": email,
        "name": claims.get("name", email.split("@")[0]),
        "picture": claims.get("picture", ""),
    }


def is_creator(email: str) -> bool:
    """Top-tier role (above Super Admin): may manage secrets/integrations."""
    return email.lower() in settings.creator_email_set


def is_admin(email: str) -> bool:
    # Creators are a superset of Super Admins — they keep all admin access.
    return email.lower() in settings.admin_email_set or is_creator(email)


def is_geo_editor(email: str) -> bool:
    """May shape the GEO agent's registry — prompts, personas, brand config.

    A NARROW role, and the first per-agent one here: it unlocks the eight
    registry-shaping GEO routes and nothing else. It is not a step on the
    admin ladder — a GEO editor is not an admin and cannot read Secrets, the
    admin database viewer or any other agent's writes.

    Creators are included the same way they are in ``is_admin``: the people who
    could already do this keep doing it, and the implication is stated once
    here rather than re-derived at each guard.
    """
    return email.lower() in settings.geo_editor_email_set or is_creator(email)


def is_geo_only(email: str) -> bool:
    """Whether this account's reach STOPS at the GEO workspace.

    The counterpart to ``is_geo_editor`` and its opposite in direction.
    ``is_geo_editor`` is additive — it opens nine routes on top of whatever the
    account already reached. This is subtractive: it closes every route outside
    one workspace. That distinction is the whole defect this closes. Being in
    ``ALLOWED_EMAILS`` meant the entire workspace, so "GEO panel only" was a
    label on a flag that removed nothing, and four outside contractors could
    read the company marketing tracker in two requests.

    Creators and admins are exempt unconditionally, and the check runs BEFORE
    the list lookup on purpose: an owner who fat-fingers their own address into
    ``GEO_ONLY_EMAILS`` must not be able to lock themselves out of the panel
    they administer. It is the same "the people who could already do this keep
    doing it" implication ``is_admin`` and ``is_geo_editor`` carry, stated once
    here rather than re-derived at the guard.
    """
    if is_creator(email) or is_admin(email):
        return False
    return email.lower() in settings.geo_only_email_set


def create_token(
    user_id: str, email: str, session_id: str | None = None, timezone: str = "UTC"
) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "email": email,
        "admin": is_admin(email),
        "creator": is_creator(email),
        # Session this token belongs to, so each request can be attributed back
        # to a sign-in (older tokens simply won't carry it).
        "sid": session_id,
        # Caller's IANA timezone, stamped onto run rows for local-time display.
        "tz": timezone,
        "iat": now,
        "exp": now + settings.jwt_expires_minutes * 60,
    }
    return jwt.encode(payload, _require_config("jwt_secret"), algorithm="HS256")


def _still_allowed(email: str) -> bool:
    """Re-apply the sign-in allowlist to an already-issued token.

    Deliberately delegates to ``app.routers.auth.is_allowed_email`` rather than
    re-implementing the rule. Two copies of an access rule drift, and the copy
    that drifts is the one that grants access it shouldn't. The import is local
    because ``app.routers.auth`` imports *this* module at load time — by the
    time any request runs, both are in ``sys.modules`` and this is a dict
    lookup, not a re-import.
    """
    from app.routers.auth import is_allowed_email

    return is_allowed_email(email)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, object]:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )
    try:
        payload = jwt.decode(
            credentials.credentials, _require_config("jwt_secret"), algorithms=["HS256"]
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        ) from exc

    # Gating sign-in only protects accounts that sign in *after* the gate: a
    # token minted before it stays valid for the rest of its 7-day life, and
    # revoking someone's access has no effect until it expires. So the
    # allowlist is re-checked here, on every request, against the email claim
    # already inside the token — a set-membership test, no database round trip.
    email = str(payload.get("email") or "")
    try:
        allowed = _still_allowed(email)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — cannot evaluate the rule
        # Fail closed and say so honestly: an authorisation check that cannot
        # run must never be read as "authorised". 503, not 401 — the token may
        # be perfectly good; it is the server that is broken.
        logger.exception("could not evaluate the sign-in allowlist")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authorisation is temporarily unavailable.",
        ) from exc
    if not allowed:
        # 401 (not 403) on purpose: the frontend's existing 401 handler clears
        # the session and returns the user to sign-in, which is exactly the
        # right outcome for a token that should no longer exist.
        logger.warning("rejected token for non-allowlisted account: %s", email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This account is no longer authorised to use AgentOS.",
        )

    # Same reasoning one step further: the admin/creator claims were stamped at
    # mint time, so dropping an address from CREATOR_EMAILS left that token
    # holding Settings → Secrets for the rest of its 7-day life. Re-derive both
    # from the email claim instead — the same helpers the minting path uses, and
    # the same kind of set-membership lookup as the allowlist above, so there is
    # no cost argument for trusting the stale copy. The claims stay in the
    # payload; they simply stop being authoritative.
    #
    # ``is_geo_editor`` joins them rather than being stamped into the token at
    # mint time: a claim minted today would still be honoured for the rest of
    # its 7-day life after the address left GEO_EDITOR_EMAILS, which is the
    # exact defect the two flags above were moved here to close. It is
    # deliberately absent from ``create_token`` for the same reason — a claim
    # nothing reads cannot drift.
    try:
        admin, creator = is_admin(email), is_creator(email)
        geo_editor = is_geo_editor(email)
        # The SCOPE, derived here for the same reason as the three flags above
        # and never stamped into the token: a scope minted at sign-in would
        # outlive its own revocation by the 7-day token life, and the direction
        # that matters most is the one where scoping someone DOWN has to take
        # effect on the next request rather than next week.
        geo_only = is_geo_only(email)
    except Exception as exc:  # noqa: BLE001 — cannot evaluate the role config
        # Fail closed, exactly as above: a role check that cannot run is not a
        # grant. 503 because the token may be fine; the server is not.
        logger.exception("could not evaluate the role configuration")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authorisation is temporarily unavailable.",
        ) from exc

    return {
        "id": payload["sub"],
        "email": payload["email"],
        "is_admin": admin,
        "is_creator": creator,
        "is_geo_editor": geo_editor,
        "is_geo_only": geo_only,
        "session_id": payload.get("sid") or "",
        "timezone": payload.get("tz") or "UTC",
    }


def optional_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, object] | None:
    """``get_current_user`` for callers who may legitimately not have a token.

    Exists for exactly one consumer: ``app.scopes.deny_outside_geo``, which is
    attached to EVERY router at include time — public routes, cron routes and
    OAuth callbacks included. Depending on ``get_current_user`` there would
    have turned the sign-in door and the Cloud Run liveness probe into
    authenticated endpoints, which is a far larger change than the one being
    made.

    So a missing or unusable token is not an error here, it is ``None``: the
    scope layer has nothing to narrow and steps aside, and the route's own
    guard — if it has one — still answers 401 a moment later, from the same
    function, with the same message. Nothing is made reachable that was not
    already: this returns a principal, never a decision.

    Delegating to ``get_current_user`` rather than decoding here is the point.
    A second decode would be a second copy of the sign-in allowlist re-check
    and the role re-derivation, and the copy that drifts is always the one that
    grants what it should not. The cost is one extra HS256 verification per
    request, which is a set of hash operations on a string already in memory.
    """
    if credentials is None:
        return None
    try:
        return get_current_user(credentials)
    except HTTPException:
        # 401 (bad/expired/de-provisioned) and 503 (auth unconfigured) both mean
        # "no principal to narrow". The route's own dependency raises the very
        # same thing immediately after, so suppressing it here changes what is
        # reachable by nothing at all.
        return None


def require_admin(
    user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    if not user.get("is_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def require_creator(
    user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """Creator-only guard — for managing secrets/integrations."""
    if not user.get("is_creator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Creator only"
        )
    return user


def require_geo_editor(
    user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """GEO editor guard — the registry-shaping routes of the GEO agent.

    Reads the ONE flag ``get_current_user`` derived, exactly as
    ``require_admin`` does. It deliberately does not also check ``is_creator``:
    that implication already lives in ``is_geo_editor``, and a second copy of
    an access rule is a second thing to drift — with the drifting copy being
    the one that grants what it should not.

    Delegating to ``get_current_user`` rather than decoding the JWT here is
    load-bearing, not stylistic: it is what keeps the per-request sign-in
    allowlist in front of the role check, so a de-provisioned account is
    refused with 401 before this ever runs.
    """
    if not user.get("is_geo_editor"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="GEO editor only"
        )
    return user
