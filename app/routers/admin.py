import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.security import get_current_user, require_admin, require_creator
from app.services import agent_config, firestore_repo, runtime_config

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
        # Curated model choices so the UI offers dropdowns instead of free text.
        "catalog": agent_config.MODEL_CATALOG,
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


# --------------------------------------------------------------------------- #
# Agent configuration (creator-only): per-agent model overrides.
# --------------------------------------------------------------------------- #

def _agents_payload() -> dict:
    """Every agent with its per-agent overrides and the resolved (effective)
    model for each field, plus the curated catalog the UI offers as dropdowns."""
    overrides = firestore_repo.get_app_config(use_cache=False)
    agents_cfg = overrides.get("agents") or {}
    fields = runtime_config.AGENT_OVERRIDE_FIELDS

    agents = []
    for agent in agent_config.AGENTS:
        agent_id = str(agent["id"])
        saved = agents_cfg.get(agent_id) or {}
        agents.append(
            {
                **agent,
                # The value explicitly chosen for this agent ("" / missing = inherit global).
                "overrides": {f: saved.get(f, "") for f in fields},
                # What this agent will actually use right now (agent → global → env).
                "effective": {
                    f: runtime_config.get_for_agent(agent_id, f) for f in fields
                },
            }
        )

    return {
        "agents": agents,
        "fields": list(fields),
        "catalog": agent_config.MODEL_CATALOG,
        # The platform-wide fallback shown as the "inherit" option per field.
        "global_defaults": {f: runtime_config.get(f) for f in fields},
    }


class AgentConfigBody(BaseModel):
    # None = leave untouched; "" = clear the override (fall back to global).
    openrouter_model: str | None = None
    openrouter_fast_model: str | None = None
    openrouter_image_model: str | None = None
    openrouter_vision_model: str | None = None


@router.get("/admin/agents")
def get_agent_config(_creator: dict = Depends(require_creator)) -> dict:
    """Creator only: per-agent model configuration + the model catalog."""
    return _agents_payload()


@router.post("/admin/agents/{agent_id}")
def update_agent_config(
    agent_id: str,
    body: AgentConfigBody,
    _creator: dict = Depends(require_creator),
) -> dict:
    """Creator only: save per-agent model overrides for ``agent_id``.

    Only provided fields are written; an empty string clears that override so the
    agent reverts to the global default. A chosen model must exist in the curated
    catalog for that field — this prevents typos that silently break generation.
    """
    if agent_id not in agent_config.AGENT_IDS:
        raise HTTPException(404, f"Unknown agent '{agent_id}'.")

    patch: dict[str, str] = {}
    for field in runtime_config.AGENT_OVERRIDE_FIELDS:
        value = getattr(body, field)
        if value is None:
            continue
        value = value.strip()
        if value:
            allowed = {str(m["id"]) for m in agent_config.MODEL_CATALOG.get(field, [])}
            if value not in allowed:
                raise HTTPException(400, f"'{value}' is not an allowed {field}.")
        patch[field] = value

    if patch:
        firestore_repo.set_agent_config(agent_id, patch)
        logger.info(
            "Creator %s updated agent %s config: %s",
            _creator.get("email"),
            agent_id,
            sorted(patch.keys()),
        )
    return _agents_payload()


# --------------------------------------------------------------------------- #
# News banner: a single announcement the creator writes; every signed-in user
# sees it scroll across the top bar. Stored on the global app-config doc.
# --------------------------------------------------------------------------- #

class NewsBody(BaseModel):
    text: str = ""


def _news_payload() -> dict:
    cfg = firestore_repo.get_app_config(use_cache=False)
    news = cfg.get("news_banner") or {}
    if isinstance(news, str):  # tolerate a legacy plain-string value
        news = {"text": news}
    return {"text": news.get("text", ""), "updated_at": news.get("updated_at", "")}


@router.get("/news")
def get_news(_user: dict = Depends(get_current_user)) -> dict:
    """Any signed-in user: the current announcement banner (set by the creator)."""
    return _news_payload()


@router.post("/news")
def update_news(body: NewsBody, _creator: dict = Depends(require_creator)) -> dict:
    """Creator only: set (or clear, with empty text) the announcement banner."""
    text = body.text.strip()
    firestore_repo.set_app_config(
        {"news_banner": {"text": text, "updated_at": firestore_repo._now()}}
    )
    logger.info("Creator %s updated news banner (%d chars)", _creator.get("email"), len(text))
    return _news_payload()


# --------------------------------------------------------------------------- #
# Usage dashboard (Home): per-agent activity + a daily creatives/sessions graph.
# Per-user by default; the creator may request scope=all (everyone aggregated).
# --------------------------------------------------------------------------- #

def _display_name_from_email(email: str) -> str:
    """Fallback display name for the leaderboard when no profile name exists:
    turn ``vishal.tiwari@…`` into ``Vishal Tiwari``."""
    local = (email or "").split("@")[0]
    parts = [p for p in local.replace(".", " ").replace("_", " ").split() if p]
    return " ".join(p.capitalize() for p in parts) or (email or "Unknown")


def _usage_payload(user_id: str | None, days: int) -> dict:
    days = max(1, min(days, 365))
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    events = firestore_repo.list_usage_events(user_id, start.isoformat())

    agent_sessions: Counter[str] = Counter()   # per-agent tile count
    agent_creatives: Counter[str] = Counter()  # assets produced per agent
    day_sessions: Counter[str] = Counter()
    day_creatives: Counter[str] = Counter()
    user_sessions: Counter[str] = Counter()    # per-user leaderboard: runs
    user_creatives: Counter[str] = Counter()   # per-user leaderboard: assets
    user_agents: defaultdict[str, set] = defaultdict(set)  # distinct agents/user
    user_email: dict[str, str] = {}
    for ev in events:
        agent_id = ev.get("agent_id") or ev.get("category") or "unknown"
        day = ev.get("day") or str(ev.get("created_at", ""))[:10]
        count = int(ev.get("count", 1) or 1)
        uid = str(ev.get("user_id") or "unknown")
        user_email.setdefault(uid, ev.get("email") or "")
        user_agents[uid].add(agent_id)
        if ev.get("action") == "session":
            agent_sessions[agent_id] += 1
            day_sessions[day] += 1
            user_sessions[uid] += 1
        else:  # "generate" (or legacy events without an action)
            agent_creatives[agent_id] += count
            day_creatives[day] += count
            user_creatives[uid] += count

    per_agent = [
        {
            "agent_id": str(agent["id"]),
            "name": agent["name"],
            "role": agent.get("role", ""),
            "category": agent.get("category", "design"),
            "live": bool(agent.get("live")),
            "sessions": int(agent_sessions.get(str(agent["id"]), 0)),
            "creatives": int(agent_creatives.get(str(agent["id"]), 0)),
        }
        for agent in agent_config.AGENTS
    ]

    # Per-user leaderboard. Real profile names/pictures are only worth the extra
    # Firestore read on the creator's all-users view; the per-user view is a
    # single row, so fall back to an email-derived name there.
    directory: dict[str, dict] = {}
    if user_id is None:
        directory = {str(u["id"]): u for u in firestore_repo.list_users()}

    per_user = []
    for uid in user_agents:
        profile = directory.get(uid, {})
        email = profile.get("email") or user_email.get(uid, "")
        per_user.append(
            {
                "user_id": uid,
                "email": email,
                "name": profile.get("name") or _display_name_from_email(email),
                "picture": profile.get("picture", ""),
                "sessions": int(user_sessions.get(uid, 0)),
                "creatives": int(user_creatives.get(uid, 0)),
                "agents_used": len(user_agents.get(uid, set())),
            }
        )
    per_user.sort(
        key=lambda u: (u["sessions"], u["creatives"], u["agents_used"]), reverse=True
    )

    # Continuous day-by-day series so idle days render as zero on the chart.
    daily = []
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        daily.append(
            {
                "day": d,
                "creatives": int(day_creatives.get(d, 0)),
                "sessions": int(day_sessions.get(d, 0)),
            }
        )

    return {
        "days": days,
        "scope": "all" if user_id is None else "me",
        "per_agent": per_agent,
        "per_user": per_user,
        "daily": daily,
        "totals": {
            "sessions": int(sum(agent_sessions.values())),
            "creatives": int(sum(agent_creatives.values())),
            "active_days": sum(1 for d in daily if d["creatives"] or d["sessions"]),
        },
    }


@router.get("/usage")
def get_usage(
    days: int = 30, scope: str = "me", user: dict = Depends(get_current_user)
) -> dict:
    """Home dashboard data. Default = the caller's own activity; ``scope=all``
    (creator only) aggregates every user's activity together."""
    if scope == "all":
        if not user.get("is_creator"):
            raise HTTPException(403, "The all-users view is available to the creator only.")
        return _usage_payload(None, days)
    return _usage_payload(str(user["id"]), days)


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


# --------------------------------------------------------------------------- #
# Database viewer (Super Admin): read-only inspection of the Firestore
# collections, rendered as tables in the admin UI. Built so the team can *see*
# that data is genuinely flowing into the database — visual proof — without
# needing direct access to the GCP console.
# --------------------------------------------------------------------------- #

# Field names whose values must never be shown in full, wherever they appear
# (top-level or nested). Secrets are masked to a recognisable hint instead.
_SENSITIVE_FIELDS = {"openrouter_api_key", "api_key", "google_sub", "jwt_secret"}

# Long text/blobs are truncated per cell so a single huge field (e.g. a base64
# string) can't bloat the table; the truncation is signalled in the value.
_MAX_CELL_CHARS = 2000

# Columns surfaced first (when present) so the most useful fields lead the table;
# everything else follows alphabetically.
_PREFERRED_COLUMNS = (
    "id", "email", "name", "action", "agent_name", "status", "stage",
    "brand", "brand_name", "title", "file_name", "file_type", "agent_id",
    "category", "request_count", "user_id", "session_id", "brand_id", "ip",
    "started_at", "last_seen_at", "created_at", "updated_at", "last_login",
    "day", "year_month", "artifact",
)


def _sanitize(value, *, key: str = ""):
    """Make a Firestore value JSON-safe, redact secrets, and cap blob sizes."""
    if value and key in _SENSITIVE_FIELDS and isinstance(value, str):
        return _mask(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _sanitize(v, key=k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    if isinstance(value, str) and len(value) > _MAX_CELL_CHARS:
        return f"{value[:_MAX_CELL_CHARS]}… (+{len(value) - _MAX_CELL_CHARS} chars)"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)  # bytes, GeoPoint, DocumentReference, etc.


def _ordered_columns(rows: list[dict]) -> list[str]:
    """Union of all field names across rows: preferred fields first, then the
    rest alphabetically — so the table layout is stable run to run."""
    keys: set[str] = set()
    for row in rows:
        keys.update(row.keys())
    preferred = [c for c in _PREFERRED_COLUMNS if c in keys]
    rest = sorted(k for k in keys if k not in _PREFERRED_COLUMNS)
    return preferred + rest


@router.get("/admin/db/collections")
def list_db_collections(_admin: dict = Depends(require_admin)) -> dict:
    """Super Admin: the inspectable collections with their live document counts.

    ``count`` is ``null`` for any collection whose count couldn't be read; if
    *every* count is null we report ``connected: false`` so the UI can say
    "couldn't reach the database" instead of implying everything is empty.
    """
    collections = [
        {**c, "count": firestore_repo.count_collection(c["name"])}
        for c in firestore_repo.VIEWABLE_COLLECTIONS
    ]
    connected = any(c["count"] is not None for c in collections)
    return {
        "collections": collections,
        "connected": connected,
        "database": settings.firestore_database,
        "project": settings.gcp_project_id,
    }


@router.get("/admin/db/collections/{name}")
def get_db_collection(
    name: str, limit: int = 50, _admin: dict = Depends(require_admin)
) -> dict:
    """Super Admin: up to ``limit`` documents from one collection, sanitised and
    shaped into columns/rows the UI renders as a table (secrets masked)."""
    if not firestore_repo.is_viewable_collection(name):
        raise HTTPException(404, f"Collection '{name}' is not available to view.")
    limit = max(1, min(limit, 500))
    try:
        raw = firestore_repo.list_collection_documents(name, limit=limit)
    except Exception as exc:  # surface the real reason instead of a blank 500
        logger.warning("DB viewer could not read '%s': %s", name, exc)
        raise HTTPException(
            502, f"Could not read '{name}' from the database: {exc}"
        ) from exc
    rows = [{k: _sanitize(v, key=k) for k, v in doc.items()} for doc in raw]
    meta = next(
        (c for c in firestore_repo.VIEWABLE_COLLECTIONS if c["name"] == name), {}
    )
    return {
        "name": name,
        "label": meta.get("label", name),
        "description": meta.get("description", ""),
        "count": firestore_repo.count_collection(name),
        "returned": len(rows),
        "limit": limit,
        "columns": _ordered_columns(rows),
        "rows": rows,
    }


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
