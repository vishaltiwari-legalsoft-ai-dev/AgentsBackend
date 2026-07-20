"""Persistence — mirrors marketing_research_agent/runs.py: JSON on disk under
env-overridable SEO_RUNS_DIR (source of truth), best-effort Firestore mirror
when the backend is cloud-configured. SEO_OFFLINE=1 forces disk-only (tests)."""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger("agentos.seo.store")

_DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "runs"
_BENCH_COLLECTION = "seo_benchmarks"
_GEO_COLLECTION = "seo_geo_runs"
_META_FIELDS = ("id", "keyword", "location", "brand", "created_at",
                "serp_fetched_at", "topics_ai")


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


def _load(sub: str, doc_id: str) -> dict | None:
    path = _root(sub) / f"{doc_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_all(sub: str) -> list[dict]:
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in _root(sub).glob("*.json")]
    docs.sort(key=lambda d: d.get("created_at") or d.get("captured_at") or "", reverse=True)
    return docs


def save_benchmark(b: dict) -> None:
    _save("benchmarks", _BENCH_COLLECTION, b)


def get_benchmark(bid: str) -> dict | None:
    return _load("benchmarks", bid)


def list_benchmarks() -> list[dict]:
    return [{k: d.get(k) for k in _META_FIELDS} for d in _load_all("benchmarks")]


def save_geo_run(run: dict) -> None:
    _save("geo", _GEO_COLLECTION, run)


def get_geo_run(rid: str) -> dict | None:
    return _load("geo", rid)


def list_geo_runs(brand: str | None = None) -> list[dict]:
    runs = _load_all("geo")
    return [r for r in runs if brand is None or r.get("brand") == brand]


def _config_path() -> Path:
    return _root("") / "seo_config.json"


def load_config() -> dict:
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
    _config_path().write_text(json.dumps(overrides, indent=2), encoding="utf-8")
    if _use_cloud():
        try:
            _collection("seo_config").document("global").set(overrides)
        except Exception:
            logger.warning("SEO config cloud save failed; disk saved")
