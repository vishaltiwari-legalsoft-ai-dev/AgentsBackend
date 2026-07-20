"""Persistence — mirrors marketing_research_agent/runs.py: JSON on disk under
env-overridable SEO_RUNS_DIR (source of truth), best-effort Firestore mirror
when the backend is cloud-configured. SEO_OFFLINE=1 forces disk-only (tests)."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

logger = logging.getLogger("agentos.seo.store")

_DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "runs"
_BENCH_COLLECTION = "seo_benchmarks"
_GEO_COLLECTION = "seo_geo_runs"
_META_FIELDS = ("id", "keyword", "location", "brand", "created_at",
                "serp_fetched_at", "topics_ai")

# In-process TTL cache for load_config() (I5): the /score hot path calls it on
# every debounced keystroke-pause, and it shouldn't hit Firestore that often.
# (monotonic_timestamp, config) | None. Invalidated by save_config().
_config_cache: tuple[float, dict] | None = None
_CONFIG_CACHE_TTL_S = 15.0


def _root(sub: str) -> Path:
    root = Path(os.environ.get("SEO_RUNS_DIR") or _DEFAULT_ROOT) / sub
    root.mkdir(parents=True, exist_ok=True)
    return root


def _use_cloud() -> bool:
    if os.environ.get("SEO_OFFLINE") == "1":
        return False
    try:
        from app.config import settings
        from app.services import firestore_repo  # noqa: F401

        return bool(settings.gcp_project_id)
    except Exception:
        return False


def _collection(name: str):
    from app.services import firestore_repo

    return firestore_repo._db().collection(name)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _save(sub: str, collection: str, doc: dict) -> None:
    payload = json.dumps(doc, default=str, indent=2)
    (_root(sub) / f"{doc['id']}.json").write_text(payload, encoding="utf-8")
    if _use_cloud():
        try:
            _collection(collection).document(doc["id"]).set(json.loads(payload))
        except Exception:  # cloud is best-effort; disk is source of truth
            logger.warning("SEO cloud save failed for %s/%s", collection, doc.get("id"))


def _cloud_doc(collection: str, doc_id: str) -> dict | None:
    """Best-effort Firestore point read: cloud is a mirror, so any failure
    (unconfigured, offline, permission error) degrades to None, never raises."""
    try:
        doc = _collection(collection).document(doc_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception:
        logger.warning("SEO cloud read failed for %s/%s", collection, doc_id)
        return None


def _cloud_docs(collection: str) -> list[dict]:
    """Best-effort Firestore collection scan; [] (never raises) on failure."""
    try:
        return [d.to_dict() for d in _collection(collection).stream()]
    except Exception:
        logger.warning("SEO cloud list failed for %s", collection)
        return []


def _load(sub: str, collection: str, doc_id: str) -> dict | None:
    """Disk copy first (fast, no round-trip); on Cloud Run the disk is
    ephemeral, so a doc missing on disk (fresh instance, restart) falls back
    to the durable Firestore mirror when cloud-configured."""
    path = _root(sub) / f"{doc_id}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    if _use_cloud():
        return _cloud_doc(collection, doc_id)
    return None


def _load_all(sub: str, collection: str) -> list[dict]:
    """Union of disk + cloud docs, keyed by id. Firestore is the durable
    history (older instances' runs may no longer be on this instance's
    ephemeral disk), so a cloud copy wins over a same-id disk copy."""
    by_id: dict[str, dict] = {}
    for p in _root(sub).glob("*.json"):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if doc.get("id"):
            by_id[doc["id"]] = doc
    if _use_cloud():
        for doc in _cloud_docs(collection):
            if isinstance(doc, dict) and doc.get("id"):
                by_id[doc["id"]] = doc  # cloud wins on id collision
    docs = list(by_id.values())
    docs.sort(key=lambda d: d.get("created_at") or d.get("captured_at") or "", reverse=True)
    return docs


def save_benchmark(b: dict) -> None:
    _save("benchmarks", _BENCH_COLLECTION, b)


def get_benchmark(bid: str) -> dict | None:
    return _load("benchmarks", _BENCH_COLLECTION, bid)


def list_benchmarks() -> list[dict]:
    return [{k: d.get(k) for k in _META_FIELDS} for d in _load_all("benchmarks", _BENCH_COLLECTION)]


def save_geo_run(run: dict) -> None:
    _save("geo", _GEO_COLLECTION, run)


def get_geo_run(rid: str) -> dict | None:
    return _load("geo", _GEO_COLLECTION, rid)


def list_geo_runs(brand: str | None = None) -> list[dict]:
    runs = _load_all("geo", _GEO_COLLECTION)
    return [r for r in runs if brand is None or r.get("brand") == brand]


def _config_path() -> Path:
    return _root("") / "seo_config.json"


def load_config() -> dict:
    """Effective config overrides, cached in-process for _CONFIG_CACHE_TTL_S so
    the /score hot path (called on every debounced keystroke-pause) doesn't hit
    Firestore per call. ``save_config`` invalidates the cache immediately."""
    global _config_cache
    now = time.monotonic()
    if _config_cache is not None and now - _config_cache[0] < _CONFIG_CACHE_TTL_S:
        return _config_cache[1]
    cfg = _load_config_uncached()
    _config_cache = (now, cfg)
    return cfg


def _load_config_uncached() -> dict:
    if _use_cloud():
        try:
            doc = _collection("seo_config").document("global").get()
            if doc.exists:
                return doc.to_dict() or {}
        except Exception:
            logger.warning("SEO config cloud read failed; using disk")
    path = _config_path()
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_config(overrides: dict) -> None:
    global _config_cache
    _config_path().write_text(json.dumps(overrides, indent=2), encoding="utf-8")
    if _use_cloud():
        try:
            _collection("seo_config").document("global").set(overrides)
        except Exception:
            logger.warning("SEO config cloud save failed; disk saved")
    _config_cache = None
