"""Persistence gate: Firestore in cloud mode, local JSON files when SEO_OFFLINE=1.

Doc ids use ``-`` separators only (``run-{brand}``, ``todos-{brand}``, ``brands``)
so the local fallback can map them 1:1 to Windows-safe filenames.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_COLLECTION = "seo_geo"


def use_cloud() -> bool:
    return os.environ.get("SEO_OFFLINE", "0") != "1"


def _local_dir() -> Path:
    raw = os.environ.get("SEO_LOCAL_DIR", "")
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
