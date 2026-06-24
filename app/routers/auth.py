from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.security import create_token, is_admin, is_creator, verify_google_id_token
from app.services import firestore_repo

router = APIRouter()


class GoogleLogin(BaseModel):
    credential: str = Field(..., min_length=10, description="Google ID token (JWT)")
    # The browser's IANA timezone (e.g. "Asia/Kolkata"); stamped onto run rows so
    # the admin tables show local time. Defaults to UTC if the client omits it.
    timezone: str = ""


@router.post("/auth/google")
def google_login(body: GoogleLogin) -> dict:
    """Verify a Google ID token, upsert the user, return an app JWT.

    A fresh session id + the caller's timezone are baked into the token so every
    later request can be attributed to this sign-in and stamped with local time.
    """
    claims = verify_google_id_token(body.credential)
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
        },
    }
