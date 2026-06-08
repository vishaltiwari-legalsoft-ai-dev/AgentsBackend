"""Canva Connect integration (OAuth 2.0 + PKCE) and asset import.

Used in Phase 4 to import Variation B (the placeholder image) into an editable
Canva workspace for precise logo/font placement.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx

from app.config import settings

CANVA_AUTH_URL = "https://www.canva.com/api/oauth/authorize"
CANVA_TOKEN_URL = "https://api.canva.com/rest/v1/oauth/token"
CANVA_ASSET_UPLOAD_URL = "https://api.canva.com/rest/v1/asset-uploads"
CANVA_SCOPES = "asset:read asset:write design:content:read design:content:write"

# Ephemeral state -> PKCE verifier store (single-instance dev/agency use).
_pkce_store: dict[str, tuple[str, float]] = {}
_PKCE_TTL_SECONDS = 600


def is_configured() -> bool:
    return bool(settings.canva_client_id and settings.canva_client_secret)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _prune() -> None:
    now = time.time()
    for state, (_, created) in list(_pkce_store.items()):
        if now - created > _PKCE_TTL_SECONDS:
            _pkce_store.pop(state, None)


def get_authorization_url() -> tuple[str, str]:
    """Build the Canva authorization URL; returns (url, state)."""
    _prune()
    client_id = settings.require("canva_client_id")
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(24))
    _pkce_store[state] = (verifier, time.time())

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": settings.canva_redirect_uri,
        "scope": CANVA_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{CANVA_AUTH_URL}?{urlencode(params)}", state


def exchange_code_for_token(code: str, state: str) -> str:
    """Exchange the OAuth code for an access token; returns the access token."""
    entry = _pkce_store.pop(state, None)
    if entry is None:
        raise RuntimeError("Invalid or expired OAuth state")
    verifier, _ = entry

    client_id = settings.require("canva_client_id")
    client_secret = settings.require("canva_client_secret")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    response = httpx.post(
        CANVA_TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": settings.canva_redirect_uri,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Canva token exchange failed ({response.status_code}): {response.text}"
        )
    return response.json()["access_token"]


def import_asset(access_token: str, data: bytes, asset_name: str) -> str:
    """Upload an image to the connected Canva workspace; returns the asset id."""
    name_b64 = base64.b64encode(asset_name.encode()).decode()
    create = httpx.post(
        CANVA_ASSET_UPLOAD_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/octet-stream",
            "Asset-Upload-Metadata": f'{{"name_base64":"{name_b64}"}}',
        },
        content=data,
        timeout=60,
    )
    if create.status_code >= 400:
        raise RuntimeError(
            f"Canva asset upload failed ({create.status_code}): {create.text}"
        )

    job = create.json().get("job", {})
    for _ in range(10):
        if job.get("status") != "in_progress":
            break
        time.sleep(1)
        poll = httpx.get(
            f"{CANVA_ASSET_UPLOAD_URL}/{job.get('id')}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        if poll.status_code >= 400:
            raise RuntimeError(
                f"Canva upload status check failed ({poll.status_code}): {poll.text}"
            )
        job = poll.json().get("job", {})

    asset = job.get("asset")
    if job.get("status") != "success" or not asset:
        raise RuntimeError(f"Canva asset upload did not succeed: {job.get('status')}")
    return asset["id"]
