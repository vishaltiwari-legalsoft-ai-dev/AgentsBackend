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


def save_run(run: dict) -> None:
    payload = json.dumps(run, default=str, indent=2)
    _path(run["id"]).write_text(payload, encoding="utf-8")
    if _use_cloud():
        try:
            # Same serialization as disk: dataset runs embed datetime.date
            # objects, which the Firestore client rejects.
            _collection().document(run["id"]).set(json.loads(payload))
        except Exception:  # cloud write is best-effort; disk is source of truth
            logger.warning("MR cloud save failed for run %s", run.get("id"))


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


def _cloud_list() -> list[dict]:
    try:
        return [d.to_dict() for d in _collection().stream()]
    except Exception:
        logger.warning("MR cloud list failed")
        return []


def list_runs(user_id: str | None = None) -> list[dict]:
    by_id: dict[str, dict] = {}
    if _use_cloud():  # durable history first; local same-id copies override
        for run in _cloud_list():
            if isinstance(run, dict) and run.get("id"):
                by_id[run["id"]] = run
    for p in _root().glob("*.json"):
        try:
            by_id[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    out = []
    for run in by_id.values():
        if user_id is None or run.get("user_id") == user_id:
            out.append(run)
    out.sort(key=lambda r: r.get("generated_at") or "", reverse=True)
    return out
