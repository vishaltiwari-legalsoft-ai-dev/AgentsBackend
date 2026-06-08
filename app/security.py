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


def verify_google_id_token(credential: str) -> dict[str, str]:
    """Verify a Google ID token and return key profile claims.

    Raises HTTP 401 if the token is invalid or its audience does not match our
    configured Google Web Client ID.
    """
    client_id = settings.require("google_client_id")
    try:
        claims = google_id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            client_id,
            clock_skew_in_seconds=10,  # tolerate minor server/Google clock drift
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


def is_admin(email: str) -> bool:
    return email.lower() in settings.admin_email_set


def create_token(user_id: str, email: str) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "email": email,
        "admin": is_admin(email),
        "iat": now,
        "exp": now + settings.jwt_expires_minutes * 60,
    }
    return jwt.encode(payload, settings.require("jwt_secret"), algorithm="HS256")


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, object]:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )
    try:
        payload = jwt.decode(
            credentials.credentials, settings.require("jwt_secret"), algorithms=["HS256"]
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        ) from exc
    return {
        "id": payload["sub"],
        "email": payload["email"],
        "is_admin": bool(payload.get("admin", False)),
    }


def require_admin(
    user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    if not user.get("is_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user
