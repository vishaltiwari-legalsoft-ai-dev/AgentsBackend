import base64
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.security import get_current_user
from app.services import canva

router = APIRouter()

# Most-recent access token (single-instance dev/agency use). Swap for a
# per-user, persisted token store for multi-tenant production.
_active_token: dict[str, str] = {}

# Only fetch import sources we hand out ourselves (signed GCS URLs) or inline
# data URLs — a caller-controlled URL is otherwise an SSRF primitive from
# inside Cloud Run (metadata server, VPC services).
_ALLOWED_FETCH_HOSTS = {"storage.googleapis.com"}
_MAX_DATA_URL_BYTES = 25 * 1024 * 1024


class ImportRequest(BaseModel):
    image_url: str = Field(..., min_length=1)
    name: str = Field(default="AgentOS Asset", min_length=1)


@router.get("/canva/authorize")
def authorize(user: dict = Depends(get_current_user)) -> RedirectResponse:
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
            f"&reason={quote(str(error)[:200])}&detail={quote(str(detail)[:400])}"
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
            f"&detail={quote(str(exc)[:400])}"
        )
    return RedirectResponse(f"{settings.app_public_url}?canva=connected")


def _fetch_bytes(image_url: str) -> bytes:
    if image_url.startswith("data:"):
        _, _, payload = image_url.partition(",")
        if not payload:
            raise HTTPException(400, "Malformed data URL")
        if len(payload) > _MAX_DATA_URL_BYTES * 4 // 3:
            raise HTTPException(413, "Image too large (max 25 MB)")
        return base64.b64decode(payload)
    parsed = urlparse(image_url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_FETCH_HOSTS:
        raise HTTPException(
            400, "image_url must be a data: URL or a signed storage URL"
        )
    try:
        response = httpx.get(image_url, timeout=60)
    except httpx.HTTPError as exc:
        raise HTTPException(400, f"Could not fetch image: {exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(400, f"Could not fetch image ({response.status_code})")
    return response.content


@router.post("/canva/import")
def import_asset(request: ImportRequest, user: dict = Depends(get_current_user)) -> dict:
    token = _active_token.get("token")
    if not token:
        raise HTTPException(401, "Connect Canva first via /api/canva/authorize")
    asset_id = canva.import_asset(token, _fetch_bytes(request.image_url), request.name)
    return {"asset_id": asset_id}
