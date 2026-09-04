"""Run persistence — every report (and ingested dataset) is saved as a run.

Mirrors the Graphics Designer ``runs.py`` pattern: JSON on disk under an
env-overridable ``MR_RUNS_DIR`` (default ``<agent>/runs``), with Firestore used
when the backend is cloud-configured. Disk is always written as the source of
truth for local/offline operation.

``save_run`` is also where the store's LIFECYCLE lives — see :data:`STATE_KINDS`
and :func:`_enforce_retention`. Nothing else bounded it: ``POST
/mr/reports/{kind}`` mints a fresh uuid run per call with no dedup, and on
Cloud Run the runs directory is an in-memory image overlay, so "unbounded disk"
is really unbounded memory.
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

#: Kinds that are workspace STATE rather than a deliverable, and so are exempt
#: from retention. ``mr_runs`` is the only copy of parsed tracker state: a
#: ``dataset`` run *is* the workspace's numbers, and ``_load_dataset`` reads
#: every one of them on every ``/mr/overview``, report build and board build.
#: Evicting any of these would blank the dashboard, so the sheet pull's
#: fetch-then-swap stays their only lifecycle — it retires a run once its
#: replacement is durably stored, which is a supersede, not a cap.
#:
#: Stated as an exemption rather than an allow-list of report kinds on purpose.
#: A report kind added tomorrow is bounded the day it ships instead of silently
#: joining the unbounded set, and ``runs.py`` needs no import of ``reports.py``
#: (which imports this module).
STATE_KINDS = frozenset({"dataset", "official_spend", "lead_analysis"})

#: How many runs of ONE kind ONE workspace keeps. Deliberately a count and not
#: an age: the cost this bounds — the linear ``cache_key`` scan in
#: ``reports._cached_board_run``, the ``list_runs`` disk glob, and the container
#: overlay each run occupies — is a function of how MANY runs exist, not how old
#: they are. An age TTL would also delete the entire history of a workspace that
#: went quiet for a quarter, which is the "re-opened months later" read failing
#: for no saving at all: a handful of old runs cost nothing.
_DEFAULT_RETENTION = 25


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


def retention_cap() -> int:
    """Runs of one kind one workspace keeps. ``MR_RUN_RETENTION_PER_KIND``
    overrides :data:`_DEFAULT_RETENTION`; anything unparseable, or below 1,
    falls back to the default rather than turning retention into a purge."""
    raw = (os.environ.get("MR_RUN_RETENTION_PER_KIND") or "").strip()
    if not raw:
        return _DEFAULT_RETENTION
    try:
        cap = int(raw)
    except ValueError:
        logger.warning("MR_RUN_RETENTION_PER_KIND is not a number (%r) — using %d",
                       raw, _DEFAULT_RETENTION)
        return _DEFAULT_RETENTION
    return cap if cap >= 1 else _DEFAULT_RETENTION


def _enforce_retention(run: dict) -> list[str]:
    """Retire this workspace's oldest runs of THIS kind past the cap.

    Returns the ids evicted — ``[]`` in the normal case, where the workspace is
    under the cap.

    Two properties carry the whole design:

    **Tenant scoping.** The candidate list comes from ``list_runs(user_id,
    kind=...)`` — the same call, with the same comparison, that this
    workspace's own read path uses, so eviction can only ever delete something
    this user would have seen listed. Each candidate is then re-checked against
    ``user_id`` before ``delete_run``, because ``delete_run`` takes a bare id
    and is unscoped: getting this wrong turns a retention policy into
    cross-tenant data loss, which is strictly worse than the growth it fixes.

    **Best effort, never fatal.** The run is already written by the time this
    runs. A store that cannot be read (``RunStoreError``) or a delete that fails
    leaves more runs than the cap — which is the old behaviour, not a new
    failure — and must never turn a successful save into an error.
    """
    kind = run.get("kind")
    user_id = run.get("user_id")
    if kind is None or kind in STATE_KINDS:
        return []
    if user_id is None or not str(user_id).strip():
        # An unstamped run belongs to no workspace, so there is no scope to
        # evict within. Leave it and say so — it is a defect upstream.
        logger.warning("MR run %s carries no user_id; retention skipped", run.get("id"))
        return []

    cap = retention_cap()
    try:
        mine = list_runs(user_id, kind=kind)
    except RunStoreError:
        logger.warning("MR retention skipped for kind %s: the run store could not be read",
                       kind)
        return []

    evicted: list[str] = []
    for old in mine[cap:]:
        old_id = old.get("id")
        if not old_id or old_id == run.get("id"):
            continue  # never the run we just wrote
        if old.get("user_id") != user_id:
            # Unreachable through the scoped list above. Kept as the second
            # lock: ``delete_run`` is unscoped, so nothing but this comparison
            # stands between a loosened query and another tenant's data.
            logger.error("MR retention refused to evict run %s: it belongs to "
                         "another workspace", old_id)
            continue
        try:
            delete_run(old_id)
        except Exception:  # a failed delete is over-retention, not a failed save
            logger.warning("MR retention could not evict run %s", old_id, exc_info=True)
            continue
        evicted.append(old_id)
    if evicted:
        logger.info("MR retention evicted %d %s run(s) past the cap of %d",
                    len(evicted), kind, cap)
    return evicted


def save_run(run: dict) -> bool:
    """Write a run, returning True when it reached its DURABLE store.

    Offline/local deployments have no cloud copy, so disk is the durable store
    and the answer is always True. When the backend is cloud-configured the
    Firestore document is the durable copy — Cloud Run's disk is ephemeral — so
    a failed ``set()`` means this run exists only on one instance's ``/tmp``.
    Callers that delete whatever this run supersedes MUST check the answer: a
    swap that deletes the durable original after a failed replacement write
    destroys the only copy that survives the next deploy.

    This is also the store's one choke point, so it is where retention is
    enforced — every kind, every route, including ``POST /mr/reports/{kind}``,
    which predates any guard and still mints a fresh uuid run per call.
    Retention runs only on a DURABLE write, for the reason above: trading a
    durable old run for an ephemeral new one is the same data loss the sheet
    pull's fetch-then-swap already refuses to risk.
    """
    payload = json.dumps(run, default=str, indent=2)
    _path(run["id"]).write_text(payload, encoding="utf-8")
    durable = True
    if _use_cloud():
        try:
            # Same serialization as disk: dataset runs embed datetime.date
            # objects, which the Firestore client rejects.
            _collection().document(run["id"]).set(json.loads(payload))
        except Exception:  # disk still holds it; the caller decides what that is worth
            logger.warning("MR cloud save failed for run %s", run.get("id"))
            durable = False
    if durable:
        try:
            _enforce_retention(run)
        except Exception:  # retention is never allowed to fail a save
            logger.warning("MR retention pass failed for run %s", run.get("id"),
                           exc_info=True)
    return durable


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
    single-field indexes — no composite index is required, and that is still
    true of the retention read, which is this same ``(user_id, kind)`` pair.
    Deliberately no ``order_by``/``limit``: the disk copies merge in afterwards
    and the sort happens over the union, and a server-side ``limit`` without a
    server-side order would silently drop vendor datasets.
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


def list_runs(user_id, kind: str | tuple[str, ...] | None = None) -> list[dict]:
    """Every run for ``user_id`` (newest first), durable copies merged with the
    local ones. Pass ``kind`` to have Firestore return only that kind.

    ``user_id`` is REQUIRED and may not be blank. It used to default to
    ``None``, and the Python filter read ``if user_id is not None`` — so
    ``list_runs()`` returned every tenant's runs. Unreachable through sign-in
    (``payload["sub"]`` is always a Firestore doc id) but one careless cron or
    backfill call site from being live, and this is now also the read that
    eviction decides from. A missing tenant key is a programming error, so it
    raises rather than quietly widening.

    Raises :class:`ValueError` for a missing or blank ``user_id``, and
    :class:`RunStoreError` when the durable store could not be read."""
    if user_id is None or not str(user_id).strip():
        raise ValueError(
            "list_runs needs the workspace it is reading for — a blank user_id "
            "would return every tenant's runs")
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
            local = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        # The same admission test the cloud half applies. MR_RUNS_DIR is shared
        # with ``profiles._cache_path`` (workbook_profiles*.json), so this
        # directory holds JSON that is not a run at all; without this it entered
        # the map keyed by filename and was only ever dropped by the user filter
        # below — which is not a filter eviction should ever have leaned on.
        if not isinstance(local, dict) or not local.get("id"):
            continue
        by_id[p.stem] = local
    kinds = (kind,) if isinstance(kind, str) else kind
    out = []
    for run in by_id.values():
        if run.get("user_id") != user_id:
            continue
        # Re-applied in Python because the disk copies above bypass the query.
        if kinds is not None and run.get("kind") not in kinds:
            continue
        out.append(run)
    out.sort(key=lambda r: r.get("generated_at") or "", reverse=True)
    return out
