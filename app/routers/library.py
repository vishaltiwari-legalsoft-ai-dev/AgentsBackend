"""Library endpoint — every brand kit's creatives in one call.

Backs the sidebar "Library" view in the frontend. Returns each brand with its
real total creative count and a curated set of viewable assets (signed URLs).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.security import get_current_user
from app.services import firestore_repo, storage

router = APIRouter()


def _is_brand_kit_asset(creative: dict) -> bool:
    """True if the record is a human-curated brand-kit asset (not AI-generated).

    AI-generated assets should never reach Firestore in the first place (see
    `_persist_one` in `app.agent.nodes`), but this filter is kept as a final
    safety net so any stray record from a previous build cannot leak into the
    Library or the retrieval context that feeds future generations.
    """
    author = (creative.get("creative_metadata") or {}).get("author", "")
    return author != "AgentOS"


@router.get("/library")
def library(
    per_brand: int = Query(default=24, ge=1, le=100),
    _user: dict = Depends(get_current_user),
) -> dict:
    """Return every brand and its ingested brand-kit creatives."""
    brands_with_assets: list[dict] = []
    for brand in firestore_repo.list_brands():
        creatives = [
            c
            for c in firestore_repo.list_creatives_by_brand(brand["id"], limit=500)
            if _is_brand_kit_asset(c)
        ]
        brands_with_assets.append(
            {
                "id": brand["id"],
                "brand_name": brand["brand_name"],
                "creative_count": len(creatives),
                "creatives": storage.to_gallery(creatives, per_brand),
            }
        )
    return {"brands": brands_with_assets}
