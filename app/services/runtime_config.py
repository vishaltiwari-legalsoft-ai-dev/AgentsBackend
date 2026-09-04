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
    "gd_planner_model",
    "gd_polish_image_model",
    # GEO agent engine keys (see Settings.perplexity_api_key etc.)
    "perplexity_api_key",
    "gemini_api_key",
    "openai_api_key",
    # GEO agent search provider (DataForSEO) — both halves of one Basic-auth
    # credential, overridable so it can be rotated without a redeploy.
    "dataforseo_login",
    "dataforseo_password",
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
    "gd_planner_model",
    "gd_polish_image_model",
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


def resolve_for_agent(agent_id: str | None, field: str) -> tuple[str, str | None]:
    """``(effective value, layer that supplied it)`` for one agent's ``field``.

    The value is exactly what :func:`get_for_agent` returns — this is that same
    resolution with the winning layer reported alongside: ``"agent"`` (the
    per-agent override), ``"global"`` (the admin override), ``"env"`` (the
    environment/``settings``), or ``None`` when every layer is empty (the value
    is then ``""``). Kept here, next to the resolution itself, so a surface that
    reports the source (the agents health rollup) can never drift from the
    resolution the Agent Configuration panel applies.
    """
    if agent_id and field in AGENT_OVERRIDE_FIELDS:
        agents = _overrides().get("agents") or {}
        per_agent = agents.get(agent_id) or {}
        value = per_agent.get(field)
        if value:
            return str(value), "agent"
    if field in OVERRIDE_FIELDS:
        value = _overrides().get(field)
        if value:
            return str(value), "global"
    env_value = str(getattr(settings, field, "") or "")
    return env_value, ("env" if env_value else None)


def get_for_agent(agent_id: str | None, field: str) -> str:
    """Effective value for ``field`` as seen by a specific agent.

    Resolution order: a non-empty per-agent override (``agents.{agent_id}.{field}``)
    wins, otherwise the global override, otherwise the environment — one code
    path shared with :func:`resolve_for_agent`. Only
    :data:`AGENT_OVERRIDE_FIELDS` can be set per agent; anything else (e.g. the
    shared API key) resolves globally.
    """
    return resolve_for_agent(agent_id, field)[0]


def require(field: str) -> str:
    """Like :func:`get`, but raise a clear error if the value is empty."""
    value = get(field)
    if not value:
        raise RuntimeError(
            f'Missing required configuration "{field}". A Super Admin can set it '
            f"in Settings → Secrets, or it can be provided via the environment."
        )
    return value
