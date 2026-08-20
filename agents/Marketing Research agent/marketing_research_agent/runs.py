"""Run persistence — every report (and ingested dataset) is saved as a run.

Mirrors the Graphics Designer ``runs.py`` pattern: JSON on disk under an
env-overridable ``MR_RUNS_DIR`` (default ``<agent>/runs``), with Firestore used
when the backend is cloud-configured. Disk is always written as the source of
truth for local/offline operation.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger("agentos.mr.runs")

_DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "runs"
_MR_COLLECTION = "mr_runs"


class RunStoreError(RuntimeError):
    """The durable run store could not be read.

    Raised instead of returning the disk-only (usually empty) list. ``mr_runs``
    is the only copy of parsed tracker state, so "Firestore is unreachable" and
    "this workspace has no data" used to arrive at the caller as the same empty
    list — the dashboard said "no data yet" during an outage, and the sheet-pull
    swap computed its superseded set from a list that was missing every durable
    run. Callers answer honestly (the HTTP layer turns this into a 502).
    """


def _root() -> Path:
    # Re-read env on each call so tests can monkeypatch MR_RUNS_DIR.
    root = Path(os.environ.get("MR_RUNS_DIR") or _DEFAULT_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _use_cloud() -> bool:
    if os.environ.get("MR_OFFLINE") == "1":
        return False
    try:
        # Same source of truth firestore_repo connects with (GCP_PROJECT_ID env);
        # Cloud Run does NOT set GOOGLE_CLOUD_PROJECT/GCP_PROJECT.
        from app.config import settings
        from app.services import firestore_repo  # noqa: F401

        return bool(settings.gcp_project_id)
    except Exception:
        return False


def _collection():
    from app.services import firestore_repo

    return firestore_repo._db().collection(_MR_COLLECTION)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _path(run_id: str) -> Path:
    return _root() / f"{run_id}.json"


def save_run(run: dict) -> bool:
    """Write a run, returning True when it reached its DURABLE store.

    Offline/local deployments have no cloud copy, so disk is the durable store
    and the answer is always True. When the backend is cloud-configured the
    Firestore document is the durable copy — Cloud Run's disk is ephemeral — so
    a failed ``set()`` means this run exists only on one instance's ``/tmp``.
    Callers that delete whatever this run supersedes MUST check the answer: a
    swap that deletes the durable original after a failed replacement write
    destroys the only copy that survives the next deploy.
    """
    payload = json.dumps(run, default=str, indent=2)
    _path(run["id"]).write_text(payload, encoding="utf-8")
    if not _use_cloud():
        return True
    try:
        # Same serialization as disk: dataset runs embed datetime.date
        # objects, which the Firestore client rejects.
        _collection().document(run["id"]).set(json.loads(payload))
    except Exception:  # disk still holds it; the caller decides what that is worth
        logger.warning("MR cloud save failed for run %s", run.get("id"))
        return False
    return True


def get_run(run_id: str) -> dict | None:
    p = _path(run_id)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    if _use_cloud():
        try:
            doc = _collection().document(run_id).get()
            return doc.to_dict() if doc.exists else None
        except Exception:
            return None
    return None


def delete_run(run_id: str) -> None:
    p = _path(run_id)
    if p.exists():
        p.unlink()
    if _use_cloud():
        try:
            _collection().document(run_id).delete()
        except Exception:
            logger.warning("MR cloud delete failed for run %s", run_id)


def _cloud_query(user_id: str | None = None, kind: str | tuple[str, ...] | None = None):
    """The ``mr_runs`` query for one workspace, filtered SERVER-side.

    ``mr_runs`` is shared by every workspace, so a bare ``.stream()`` billed and
    shipped every other user's runs on every read. Both filters are equality
    (``in`` is an equality set), so Firestore serves them from the automatic
    single-field indexes — no composite index is required. Deliberately no
    ``order_by``/``limit``: the disk copies merge in afterwards and the sort
    happens over the union, and a server-side ``limit`` without a server-side
    order would silently drop vendor datasets.
    """
    from google.cloud import firestore as _fs

    query = _collection()
    if user_id is not None:
        query = query.where(filter=_fs.FieldFilter("user_id", "==", user_id))
    if isinstance(kind, str):
        query = query.where(filter=_fs.FieldFilter("kind", "==", kind))
    elif kind:
        query = query.where(filter=_fs.FieldFilter("kind", "in", list(kind)))
    return query


def _cloud_list(user_id: str | None = None,
                kind: str | tuple[str, ...] | None = None) -> list[dict] | None:
    """Durable runs for this workspace, or ``None`` when the read FAILED.

    ``[]`` means the workspace genuinely has no runs. Same contract as
    ``firestore_repo.count_collection``."""
    try:
        return [d.to_dict() for d in _cloud_query(user_id, kind).stream()]
    except Exception:
        logger.warning("MR cloud list failed", exc_info=True)
        return None


def list_runs(user_id: str | None = None,
              kind: str | tuple[str, ...] | None = None) -> list[dict]:
    """Every run for ``user_id`` (newest first), durable copies merged with the
    local ones. Pass ``kind`` to have Firestore return only that kind.

    Raises :class:`RunStoreError` when the durable store could not be read."""
    by_id: dict[str, dict] = {}
    if _use_cloud():  # durable history first; local same-id copies override
        cloud = _cloud_list(user_id, kind)
        if cloud is None:
            raise RunStoreError("the saved-runs store could not be read")
        for run in cloud:
            if isinstance(run, dict) and run.get("id"):
                by_id[run["id"]] = run
    for p in _root().glob("*.json"):
        try:
            by_id[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    kinds = (kind,) if isinstance(kind, str) else kind
    out = []
    for run in by_id.values():
        if user_id is not None and run.get("user_id") != user_id:
            continue
        # Re-applied in Python because the disk copies above bypass the query.
        if kinds is not None and run.get("kind") not in kinds:
            continue
        out.append(run)
    out.sort(key=lambda r: r.get("generated_at") or "", reverse=True)
    return out
