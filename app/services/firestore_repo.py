"""Firestore data access (the metadata "Brain").

Collections:
- brands               brand detail objects
- creatives            files linked to a brand (stores GCS URLs, not bytes)
- reference_creatives  user-uploaded reference material
- users                application accounts

The client is created lazily so the server can boot before GCP is configured.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from google.cloud import firestore

from app.config import settings

_client: Optional[firestore.Client] = None

# Brands change only on ingest, so a short in-process cache keeps the opening
# brand picker instant and avoids re-hitting Firestore mid-conversation.
_BRANDS_TTL_SECONDS = 60.0
_brands_cache: tuple[float, list[dict[str, Any]]] | None = None


def _db() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client(
            project=settings.require("gcp_project_id"),
            database=settings.firestore_database,
        )
    return _client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Brands
# --------------------------------------------------------------------------- #

def list_brands(*, use_cache: bool = True) -> list[dict[str, Any]]:
    global _brands_cache
    if use_cache and _brands_cache and (time.monotonic() - _brands_cache[0]) < _BRANDS_TTL_SECONDS:
        return _brands_cache[1]
    docs = _db().collection("brands").order_by("brand_name").stream()
    brands = [doc.to_dict() | {"id": doc.id} for doc in docs]
    _brands_cache = (time.monotonic(), brands)
    return brands


def get_brand(brand_id: str) -> Optional[dict[str, Any]]:
    doc = _db().collection("brands").document(brand_id).get()
    return (doc.to_dict() | {"id": doc.id}) if doc.exists else None


def find_brand_by_name(name: str) -> Optional[dict[str, Any]]:
    query = (
        _db()
        .collection("brands")
        .where(filter=firestore.FieldFilter("brand_name_lower", "==", name.lower()))
        .limit(1)
    )
    for doc in query.stream():
        return doc.to_dict() | {"id": doc.id}
    return None


def _invalidate_brands_cache() -> None:
    global _brands_cache
    _brands_cache = None


def upsert_brand(brand_name: str, brand_metadata: dict[str, Any]) -> dict[str, Any]:
    _invalidate_brands_cache()
    existing = find_brand_by_name(brand_name)
    payload = {
        "brand_name": brand_name,
        "brand_name_lower": brand_name.lower(),
        "brand_metadata": brand_metadata,
    }
    if existing:
        _db().collection("brands").document(existing["id"]).set(payload, merge=True)
        return get_brand(existing["id"]) or (existing | payload)

    brand_id = uuid.uuid4().hex
    payload["created_at"] = _now()
    _db().collection("brands").document(brand_id).set(payload)
    return payload | {"id": brand_id}


# --------------------------------------------------------------------------- #
# Creatives
# --------------------------------------------------------------------------- #

def list_creatives_by_brand(brand_id: str, limit: int = 50) -> list[dict[str, Any]]:
    docs = (
        _db()
        .collection("creatives")
        .where(filter=firestore.FieldFilter("brand_id", "==", brand_id))
        .limit(limit)
        .stream()
    )
    return [doc.to_dict() | {"id": doc.id} for doc in docs]


_LOGO_IMAGE_EXTS = (".png", ".svg", ".jpg", ".jpeg", ".webp")


def find_brand_logo(brand_id: str) -> Optional[dict[str, Any]]:
    """Best-guess the brand's logo from its ingested creatives (None if unknown).

    Logos aren't explicitly tagged, so rank the human-curated (non-AgentOS) image
    assets: a "logo" in the file name wins, then SVG, then PNG (usually
    transparent), then any other image. Returns the creative record (carrying the
    ``gs://`` ``file_url``) so callers can sign it or download the bytes.
    """
    if not brand_id:
        return None
    candidates = [
        c
        for c in list_creatives_by_brand(brand_id, limit=500)
        if str(c.get("file_url", "")).startswith("gs://")
        and (c.get("creative_metadata") or {}).get("author", "") != "AgentOS"
        and _is_image_asset(c)
    ]
    if not candidates:
        return None
    return max(candidates, key=_logo_score)


def _is_image_asset(creative: dict[str, Any]) -> bool:
    name = (creative.get("file_name") or "").lower()
    ftype = (creative.get("file_type") or "").lower()
    return ftype.startswith("image/") or name.endswith(_LOGO_IMAGE_EXTS)


def _logo_score(creative: dict[str, Any]) -> int:
    name = (creative.get("file_name") or "").lower()
    ftype = (creative.get("file_type") or "").lower()
    score = 0
    if "logo" in name:
        score += 100
    if name.endswith(".svg") or ftype == "image/svg+xml":
        score += 20
    elif name.endswith(".png") or ftype == "image/png":
        score += 10
    return score


def count_creatives_by_brand(brand_id: str) -> int:
    """Return the real total of creatives for a brand (no limit)."""
    query = (
        _db()
        .collection("creatives")
        .where(filter=firestore.FieldFilter("brand_id", "==", brand_id))
        .count()
    )
    result = query.get()
    # `count()` returns a list of aggregation results; value is on the first.
    return int(result[0][0].value) if result and result[0] else 0


def _delete_collection(collection_name: str) -> int:
    """Delete every document in a Firestore collection. Returns deleted count."""
    deleted = 0
    batch = _db().batch()
    batch_size = 0
    for doc in _db().collection(collection_name).stream():
        batch.delete(doc.reference)
        batch_size += 1
        deleted += 1
        if batch_size >= 400:
            batch.commit()
            batch = _db().batch()
            batch_size = 0
    if batch_size > 0:
        batch.commit()
    return deleted


def delete_all_brands() -> int:
    """Wipe the entire brands collection."""
    _invalidate_brands_cache()
    return _delete_collection("brands")


def delete_all_creatives() -> int:
    """Wipe the entire creatives collection."""
    return _delete_collection("creatives")


def delete_ingested_creatives(brand_id: str) -> int:
    """Delete creatives previously written by the ingestion script (Marketing
    Team author), preserving AI-generated ones. Returns deleted count.
    Used to make re-running `python -m app.ingest` idempotent."""
    query = (
        _db()
        .collection("creatives")
        .where(filter=firestore.FieldFilter("brand_id", "==", brand_id))
        .where(
            filter=firestore.FieldFilter(
                "creative_metadata.author", "==", "Marketing Team"
            )
        )
    )
    deleted = 0
    batch = _db().batch()
    batch_size = 0
    for doc in query.stream():
        batch.delete(doc.reference)
        batch_size += 1
        deleted += 1
        if batch_size >= 400:
            batch.commit()
            batch = _db().batch()
            batch_size = 0
    if batch_size > 0:
        batch.commit()
    return deleted


def create_creative(
    brand_id: str,
    file_name: str,
    file_type: str,
    file_url: str,
    creative_metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    creative_id = uuid.uuid4().hex
    payload = {
        "brand_id": brand_id,
        "file_name": file_name,
        "file_type": file_type,
        "file_url": file_url,
        "creative_metadata": creative_metadata or {},
        "created_at": _now(),
    }
    _db().collection("creatives").document(creative_id).set(payload)
    return payload | {"id": creative_id}


# --------------------------------------------------------------------------- #
# Reference creatives (user uploads)
# --------------------------------------------------------------------------- #

def create_reference(user_id: str, file_name: str, file_url: str) -> dict[str, Any]:
    asset_id = uuid.uuid4().hex
    payload = {
        "user_id": user_id,
        "file_name": file_name,
        "file_url": file_url,
        "upload_timestamp": _now(),
    }
    _db().collection("reference_creatives").document(asset_id).set(payload)
    return payload | {"asset_id": asset_id}


def list_references_by_user(user_id: str) -> list[dict[str, Any]]:
    docs = (
        _db()
        .collection("reference_creatives")
        .where(filter=firestore.FieldFilter("user_id", "==", user_id))
        .stream()
    )
    return [doc.to_dict() | {"asset_id": doc.id} for doc in docs]


# --------------------------------------------------------------------------- #
# Users (Google sign-in)
# --------------------------------------------------------------------------- #

def get_user_by_email(email: str) -> Optional[dict[str, Any]]:
    query = (
        _db()
        .collection("users")
        .where(filter=firestore.FieldFilter("email", "==", email.lower()))
        .limit(1)
    )
    for doc in query.stream():
        return doc.to_dict() | {"id": doc.id}
    return None


def get_or_create_google_user(
    email: str, name: str, picture: str, google_sub: str
) -> dict[str, Any]:
    """Look up a user by email, creating one on first Google sign-in.

    Always refreshes last_login so the admin directory shows recency.
    """
    existing = get_user_by_email(email)
    if existing:
        _db().collection("users").document(existing["id"]).set(
            {"last_login": _now(), "name": name, "picture": picture}, merge=True
        )
        return existing | {"name": name, "picture": picture}

    user_id = uuid.uuid4().hex
    payload = {
        "email": email.lower(),
        "name": name,
        "picture": picture,
        "google_sub": google_sub,
        "provider": "google",
        "created_at": _now(),
        "last_login": _now(),
    }
    _db().collection("users").document(user_id).set(payload)
    return payload | {"id": user_id}


def list_users() -> list[dict[str, Any]]:
    docs = _db().collection("users").stream()
    users = [doc.to_dict() | {"id": doc.id} for doc in docs]
    users.sort(key=lambda u: u.get("created_at", ""), reverse=True)
    return users


# --------------------------------------------------------------------------- #
# Conversations (chat history)
# --------------------------------------------------------------------------- #

def create_conversation(user_id: str, title: str) -> dict[str, Any]:
    conv_id = uuid.uuid4().hex
    payload = {
        "user_id": user_id,
        "title": title[:120] or "New chat",
        "messages": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    _db().collection("conversations").document(conv_id).set(payload)
    return payload | {"id": conv_id}


def list_conversations(user_id: str) -> list[dict[str, Any]]:
    """Conversation summaries for a user (no message bodies), newest first."""
    docs = (
        _db()
        .collection("conversations")
        .where(filter=firestore.FieldFilter("user_id", "==", user_id))
        .stream()
    )
    convos = [
        {
            "id": doc.id,
            "title": (doc.to_dict() or {}).get("title", "Chat"),
            "updated_at": (doc.to_dict() or {}).get("updated_at", ""),
        }
        for doc in docs
    ]
    convos.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return convos


def get_conversation(conv_id: str, user_id: str) -> Optional[dict[str, Any]]:
    doc = _db().collection("conversations").document(conv_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    if data.get("user_id") != user_id:
        return None  # ownership check
    return data | {"id": doc.id}


def append_messages(conv_id: str, new_messages: list[dict[str, Any]]) -> None:
    """Append messages and bump updated_at (transactional read-modify-write)."""
    ref = _db().collection("conversations").document(conv_id)
    snapshot = ref.get()
    if not snapshot.exists:
        return
    data = snapshot.to_dict() or {}
    messages = data.get("messages", [])
    messages.extend(new_messages)
    ref.set({"messages": messages, "updated_at": _now()}, merge=True)


def set_conversation_title(conv_id: str, title: str) -> None:
    _db().collection("conversations").document(conv_id).set(
        {"title": title[:120]}, merge=True
    )


def delete_conversation(conv_id: str, user_id: str) -> bool:
    ref = _db().collection("conversations").document(conv_id)
    snapshot = ref.get()
    if not snapshot.exists or (snapshot.to_dict() or {}).get("user_id") != user_id:
        return False
    ref.delete()
    return True


# --------------------------------------------------------------------------- #
# Analytics (creative request events)
# --------------------------------------------------------------------------- #

def log_usage_event(
    user_id: str,
    email: str,
    agent_id: str,
    category: str,
    action: str,
    *,
    count: int = 1,
    brand: Optional[str] = None,
    engine: Optional[str] = None,
) -> None:
    """Record one usage event for the per-user Home dashboard + admin analytics.

    ``action`` is "session" (a run/conversation was started — the per-agent tile
    count) or "generate" (creatives were produced — ``count`` is how many). One
    document per event keeps the model flexible; the dashboard reads a per-user,
    date-windowed slice and aggregates in Python. Logging must never break the
    request it accompanies, so Firestore errors are swallowed.
    """
    now = datetime.now(timezone.utc)
    event_id = uuid.uuid4().hex
    try:
        _db().collection("creative_events").document(event_id).set(
            {
                "user_id": user_id,
                "email": email,
                "agent_id": agent_id,
                "category": category,
                "action": action,
                "count": int(count),
                "brand": brand,
                "engine": engine,
                "created_at": now.isoformat(),
                "day": now.strftime("%Y-%m-%d"),
                "year_month": now.strftime("%Y-%m"),
            }
        )
    except Exception:  # analytics is best-effort — never fail the user's action
        pass


def list_creative_events(limit: int = 5000) -> list[dict[str, Any]]:
    docs = _db().collection("creative_events").limit(limit).stream()
    return [doc.to_dict() for doc in docs]


def list_usage_events(
    user_id: Optional[str], since_iso: str, limit: int = 10000
) -> list[dict[str, Any]]:
    """Usage events at/after ``since_iso``. Pass ``user_id`` for one user's data
    (the per-user dashboard) or ``None`` for everyone (creator all-users view).

    The ``user_id``-filtered query needs a composite index on
    ``(user_id ASC, created_at ASC)`` — Firestore prints a one-click link to
    create it the first time the query runs.
    """
    try:
        col = _db().collection("creative_events")
        query = col.where(
            filter=firestore.FieldFilter("created_at", ">=", since_iso)
        )
        if user_id is not None:
            query = col.where(
                filter=firestore.FieldFilter("user_id", "==", user_id)
            ).where(filter=firestore.FieldFilter("created_at", ">=", since_iso))
        return [doc.to_dict() for doc in query.limit(limit).stream()]
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# App config (admin-editable runtime settings — single document)
# --------------------------------------------------------------------------- #
# A single ``app_config/global`` doc holds admin-set overrides for sensitive
# runtime config (the OpenRouter key + model ids). It lets the Super Admin manage
# these from the UI instead of Cloud Run env vars. Read through a short cache so
# the hot path (every LLM/image call) doesn't hit Firestore each time; writes
# invalidate it. Firestore failures fall back to {} so the app still boots off
# the environment.

_APP_CONFIG_TTL_SECONDS = 30.0
_app_config_cache: tuple[float, dict[str, Any]] | None = None


def get_app_config(*, use_cache: bool = True) -> dict[str, Any]:
    global _app_config_cache
    if (
        use_cache
        and _app_config_cache
        and (time.monotonic() - _app_config_cache[0]) < _APP_CONFIG_TTL_SECONDS
    ):
        return _app_config_cache[1]
    try:
        doc = _db().collection("app_config").document("global").get()
        data = doc.to_dict() if doc.exists else {}
    except Exception:  # Firestore unavailable/unconfigured — fall back to env.
        data = {}
    data = data or {}
    _app_config_cache = (time.monotonic(), data)
    return data


def set_app_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a patch into the global app-config doc and return the fresh state."""
    global _app_config_cache
    _db().collection("app_config").document("global").set(
        {**patch, "updated_at": _now()}, merge=True
    )
    _app_config_cache = None
    return get_app_config(use_cache=False)


def set_agent_config(agent_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Set per-agent model overrides under ``agents.{agent_id}`` and return the
    fresh global config.

    The merge is done explicitly in Python (read → merge → write the whole
    ``agents`` map) rather than relying on Firestore's nested-merge semantics, so
    the behaviour is identical whether or not Firestore is reachable in tests. An
    empty-string value clears that field's override so it reverts to the global /
    environment default.
    """
    current = get_app_config(use_cache=False)
    agents: dict[str, Any] = dict(current.get("agents") or {})
    agent_cfg: dict[str, Any] = dict(agents.get(agent_id) or {})
    for field, value in patch.items():
        if value == "" or value is None:
            agent_cfg.pop(field, None)
        else:
            agent_cfg[field] = value
    agents[agent_id] = agent_cfg
    return set_app_config({"agents": agents})
