from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.security import create_token, is_admin, is_creator, verify_google_id_token
from app.services import firestore_repo

router = APIRouter()


class GoogleLogin(BaseModel):
    credential: str = Field(..., min_length=10, description="Google ID token (JWT)")


@router.post("/auth/google")
def google_login(body: GoogleLogin) -> dict:
    """Verify a Google ID token, upsert the user, and return an app JWT."""
    claims = verify_google_id_token(body.credential)
    user = firestore_repo.get_or_create_google_user(
        email=claims["email"],
        name=claims["name"],
        picture=claims["picture"],
        google_sub=claims["sub"],
    )
    token = create_token(user["id"], user["email"])
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
