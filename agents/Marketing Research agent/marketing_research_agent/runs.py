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

import hashlib
import json
import logging
import os
import threading
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


# --- report templates (workspace-wide, append-only, versioned) ----------------
#
# Owner decisions (2026-10-08): a template applies to the WHOLE workspace; any
# member may upload or replace it; every version is kept for history and
# one-click revert; the built-in default is always available. The uploaded
# original (a client's sample PDF) is NOT kept anywhere: a version stores what
# it renders from (a layout ``spec`` or sanitized ``html``), the upload's
# ``filename`` and a ``sha256`` of the stored content, and nothing else.
#
# Stored as ordinary ``mr_runs`` docs — this module is the shared MR store, so
# templates inherit its tenancy (``user_id`` = the workspace key the router
# resolves with ``workspace.workspace_id``, so they follow ``MR_WORKSPACE_SHARED``)
# and its lifecycle (``_enforce_retention``: the newest :func:`retention_cap`
# records per workspace per report, 25 by default). Append-only: an activation
# (a revert, or "back to built-in") WRITES a new record, so the active template
# is always simply the newest record and retention can never evict it.
#
# Two kinds of record share the kind ``report_template:<report>``:
#
# * a CONTENT version — what someone saved. It gets the next ``number`` (1, 2,
#   3… per workspace, for "Version 5" in the UI), and its ``content_id`` is its
#   own id. ``uploaded_by``/``created_at`` are its author and time.
# * an ACTIVATION — a revert. It copies the content, keeps the content's
#   ``number``, ``content_id``, author and ``created_at``, and records who set it
#   active and when (``set_by``/``set_at``). A content version is also stamped
#   ``set_by``/``set_at`` = its own author and time: saving activates it.
#
# ``last_number`` rides on EVERY record (the highest number issued so far), so
# the next number survives retention evicting the record that issued the last
# one — the newest record is never evicted.
#
# One ``kind`` per report (``report_template:<report>``) so each report's history
# is retained on its own — under a shared kind, 25 uploads of one report's
# template would evict another report's ACTIVE template.
#
# Not in :data:`STATE_KINDS`, and not in ``reports.KINDS``, so ``GET /mr/runs``
# never lists a template as a saved report.

TEMPLATE_REPORTS = frozenset({"vendor_performance"})
TEMPLATE_SOURCE_KINDS = frozenset({"pdf", "image", "html", "builder"})
BUILTIN_TEMPLATE_ID = "builtin"

#: Firestore's document limit is 1 MiB; leave headroom for the envelope.
_TEMPLATE_MAX_BYTES = 900_000


def template_kind(report: str = "vendor_performance") -> str:
    if report not in TEMPLATE_REPORTS:
        raise ValueError(f"unknown report for templates: {report!r}")
    return f"report_template:{report}"


def _require_workspace(workspace) -> None:
    if workspace is None or not str(workspace).strip():
        raise ValueError("a template belongs to a workspace — a blank key would "
                         "write or read outside every tenant")


def builtin_template(report: str = "vendor_performance") -> dict:
    """The built-in default. Synthesised, never stored, always available."""
    return {"id": BUILTIN_TEMPLATE_ID, "kind": template_kind(report), "report": report,
            "builtin": True, "source_kind": None, "spec": None, "html": None,
            "sha256": None, "filename": None, "number": None, "content_id": BUILTIN_TEMPLATE_ID,
            "created_at": None, "uploaded_by": None, "set_by": None, "set_at": None,
            "reverted_from": None}


def _clean_person(value, what: str) -> str:
    if not value or not str(value).strip():
        raise ValueError(f"{what} is required — template history names who changed it")
    return str(value).strip()


def _display(name, fallback: str) -> str:
    """A person's display name for the history, or their email when there is
    none. Plain text, capped; the console escapes it."""
    clean = " ".join(str(name or "").split())[:120]
    return clean or fallback


# --- the head: one small pointer doc per workspace per report ------------------
#
# Every template write is ONE atomic step against a head doc (``mr_runs`` id
# :func:`_head_id`, kind ``report_template_head:<report>``) holding
# ``last_number``, the ordering floor and the ACTIVE record's metadata (no
# body). In the cloud that step is a Firestore transaction — read the head,
# assign the number, write the record and the head together — so two
# simultaneous saves can never share a number: the loser's commit is rejected
# and retried against the winner's head. Offline (disk) the same step runs
# under a process lock plus a lock file.
#
# Reads follow from it: "which template is active" is the head alone (1 small
# doc); a build reads the head and then the one active record (2 docs); the
# version list is a projection without the ``spec``/``html`` bodies.
#
# Template records are written ONLY through this path. In the cloud they are
# not mirrored to the instance's disk (the head is the source of truth, and a
# disk copy would let one instance serve a version the head does not know).

TEMPLATE_HEAD_PREFIX = "report_template_head"

#: Everything a template record carries except its BODY (``spec``/``html``).
_TEMPLATE_META_FIELDS = (
    "id", "kind", "user_id", "report", "generated_at", "created_at", "uploaded_by",
    "created_by_name", "source_kind", "sha256", "filename", "builtin", "reverted_from",
    "content_id", "number", "last_number", "set_by", "set_by_name", "set_at",
)

#: Seconds an offline lock file may be held before it is treated as abandoned.
_LOCAL_LOCK_STALE_S = 30.0
_LOCAL_LOCK = threading.Lock()


def _head_id(workspace, report: str) -> str:
    """Deterministic, path-safe and tenant-exact (``repr`` keeps ``7`` and
    ``"7"`` two workspaces, as everywhere else in MR)."""
    digest = hashlib.sha256(f"{report}\x00{workspace!r}".encode("utf-8")).hexdigest()
    return f"tplhead_{digest[:40]}"


def _meta(record: dict) -> dict:
    return {k: record.get(k) for k in _TEMPLATE_META_FIELDS}


def _head_for(workspace, report: str, record: dict) -> dict:
    return {"id": _head_id(workspace, report), "kind": f"{TEMPLATE_HEAD_PREFIX}:{report}",
            "user_id": workspace, "report": report,
            "last_number": record.get("last_number") or 0,
            "generated_at": record["generated_at"], "active": _meta(record)}


def _next_ordering(head: dict | None) -> str:
    """"Active" is "newest", so a new record must sort strictly after the current
    one even when the clock ties (coarse on some hosts) or skews."""
    from datetime import datetime, timedelta, timezone

    now_dt = datetime.now(timezone.utc)
    if head:
        try:
            floor = datetime.fromisoformat(head["generated_at"]) + timedelta(microseconds=1)
            now_dt = max(now_dt, floor)
        except (KeyError, TypeError, ValueError):
            pass
    return now_dt.isoformat()


def _check_size(record: dict) -> dict:
    payload = json.loads(json.dumps(record, default=str))
    if len(json.dumps(payload).encode("utf-8")) > _TEMPLATE_MAX_BYTES:
        raise ValueError("template version is larger than the store's document limit")
    return payload


def _own_head(head: dict | None, workspace) -> dict | None:
    if head is not None and head.get("user_id") != workspace:
        # A digest collision, or a doc written by something else. Never build
        # on another tenant's head.
        raise RunStoreError("the template head belongs to another workspace")
    return head


def _transactional(fn):
    """Seam over ``firestore.transactional`` (tests substitute a fake that
    enforces the same optimistic-concurrency contract)."""
    from google.cloud import firestore as _fs

    return _fs.transactional(fn)


def _cloud_commit(workspace, report: str, build) -> dict:
    from app.services import firestore_repo

    db = firestore_repo._db()
    col = db.collection(_MR_COLLECTION)
    head_ref = col.document(_head_id(workspace, report))
    out: dict = {}

    def body(txn):
        snap = head_ref.get(transaction=txn)
        head = _own_head(snap.to_dict() if snap.exists else None, workspace)
        record = build(head)               # pure: a retried attempt rebuilds it
        txn.set(col.document(record["id"]), record)
        txn.set(head_ref, _head_for(workspace, report, record))
        out["record"] = record

    _transactional(body)(db.transaction())
    return out["record"]


class _LocalFileLock:
    """Cross-process exclusion for the offline store (O_EXCL lock file)."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        import time

        deadline = time.monotonic() + 2 * _LOCAL_LOCK_STALE_S
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > _LOCAL_LOCK_STALE_S:
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise RunStoreError("the template store is locked by another writer")
                time.sleep(0.01)

    def __exit__(self, *_exc):
        self.path.unlink(missing_ok=True)


def _atomic_write(path: Path, payload: dict) -> None:
    import time

    tmp = path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    # Windows refuses to replace a file another thread or process has open for
    # reading (any ``list_runs`` glob may be reading it); that lasts
    # milliseconds, so wait it out briefly rather than fail the write.
    for attempt in range(100):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 99:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.005)


def _local_head(workspace, report: str) -> dict | None:
    p = _path(_head_id(workspace, report))
    if not p.exists():
        return None
    return _own_head(json.loads(p.read_text(encoding="utf-8")), workspace)


def _local_commit(workspace, report: str, build) -> dict:
    head_id = _head_id(workspace, report)
    with _LOCAL_LOCK, _LocalFileLock(_root() / f"{head_id}.lock"):
        record = build(_local_head(workspace, report))
        _atomic_write(_path(record["id"]), record)
        _atomic_write(_path(head_id), _head_for(workspace, report, record))
    return record


def _commit_template(workspace, report: str, build) -> dict:
    """Run ``build(head) -> record`` and write the record + head atomically.
    ``ValueError`` from ``build`` passes through; any store failure is a
    :class:`RunStoreError` and nothing was written."""
    if not _use_cloud():
        record = _local_commit(workspace, report, build)
    else:
        try:
            record = _cloud_commit(workspace, report, build)
        except (ValueError, RunStoreError):
            raise
        except Exception as exc:
            logger.warning("MR template commit failed", exc_info=True)
            raise RunStoreError("the template version could not be saved durably") from exc
    _trim_templates(workspace, report, keep_id=record["id"])
    return record


def _list_template_meta(workspace, report: str) -> list[dict]:
    """Every template record's METADATA for this workspace, newest first. One
    projected equality query on ``(user_id, kind)``: no composite index, no
    bodies on the wire."""
    kind = template_kind(report)
    rows: list[dict] = []
    if _use_cloud():
        try:
            from google.cloud import firestore as _fs

            query = (_collection()
                     .where(filter=_fs.FieldFilter("user_id", "==", workspace))
                     .where(filter=_fs.FieldFilter("kind", "==", kind))
                     .select(list(_TEMPLATE_META_FIELDS)))
            rows = [d.to_dict() for d in query.stream()]
        except Exception as exc:
            logger.warning("MR template list failed", exc_info=True)
            raise RunStoreError("the template store could not be read") from exc
    else:
        for p in _root().glob("*.json"):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(r, dict) and r.get("kind") == kind and r.get("user_id") == workspace:
                rows.append(_meta(r))
    rows = [r for r in rows if r.get("user_id") == workspace and r.get("id")]
    rows.sort(key=lambda r: r.get("generated_at") or "", reverse=True)
    return rows


def _trim_templates(workspace, report: str, *, keep_id: str) -> list[str]:
    """Retention: the newest :func:`retention_cap` records per workspace per
    report. Reads metadata only; never evicts ``keep_id`` (the record just
    written, which IS the active one) or anything outside this workspace.
    Best effort: over-retention, never a failed save."""
    try:
        rows = _list_template_meta(workspace, report)
    except RunStoreError:
        return []
    evicted = []
    for r in rows[retention_cap():]:
        if r.get("id") == keep_id or r.get("user_id") != workspace:
            continue
        try:
            delete_run(r["id"])
            evicted.append(r["id"])
        except Exception:
            logger.warning("MR template retention could not evict %s", r.get("id"))
    return evicted


def save_template_version(workspace, *, uploaded_by: str, source_kind: str | None = None,
                          spec: dict | None = None, html: str | None = None,
                          sha256: str | None = None, filename: str | None = None,
                          builtin: bool = False, uploaded_by_name: str | None = None,
                          report: str = "vendor_performance") -> dict:
    """Save a new CONTENT version (or, with ``builtin=True``, a "use the built-in
    default" record). It becomes the active template. Returns the record.

    The number and the write are one atomic step (see the head, above), so two
    simultaneous saves get two numbers. ``html`` must ALREADY be sanitized by
    the caller (this layer stores it, it does not clean it) and is accepted
    only for ``source_kind == "html"``.

    Cost: 1 head read + 2 writes in one transaction, then a projected retention
    read (metadata of <= 26 records).

    Raises ``ValueError`` for a blank workspace/author, an unknown report/source
    kind, or a payload over Firestore's document limit, and
    :class:`RunStoreError` when nothing could be written durably.
    """
    _require_workspace(workspace)
    kind = template_kind(report)
    who = _clean_person(uploaded_by, "uploaded_by")
    who_name = _display(uploaded_by_name, who)
    if builtin:
        source_kind, spec, html, sha256, filename = None, None, None, None, None
    else:
        if source_kind not in TEMPLATE_SOURCE_KINDS:
            raise ValueError(f"source_kind must be one of {sorted(TEMPLATE_SOURCE_KINDS)}")
        if html is not None and source_kind != "html":
            raise ValueError("html is stored only for source_kind 'html'")
        if spec is None and html is None:
            raise ValueError("a template version needs a spec or html")

    def build(head: dict | None) -> dict:
        now = _next_ordering(head)
        last = int((head or {}).get("last_number") or 0)
        number = None if builtin else last + 1
        record_id = new_run_id()
        return _check_size({
            "id": record_id, "kind": kind, "user_id": workspace, "report": report,
            "generated_at": now, "created_at": now, "uploaded_by": who,
            "created_by_name": who_name,
            "source_kind": source_kind, "spec": spec, "html": html, "sha256": sha256,
            "filename": filename, "builtin": bool(builtin), "reverted_from": None,
            "content_id": BUILTIN_TEMPLATE_ID if builtin else record_id,
            "number": number, "last_number": last if builtin else number,
            "set_by": who, "set_by_name": who_name, "set_at": now,
        })

    return _commit_template(workspace, report, build)


def list_template_versions(workspace, *, report: str = "vendor_performance",
                           limit: int | None = None) -> list[dict]:
    """This workspace's template records (content versions and activations),
    newest first, as METADATA: no ``spec``/``html`` body (load one with
    :func:`find_template_version`). At most :func:`retention_cap` exist,
    ``limit`` trims further. One projected equality query: no composite index."""
    _require_workspace(workspace)
    template_kind(report)
    rows = _list_template_meta(workspace, report)
    return rows if limit is None else rows[: max(limit, 0)]


def active_template_meta(workspace, *, report: str = "vendor_performance") -> dict:
    """Which template is active: the head alone, ONE small doc read, no body.
    :func:`builtin_template` when nothing was ever saved. Same keys as a record
    minus ``spec``/``html`` (``rt.summarize`` takes it as-is)."""
    _require_workspace(workspace)
    template_kind(report)
    if _use_cloud():
        try:
            snap = _collection().document(_head_id(workspace, report)).get()
            head = _own_head(snap.to_dict() if snap.exists else None, workspace)
        except RunStoreError:
            raise
        except Exception as exc:
            logger.warning("MR template head read failed", exc_info=True)
            raise RunStoreError("the template store could not be read") from exc
    else:
        head = _local_head(workspace, report)
    if not head or not head.get("active"):
        return builtin_template(report)
    return dict(head["active"])


def active_template(workspace, *, report: str = "vendor_performance") -> dict:
    """The active template WITH its body, for a build: the head, then the one
    active record (2 doc reads). A ``builtin=True`` activation is returned as
    its metadata with ``spec``/``html`` None, like :func:`builtin_template`."""
    meta = active_template_meta(workspace, report=report)
    if meta.get("builtin") or meta.get("id") == BUILTIN_TEMPLATE_ID:
        return {**meta, "spec": None, "html": None}
    record = find_template_version(workspace, meta["id"], report=report)
    if record is None:
        # Retention never evicts the active record, so this is a broken store.
        raise RunStoreError("the active template version could not be read")
    return record


def find_template_version(workspace, version_id: str, *,
                          report: str = "vendor_performance") -> dict | None:
    """One template record by id, WITH its body, or None, including when the id
    belongs to another workspace or another kind (so a caller cannot tell the
    two apart). A single document read; the workspace check is what scopes it."""
    _require_workspace(workspace)
    if not version_id or version_id == BUILTIN_TEMPLATE_ID:
        return None
    record = get_run(str(version_id))
    if (not isinstance(record, dict) or record.get("kind") != template_kind(report)
            or record.get("user_id") != workspace):
        return None
    return record


def find_template_content(workspace, version_id: str, *,
                          report: str = "vendor_performance") -> dict | None:
    """The template content a console version id names, WITH its body: the
    record itself, or — when retention evicted the original — an activation
    that copied it. None for another workspace's id, exactly like a missing one."""
    _require_workspace(workspace)
    if not version_id or version_id == BUILTIN_TEMPLATE_ID:
        return None
    return (find_template_version(workspace, version_id, report=report)
            or _find_by_content_id(workspace, version_id, report))


def _find_by_content_id(workspace, content_id: str, report: str) -> dict | None:
    """A record carrying ``content_id``: how a version whose ORIGINAL record
    retention evicted is still reachable through an activation that copied it.
    Equality-only on three fields (no composite index); limit 1, because every
    match is a copy of the same content."""
    kind = template_kind(report)
    if _use_cloud():
        try:
            from google.cloud import firestore as _fs

            query = (_collection()
                     .where(filter=_fs.FieldFilter("user_id", "==", workspace))
                     .where(filter=_fs.FieldFilter("kind", "==", kind))
                     .where(filter=_fs.FieldFilter("content_id", "==", content_id))
                     .limit(1))
            found = [d.to_dict() for d in query.stream()]
        except Exception as exc:
            raise RunStoreError("the template store could not be read") from exc
    else:
        found = []
        for p in _root().glob("*.json"):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (isinstance(r, dict) and r.get("kind") == kind
                    and r.get("content_id") == content_id):
                found.append(r)
    return next((r for r in found
                 if r.get("user_id") == workspace and not r.get("builtin")), None)


def revert_template(workspace, version_id: str, *, set_by: str,
                    set_by_name: str | None = None,
                    report: str = "vendor_performance") -> dict:
    """One-click revert: append an ACTIVATION of ``version_id`` (``"builtin"`` =
    back to the built-in default). The version is looked up inside THIS
    workspace only, so another workspace's id is not found: it raises
    ``LookupError`` exactly like a missing one.

    Cost: 1 read for the source (2 when found through its content id), then the
    same transaction as a save (1 head read + 2 writes)."""
    _require_workspace(workspace)
    who = _clean_person(set_by, "set_by")
    who_name = _display(set_by_name, who)
    kind = template_kind(report)
    if version_id == BUILTIN_TEMPLATE_ID:
        old = builtin_template(report)
    else:
        old = find_template_content(workspace, version_id, report=report)
        if old is None:
            raise LookupError(f"no template version {version_id!r} in this workspace")

    def build(head: dict | None) -> dict:
        now = _next_ordering(head)
        return _check_size({
            "id": new_run_id(), "kind": kind, "user_id": workspace, "report": report,
            "generated_at": now,
            # The CONTENT's author and time travel with it; the activation is
            # attributed separately, so history never claims B wrote A's template.
            "created_at": old.get("created_at"), "uploaded_by": old.get("uploaded_by"),
            "created_by_name": old.get("created_by_name") or old.get("uploaded_by"),
            "source_kind": old.get("source_kind"), "spec": old.get("spec"),
            "html": old.get("html"), "sha256": old.get("sha256"),
            "filename": old.get("filename"), "builtin": bool(old.get("builtin")),
            "reverted_from": version_id,
            "content_id": old.get("content_id") or old.get("id"),
            "number": old.get("number"),
            "last_number": int((head or {}).get("last_number") or 0),
            "set_by": who, "set_by_name": who_name, "set_at": now,
        })

    return _commit_template(workspace, report, build)


# --- template readings: the per-workspace daily allowance -----------------------
#
# Reading a sample report is one billed model call (``template_extract``). A
# workspace gets :data:`TEMPLATE_READINGS_PER_DAY` of them per UTC day.
#
# The allowance is ONE counter doc per workspace (``mr_runs`` id
# :func:`_meter_id`, kind :data:`TEMPLATE_METER_KIND`) holding ``{day, used}``,
# checked and incremented in one atomic step: a Firestore transaction in the
# cloud (the same ``_transactional`` seam the template head uses), the process
# lock plus a lock file offline. Two simultaneous readings therefore cannot both
# take the last slot, and nothing else — retention, a failed list, an evicted
# doc — can make the count read low. A new UTC day resets it in place: one doc
# per workspace, ever.
#
# Each reading ALSO leaves an audit doc of kind :data:`TEMPLATE_READING_KIND`
# (who, which file, ``ok`` / ``failed`` with the billed usage / ``error``) so a
# failed-but-billed call is on record. Those docs are history only — retention
# may trim them freely, because the counter alone decides.
#
# A reading that never reached the provider is given back (:func:`release`); one
# that was billed stays counted, whether it succeeded or not.

TEMPLATE_READING_KIND = "report_template_reading"
TEMPLATE_METER_KIND = "report_template_meter"
TEMPLATE_READINGS_PER_DAY = 10


class ReadingLimitReached(RuntimeError):
    """This workspace has spent today's readings. Nothing was written."""


def _utc_day(iso: str | None = None) -> str:
    from datetime import datetime, timezone

    if iso:
        return str(iso)[:10]
    return datetime.now(timezone.utc).date().isoformat()


def _meter_id(workspace) -> str:
    """Deterministic and tenant-exact, like the template head's id."""
    digest = hashlib.sha256(f"reading-meter\x00{workspace!r}".encode("utf-8")).hexdigest()
    return f"tplmeter_{digest[:40]}"


def _meter_path(workspace) -> Path:
    """Offline the meter is NOT a ``*.json`` run file: it is not a run, and
    keeping it out of ``list_runs``'s glob means no reader holds it open."""
    return _root() / f"{_meter_id(workspace)}.meter"


def _meter_used(doc: dict | None, workspace, day: str) -> int:
    if not doc:
        return 0
    if doc.get("user_id") != workspace:
        raise RunStoreError("the reading meter belongs to another workspace")
    return int(doc.get("used") or 0) if doc.get("day") == day else 0


def _meter_doc(workspace, day: str, used: int) -> dict:
    from datetime import datetime, timezone

    return {"id": _meter_id(workspace), "kind": TEMPLATE_METER_KIND, "user_id": workspace,
            "day": day, "used": used,
            "generated_at": datetime.now(timezone.utc).isoformat()}


def _meter_step(workspace, change) -> tuple[int, int | None]:
    """Atomically: read today's count, ``new = change(used, day)``, and write
    ``new`` unless it is None. Returns ``(used, new)``. Raises
    :class:`RunStoreError` when the store cannot do it — the caller refuses."""
    day = _utc_day()
    meter_id = _meter_id(workspace)
    if _use_cloud():
        from app.services import firestore_repo

        out: dict = {}
        try:
            db = firestore_repo._db()
            ref = db.collection(_MR_COLLECTION).document(meter_id)

            def body(txn):
                snap = ref.get(transaction=txn)
                used = _meter_used(snap.to_dict() if snap.exists else None, workspace, day)
                new = change(used, day)
                out["used"], out["new"] = used, new
                if new is not None:
                    txn.set(ref, _meter_doc(workspace, day, new))

            _transactional(body)(db.transaction())
        except RunStoreError:
            raise
        except Exception as exc:
            logger.warning("MR template reading meter could not be updated", exc_info=True)
            raise RunStoreError("the reading allowance could not be checked") from exc
        return out["used"], out["new"]
    try:
        with _LOCAL_LOCK, _LocalFileLock(_root() / f"{meter_id}.lock"):
            p = _meter_path(workspace)
            doc = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
            used = _meter_used(doc, workspace, day)
            new = change(used, day)
            if new is not None:
                _atomic_write(p, _meter_doc(workspace, day, new))
    except (OSError, ValueError) as exc:
        logger.warning("MR template reading meter could not be updated", exc_info=True)
        raise RunStoreError("the reading allowance could not be checked") from exc
    return used, new


def template_readings_today(workspace) -> int:
    """Readings this workspace has spent (or has in flight) today, UTC: the
    meter alone, one small doc read."""
    _require_workspace(workspace)
    meter_id = _meter_id(workspace)
    day = _utc_day()
    if _use_cloud():
        try:
            snap = _collection().document(meter_id).get()
            return _meter_used(snap.to_dict() if snap.exists else None, workspace, day)
        except RunStoreError:
            raise
        except Exception as exc:
            raise RunStoreError("the reading allowance could not be read") from exc
    try:
        # Under the writers' lock: on Windows a reader holding the file open makes
        # a concurrent os.replace fail, and a read between two writes is stale.
        with _LOCAL_LOCK, _LocalFileLock(_root() / f"{meter_id}.lock"):
            p = _meter_path(workspace)
            doc = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError) as exc:
        raise RunStoreError("the reading allowance could not be read") from exc
    return _meter_used(doc, workspace, day)


def reserve_template_reading(workspace, *, by: str, filename: str | None,
                             source_kind: str) -> dict:
    """Take one of today's readings BEFORE the model call — or raise
    :class:`ReadingLimitReached` (nothing taken), or :class:`RunStoreError` when
    the meter cannot be updated (an allowance the store cannot hold is not
    enforced, so the caller refuses rather than spending unmetered)."""
    from datetime import datetime, timezone

    _require_workspace(workspace)
    who = _clean_person(by, "by")

    def take(used: int, _day: str) -> int | None:
        return used + 1 if used < TEMPLATE_READINGS_PER_DAY else None

    _used, new = _meter_step(workspace, take)
    if new is None:
        raise ReadingLimitReached(
            f"all {TEMPLATE_READINGS_PER_DAY} of today's readings are used")
    record = {"id": new_run_id(), "kind": TEMPLATE_READING_KIND, "user_id": workspace,
              "generated_at": datetime.now(timezone.utc).isoformat(), "day": _utc_day(),
              "by": who, "filename": filename, "source_kind": source_kind,
              "status": "pending", "code": None, "usage": None}
    try:
        save_run(record)                   # history only; the meter already counted it
    except Exception:  # noqa: BLE001
        logger.warning("MR template reading %s: audit record not written", record["id"],
                       exc_info=True)
    return record


def settle_template_reading(record: dict, *, status: str, code: str | None = None,
                            usage: dict | None = None) -> None:
    """Record how a reading ended — ``ok`` / ``failed`` (billed) / ``error`` — on its
    audit doc. The meter is untouched: a billed reading stays counted. Best
    effort."""
    updated = {**record, "status": status, "code": code, "usage": usage}
    try:
        if not save_run(updated):
            logger.warning("MR template reading %s could not be settled durably (%s)",
                           record.get("id"), status)
    except Exception:  # noqa: BLE001 - see the docstring
        logger.warning("MR template reading %s could not be settled", record.get("id"),
                       exc_info=True)


def release_template_reading(record: dict) -> None:
    """Nothing was billed: give the reading back — but only on the day it was
    taken (a reading reserved before midnight must not lower the next day's
    count). Best effort: a release that does not land over-counts, never under."""
    workspace = record.get("user_id")

    def give(used: int, day: str) -> int | None:
        return used - 1 if used > 0 and day == record.get("day") else None

    try:
        _meter_step(workspace, give)
    except Exception:  # noqa: BLE001 - see the docstring
        logger.warning("MR template reading %s could not be given back", record.get("id"),
                       exc_info=True)
    try:
        delete_run(record["id"])
    except Exception:  # noqa: BLE001
        logger.warning("MR template reading %s: audit record not removed", record.get("id"),
                       exc_info=True)
