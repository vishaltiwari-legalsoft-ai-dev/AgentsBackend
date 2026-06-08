import base64

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.services import canva

router = APIRouter()

# Most-recent access token (single-instance dev/agency use). Swap for a
# per-user, persisted token store for multi-tenant production.
_active_token: dict[str, str] = {}


class ImportRequest(BaseModel):
    image_url: str = Field(..., min_length=1)
    name: str = Field(default="AgentOS Asset", min_length=1)


@router.get("/canva/authorize")
def authorize() -> RedirectResponse:
    if not canva.is_configured():
        raise HTTPException(503, "Canva integration is not configured")
    url, _ = canva.get_authorization_url()
    return RedirectResponse(url)


@router.get("/canva/callback")
def callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    """OAuth redirect handler.

    Canva calls this with either (code + state) on success or (error +
    error_description) on failure. We surface failures back to the frontend as
    query params so the user sees a clear, actionable message instead of a 422.
    """
    if error:
        detail = error_description or error
        return RedirectResponse(
            f"{settings.app_public_url}?canva=error"
            f"&reason={error}&detail={detail}"
        )
    if not code or not state:
        return RedirectResponse(
            f"{settings.app_public_url}?canva=error&reason=missing_params"
            "&detail=Canva%20did%20not%20return%20code%20or%20state"
        )
    try:
        _active_token["token"] = canva.exchange_code_for_token(code, state)
    except Exception as exc:  # noqa: BLE001 - surface to the UI cleanly
        return RedirectResponse(
            f"{settings.app_public_url}?canva=error&reason=token_exchange"
            f"&detail={exc}"
        )
    return RedirectResponse(f"{settings.app_public_url}?canva=connected")


def _fetch_bytes(image_url: str) -> bytes:
    if image_url.startswith("data:"):
        _, _, payload = image_url.partition(",")
        if not payload:
            raise HTTPException(400, "Malformed data URL")
        return base64.b64decode(payload)
    try:
        response = httpx.get(image_url, timeout=60)
    except httpx.HTTPError as exc:
        raise HTTPException(400, f"Could not fetch image: {exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(400, f"Could not fetch image ({response.status_code})")
    return response.content


@router.post("/canva/import")
def import_asset(request: ImportRequest) -> dict:
    token = _active_token.get("token")
    if not token:
        raise HTTPException(401, "Connect Canva first via /api/canva/authorize")
    asset_id = canva.import_asset(token, _fetch_bytes(request.image_url), request.name)
    return {"asset_id": asset_id}
