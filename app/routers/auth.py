from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.security import create_token, is_admin, is_creator, verify_google_id_token
from app.services import firestore_repo

router = APIRouter()


class GoogleLogin(BaseModel):
    credential: str = Field(..., min_length=10, description="Google ID token (JWT)")


@router.post("/auth/google")
def google_login(body: GoogleLogin, request: Request) -> dict:
    """Verify a Google ID token, upsert the user, open a session, return an app JWT."""
    claims = verify_google_id_token(body.credential)
    user = firestore_repo.get_or_create_google_user(
        email=claims["email"],
        name=claims["name"],
        picture=claims["picture"],
        google_sub=claims["sub"],
    )
    # Open a session row and bake its id into the token so every subsequent
    # request can be tied back to this sign-in.
    session_id = firestore_repo.create_session(
        user_id=str(user["id"]),
        email=user["email"],
        name=user.get("name", ""),
        ip=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
    )
    token = create_token(user["id"], user["email"], session_id=session_id)
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
