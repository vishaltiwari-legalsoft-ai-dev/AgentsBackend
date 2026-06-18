"""Effective runtime configuration: admin overrides layered over the environment.

Most config comes from environment variables (``app.config.settings``). A small
set of sensitive, frequently-changed fields — the OpenRouter API key and the
model ids — may instead be set by the Super Admin from the UI and stored in
Firestore (``app_config/global``). This module returns the *effective* value:
the Firestore override when set, otherwise the environment value.

Consumers (``app.services.openrouter`` and the Graphics-Designer image provider)
read through here instead of ``settings`` directly, so an admin-set key takes
effect with no redeploy — while a missing Firestore (e.g. local dev offline)
transparently falls back to ``.env``.
"""

from __future__ import annotations

from app.config import settings
from app.services import firestore_repo

# The only fields an admin may override at runtime (must exist on ``settings``).
OVERRIDE_FIELDS: tuple[str, ...] = (
    "openrouter_api_key",
    "openrouter_model",
    "openrouter_fast_model",
    "openrouter_image_model",
    "openrouter_vision_model",
)

# Model fields that may additionally be overridden *per agent* in the creator's
# Agent Configuration panel. The API key is intentionally NOT here — there is a
# single shared OpenRouter key for the whole platform. Per-agent overrides are
# stored under ``app_config/global``'s ``agents.{agent_id}`` map and layer on top
# of the global override, which in turn layers on top of the environment.
AGENT_OVERRIDE_FIELDS: tuple[str, ...] = (
    "openrouter_model",
    "openrouter_fast_model",
    "openrouter_image_model",
    "openrouter_vision_model",
)


def _overrides() -> dict:
    try:
        return firestore_repo.get_app_config() or {}
    except Exception:
        return {}


def get(field: str) -> str:
    """Effective value for ``field``: a non-empty Firestore override wins,
    otherwise the environment/``settings`` value. Always returns a string."""
    if field in OVERRIDE_FIELDS:
        value = _overrides().get(field)
        if value:  # empty/missing override → fall through to env
            return str(value)
    return str(getattr(settings, field, "") or "")


def get_for_agent(agent_id: str | None, field: str) -> str:
    """Effective value for ``field`` as seen by a specific agent.

    Resolution order: a non-empty per-agent override (``agents.{agent_id}.{field}``)
    wins, otherwise we fall back to :func:`get` (global override → environment).
    Only :data:`AGENT_OVERRIDE_FIELDS` can be set per agent; anything else (e.g.
    the shared API key) resolves globally.
    """
    if agent_id and field in AGENT_OVERRIDE_FIELDS:
        agents = _overrides().get("agents") or {}
        per_agent = agents.get(agent_id) or {}
        value = per_agent.get(field)
        if value:
            return str(value)
    return get(field)


def require(field: str) -> str:
    """Like :func:`get`, but raise a clear error if the value is empty."""
    value = get(field)
    if not value:
        raise RuntimeError(
            f'Missing required configuration "{field}". A Super Admin can set it '
            f"in Settings → Secrets, or it can be provided via the environment."
        )
    return value
