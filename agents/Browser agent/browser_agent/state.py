"""Persistence gate: Firestore in cloud mode, local JSON when BROWSER_OFFLINE=1.

Doc ids use ``-`` separators only (``run-{id}``, ``runs-index``) so the local
fallback maps them 1:1 to Windows-safe filenames. Same pattern as
``seo_geo_agent/state.py``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_COLLECTION = "browser_agent"


def _load_local_env() -> None:
    """Export BROWSER_* keys from backend/.env into os.environ (local dev only).

    The app's pydantic settings read .env into the settings object, not the
    process env — but this agent reads os.environ so the same code works on
    Cloud Run. setdefault keeps real env vars (and test monkeypatching)
    authoritative.
    """
    env_file = Path(__file__).resolve().parents[3] / ".env"
    if not env_file.is_file():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("BROWSER_") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_local_env()


def use_cloud() -> bool:
    return os.environ.get("BROWSER_OFFLINE", "0") != "1"


def _local_dir() -> Path:
    raw = os.environ.get("BROWSER_LOCAL_DIR", "")
    base = Path(raw) if raw else Path(__file__).resolve().parent / "local_state"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _firestore_doc(doc_id: str):
    from app.services import firestore_repo

    return firestore_repo._db().collection(_COLLECTION).document(doc_id)


def save(doc_id: str, data: dict) -> None:
    # JSON round-trip keeps payloads Firestore-safe (no dataclasses, dates, sets).
    payload = json.loads(json.dumps(data, default=str))
    if use_cloud():
        _firestore_doc(doc_id).set(payload)
    else:
        (_local_dir() / f"{doc_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def load(doc_id: str) -> dict | None:
    if use_cloud():
        snap = _firestore_doc(doc_id).get()
        return snap.to_dict() if snap.exists else None
    path = _local_dir() / f"{doc_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def delete(doc_id: str) -> None:
    if use_cloud():
        _firestore_doc(doc_id).delete()
    else:
        path = _local_dir() / f"{doc_id}.json"
        if path.is_file():
            path.unlink()
