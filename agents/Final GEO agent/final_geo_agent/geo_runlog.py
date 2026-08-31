"""GEO agent — the run log: one entry per sweep that ended.

The poll's other documents answer "what did the engines say"; nothing answered
"what did the sweep itself do" — when it started, how long it took, what
stopped it, which engines it reached, how many calls errored. That is the
record a human reads when a chart point looks wrong or a cron fire "did
nothing", and it is reconstructed today by grepping Cloud Run logs.

One document per brand (``geo-runlog-{brand}``), newest entry first, capped so
it stays far under the 1 MB doc limit. Written from exactly one place — the
moment ``geo_poll.poll_step`` sees a sweep end — through the shared
transactional primitive, because a UI-driven sweep and a cron sweep can end
within the same second.
"""
from __future__ import annotations

import datetime as dt
import uuid

from final_geo_agent import geo_store
from seo_geo_agent import state

# Roughly four months of two-day sweeps plus manual re-runs; each entry is a
# few hundred bytes.
MAX_RUNS = 120


def runlog_doc_id(brand_id: str) -> str:
    return f"geo-runlog-{brand_id}"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def record_run(brand_id: str, run: dict) -> dict:
    """Store one finished sweep; returns the entry as stored.

    ``run`` is the caller's summary (see ``geo_poll._run_summary`` for the
    shape); an ``id`` is assigned when missing. Re-recording an id replaces
    the earlier entry rather than duplicating it.
    """
    entry = dict(run)
    entry.setdefault("id", uuid.uuid4().hex[:8])
    entry.setdefault("recorded_at", _now())

    def change(doc: dict) -> tuple[dict, dict]:
        doc = dict(doc or {})
        doc["brand_id"] = brand_id
        kept = [r for r in doc.get("runs") or [] if r.get("id") != entry["id"]]
        doc["runs"] = [entry, *kept][:MAX_RUNS]
        doc["updated_at"] = _now()
        return doc, entry

    return geo_store.mutate(runlog_doc_id(brand_id), change)


def recent_runs(brand_id: str, n: int = 30) -> list[dict]:
    """The last ``n`` sweeps, newest first."""
    doc = state.load(runlog_doc_id(brand_id)) or {}
    return list(doc.get("runs") or [])[: max(0, n)]


def plan_progress(brand_id: str) -> dict | None:
    """``{"done", "total"}`` over the current Action Plan's actions, or
    ``None`` when the brand has no plan — a sweep entry records where the
    plan stood when the measurement was taken, so a score move can be read
    against the work that preceded it."""
    # Imported here, not at module scope: geo_strategy reads windows that
    # geo_poll writes, and geo_poll writes this log — a top-level import would
    # close that ring.
    from final_geo_agent import geo_strategy

    doc = geo_strategy.load_strategy(brand_id)
    current = (doc or {}).get("current") or {}
    if not current:
        return None
    actions = [
        action
        for wave in current.get("waves") or []
        for action in wave.get("actions") or []
    ]
    return {
        "done": sum(1 for a in actions if a.get("status") == "done"),
        "total": len(actions),
    }
