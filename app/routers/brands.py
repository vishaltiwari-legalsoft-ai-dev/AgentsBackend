from fastapi import APIRouter, Depends, HTTPException

from app.security import get_current_user
from app.services import firestore_repo

router = APIRouter()


@router.get("/brands")
def list_brands(_user: dict = Depends(get_current_user)) -> dict:
    return {"brands": firestore_repo.list_brands()}


@router.get("/brands/{brand_id}")
def get_brand(brand_id: str, _user: dict = Depends(get_current_user)) -> dict:
    brand = firestore_repo.get_brand(brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return {"brand": brand, "creatives": firestore_repo.list_creatives_by_brand(brand_id)}


@router.get("/brands/{brand_id}/kit")
def brand_kit(brand_id: str, _user: dict = Depends(get_current_user)) -> dict:
    """Compact brand kit for the guided intake panel: palette, fonts, logo."""
    brand = firestore_repo.get_brand(brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")

    meta = brand.get("brand_metadata") or {}

    return {
        "brand_name": brand.get("brand_name"),
        "colors": meta.get("primary_colors", []),
        "fonts": meta.get("fonts", []),
        "tone_of_voice": meta.get("tone_of_voice"),
        "logo_url": None,
    }
