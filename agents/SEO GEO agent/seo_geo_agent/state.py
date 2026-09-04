"""Persistence gate: Firestore in cloud mode, local JSON files when SEO_OFFLINE=1.

Doc ids use ``-`` separators only (``run-{brand}``, ``todos-{brand}``, ``brands``)
so the local fallback can map them 1:1 to Windows-safe filenames.

Three primitives: :func:`load` / :func:`save` / :func:`delete` for documents only
one writer ever touches, and :func:`mutate` for the ones two callers can write at
the same time. ``mutate`` lives here, next to the collection it transacts over,
because a second implementation of "read-modify-write, atomically" is how one of
the two ends up non-transactional; ``final_geo_agent.geo_store`` — which is where
this code was written, for the GEO spend counters — now forwards to it.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

_COLLECTION = "seo_geo"

# Offline state is plain JSON files, so a process-local lock is the whole
# guarantee there; that is enough for tests and single-process local dev.
LOCAL_LOCK = threading.Lock()


def _load_local_env() -> None:
    """Export SEO_* keys from backend/.env into os.environ (local dev only).

    The app's pydantic settings read .env into the settings object, not the
    process env — but this agent reads os.environ so the same code works on
    Cloud Run (real env vars). setdefault keeps real env vars (and test
    monkeypatching) authoritative. Only SEO_* keys are exported on purpose.
    """
    env_file = Path(__file__).resolve().parents[3] / ".env"
    if not env_file.is_file():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            # GOOGLE_CLIENT_* feeds the per-brand Search Console OAuth connect.
            if line.startswith(("SEO_", "GOOGLE_CLIENT_")) and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_local_env()


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


def mutate(doc_id: str, change: Callable[[dict], tuple[dict, Any]]) -> Any:
    """Atomic read-modify-write of one state doc; returns ``change``'s result.

    ``change(current) -> (new_doc, result)`` runs INSIDE the transaction and may
    be retried on contention, so it must be a pure function of ``current``.
    Raising from it aborts the write and propagates — that is how a caller says
    "this document is not in a state I may write over" (an id that already
    exists, a record that is gone) without a read-then-write race in front of
    the check.

    ``load`` + ``save`` around a shared document is a lost update: two people
    adding a brand to ``brands`` at the same time both read the same list, both
    append their own, and the second ``save`` overwrites the first — one brand
    silently gone, with nothing anywhere saying so.
    """
    if use_cloud():
        from google.cloud import firestore

        from app.services import firestore_repo

        ref = _firestore_doc(doc_id)
        transaction = firestore_repo._db().transaction()

        @firestore.transactional
        def _apply(txn) -> Any:
            snap = ref.get(transaction=txn)
            current = snap.to_dict() if snap.exists else {}
            new_doc, result = change(current or {})
            # same JSON round-trip ``save`` uses: no dataclasses/dates/sets
            txn.set(ref, json.loads(json.dumps(new_doc, default=str)))
            return result

        return _apply(transaction)

    with LOCAL_LOCK:
        new_doc, result = change(load(doc_id) or {})
        save(doc_id, new_doc)
        return result
