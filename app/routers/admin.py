import logging
from collections import Counter, defaultdict

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.security import require_admin, require_creator
from app.services import firestore_repo, runtime_config

router = APIRouter()
logger = logging.getLogger("agentos.admin")

# Sensitive runtime fields an admin may manage from the UI (mirrors
# runtime_config.OVERRIDE_FIELDS). The API key is handled specially (masked).
_MODEL_FIELDS = (
    "openrouter_model",
    "openrouter_fast_model",
    "openrouter_image_model",
    "openrouter_vision_model",
)


def _mask(secret: str) -> str:
    """Show only enough of a secret to recognise it — never the full value."""
    if not secret:
        return ""
    if len(secret) <= 10:
        return "••••"
    return f"{secret[:6]}…{secret[-4:]}"


def _settings_payload() -> dict:
    """Current effective settings with the API key masked (never returned raw)."""
    overrides = firestore_repo.get_app_config(use_cache=False)
    key = runtime_config.get("openrouter_api_key")
    key_source = (
        "override" if overrides.get("openrouter_api_key")
        else "env" if settings.openrouter_api_key else "unset"
    )
    return {
        "openrouter": {
            "api_key_set": bool(key),
            "api_key_hint": _mask(key),
            "api_key_source": key_source,
            "model": runtime_config.get("openrouter_model"),
            "fast_model": runtime_config.get("openrouter_fast_model"),
            "image_model": runtime_config.get("openrouter_image_model"),
            "vision_model": runtime_config.get("openrouter_vision_model"),
        },
        # Per-field provenance so the UI can show "from env" vs "set here".
        "sources": {
            f: ("override" if overrides.get(f) else "env") for f in _MODEL_FIELDS
        },
    }


class AdminSettingsBody(BaseModel):
    # None = leave untouched; "" = clear the override (fall back to env).
    openrouter_api_key: str | None = None
    openrouter_model: str | None = None
    openrouter_fast_model: str | None = None
    openrouter_image_model: str | None = None
    openrouter_vision_model: str | None = None


@router.get("/admin/settings")
def get_admin_settings(_creator: dict = Depends(require_creator)) -> dict:
    """Creator only: current secrets/integration settings (API key masked)."""
    return _settings_payload()


@router.post("/admin/settings")
def update_admin_settings(
    body: AdminSettingsBody, _creator: dict = Depends(require_creator)
) -> dict:
    """Super Admin: save the OpenRouter key + model ids to Firestore.

    Only provided fields are written. An empty string clears that override so the
    value reverts to the environment default. The raw key is never logged.
    """
    patch: dict[str, str] = {}
    for field in ("openrouter_api_key", *_MODEL_FIELDS):
        value = getattr(body, field)
        if value is None:
            continue
        patch[field] = value.strip()

    key = patch.get("openrouter_api_key")
    if key and not key.startswith("sk-"):
        raise HTTPException(400, "An OpenRouter API key should start with 'sk-'.")

    if patch:
        firestore_repo.set_app_config(patch)
        logger.info(
            "Creator %s updated settings: %s",
            _creator.get("email"),
            sorted(patch.keys()),  # keys only — never the secret values
        )
    return _settings_payload()


@router.post("/admin/settings/test")
def test_openrouter_key(_creator: dict = Depends(require_creator)) -> dict:
    """Super Admin: verify the effective OpenRouter key against OpenRouter's
    free ``/key`` endpoint (no token cost), so you can confirm it works."""
    key = runtime_config.get("openrouter_api_key")
    if not key:
        raise HTTPException(400, "No OpenRouter key is set.")
    try:
        resp = httpx.get(
            f"{settings.openrouter_base_url}/key",
            headers={"Authorization": f"Bearer {key}"},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach OpenRouter: {exc}") from exc
    if resp.status_code == 200:
        data = resp.json().get("data", {})
        return {
            "ok": True,
            "label": data.get("label"),
            "usage": data.get("usage"),
            "limit": data.get("limit"),
            "is_free_tier": data.get("is_free_tier"),
        }
    raise HTTPException(400, f"OpenRouter rejected the key (HTTP {resp.status_code}).")


@router.get("/admin/users")
def list_users(_admin: dict = Depends(require_admin)) -> dict:
    """Super Admin: directory of everyone registered on the chatbot."""
    users = firestore_repo.list_users()
    safe = [
        {
            "id": u["id"],
            "email": u.get("email", ""),
            "name": u.get("name", ""),
            "picture": u.get("picture", ""),
            "provider": u.get("provider", "google"),
            "created_at": u.get("created_at", ""),
            "last_login": u.get("last_login", ""),
        }
        for u in users
    ]
    return {"users": safe, "total": len(safe)}


@router.get("/admin/analytics")
def analytics(_admin: dict = Depends(require_admin)) -> dict:
    """Super Admin: month-on-month creative-request volume + breakdowns."""
    events = firestore_repo.list_creative_events()

    by_month: Counter[str] = Counter()
    by_brand: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    month_brand: dict[str, Counter[str]] = defaultdict(Counter)

    for ev in events:
        month = ev.get("year_month", "unknown")
        brand = ev.get("brand") or "Unbranded"
        category = ev.get("category") or "other"
        by_month[month] += 1
        by_brand[brand] += 1
        by_category[category] += 1
        month_brand[month][brand] += 1

    months = sorted(by_month.keys())
    return {
        "total_requests": len(events),
        "monthly": [
            {
                "month": m,
                "count": by_month[m],
                "by_brand": dict(month_brand[m]),
            }
            for m in months
        ],
        "by_brand": dict(by_brand.most_common()),
        "by_category": dict(by_category.most_common()),
    }
