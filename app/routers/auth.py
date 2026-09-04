import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.config import settings
from app.security import (
    create_token, is_admin, is_creator, is_geo_editor, verify_google_id_token,
)
from app.services import firestore_repo

router = APIRouter()
logger = logging.getLogger("agentos.auth")

# Shown to every rejected account, whatever the reason. Deliberately says
# nothing about whether the address is known here — a rejected stranger and a
# de-provisioned colleague must be indistinguishable to the caller.
_NOT_ALLOWED = (
    "This Google account is not authorised to use AgentOS. "
    "Ask a workspace admin to grant access."
)


class GoogleLogin(BaseModel):
    credential: str = Field(..., min_length=10, description="Google ID token (JWT)")
    # The browser's IANA timezone (e.g. "Asia/Kolkata"); stamped onto run rows so
    # the admin tables show local time. Defaults to UTC if the client omits it.
    timezone: str = ""


def is_allowed_email(email: str) -> bool:
    """Whether a *verified* Google address is permitted to sign in.

    Holding a valid Google token proves identity, not membership. Sign-in is
    the one door that has to enforce it, because Cloud Run serves this API
    --allow-unauthenticated.

    Allowed when the address is a project owner (Creator — they are named in
    config/env and must never be locked out of their own panel), is listed in
    ALLOWED_EMAILS, or its domain is listed in ALLOWED_EMAIL_DOMAINS. With both
    lists empty nobody but an owner gets in: an unconfigured allowlist means
    "no one", never "everyone".
    """
    address = email.strip().lower()
    if not address:
        return False
    if is_creator(address):
        return True
    if address in settings.allowed_email_set:
        return True
    domain = address.rpartition("@")[2]
    return bool(domain) and domain in settings.allowed_email_domain_set


@router.post("/auth/google")
def google_login(body: GoogleLogin) -> dict:
    """Verify a Google ID token, upsert the user, return an app JWT.

    A fresh session id + the caller's timezone are baked into the token so every
    later request can be attributed to this sign-in and stamped with local time.
    """
    claims = verify_google_id_token(body.credential)
    # Gate BEFORE the upsert: an unauthorised sign-in must not create a user
    # document (no junk tenants, and no "account exists" oracle either).
    if not is_allowed_email(claims["email"]):
        logger.warning("Sign-in refused for non-allowlisted account: %s", claims["email"])
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_NOT_ALLOWED)
    user = firestore_repo.get_or_create_google_user(
        email=claims["email"],
        name=claims["name"],
        picture=claims["picture"],
        google_sub=claims["sub"],
    )
    session_id = firestore_repo.new_session_id()
    token = create_token(
        user["id"], user["email"], session_id=session_id,
        timezone=(body.timezone or "UTC").strip() or "UTC",
    )
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user.get("name", ""),
            "picture": user.get("picture", ""),
            "is_admin": is_admin(user["email"]),
            "is_creator": is_creator(user["email"]),
            # Derived from config here, exactly like the two above, and NOT
            # read back out of the token — the token carries no such claim on
            # purpose (see get_current_user), because a claim minted at sign-in
            # outlives its own revocation by the 7-day token life.
            #
            # This is a display hint, never the enforcement. The server decides
            # again on every request in ``require_geo_editor``; all this does is
            # let the console show a non-editor the read-only view instead of
            # buttons that answer 403 when pressed.
            "is_geo_editor": is_geo_editor(user["email"]),
        },
    }
