"""The record — every run this caller filed, across every agent.

Until now the ``runs`` collection had exactly one reader: the admin Database
panel, which browses it raw and only for admins. Every agent could *write* the
trail and nobody could *read* it back, so the console's own record of what its
specialists had made did not exist as a product surface.

This router is that surface, and it is deliberately small: one read, scoped to
the caller, newest first, with the filtering the panel needs done here rather
than by shipping the whole collection to a browser.

Two things it will not do:

* **It will not invent a cost.** ``runs`` rows carry who, what, which agent and
  when — not tokens and not dollars, because the trail was never asked to
  record them. So the record shows what it has. A per-run spend column would
  have to be fabricated from an account-level 30-day total, and a made-up figure
  in a ledger is worse than an absent one.
* **It will not report a count it did not read.** A Firestore that cannot be
  reached returns ``None`` from the repo and 503 from here, rather than an empty
  list that reads to the user as "you have never run anything".
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.security import get_current_user
from app.services import firestore_repo

logger = logging.getLogger("agentos.runs")

router = APIRouter()

#: How many of the caller's newest rows one request examines. The panel filters
#: and counts inside this window, and says so when the window is full — a figure
#: derived from a partial read is labelled as one.
SCAN_LIMIT = 500

#: The four states the console renders. Everything the trail actually writes
#: maps onto one of them; the raw word travels alongside so an opened row can
#: show exactly what the backend stored.
_STATE_BY_STATUS: dict[str, str] = {
    "failed": "failed",
    "error": "failed",
    "in_progress": "running",
    "running": "running",
    "queued": "queued",
    "pending": "queued",
    "completed": "done",
    "done": "done",
    "ok": "done",
}


def _state_of(row: dict[str, Any]) -> tuple[str, str]:
    """(state, the raw status word) for one row.

    An append-only trail row is written *after* the unit of work happened, so a
    row with no recognised status is finished by construction — that is what the
    row's existence means. A staged run says otherwise explicitly.
    """
    raw = str(row.get("run_status") or row.get("status") or "").strip().lower()
    return _STATE_BY_STATUS.get(raw, "done"), raw


def _iso_to_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _took_seconds(row: dict[str, Any]) -> Optional[int]:
    """How long the run took, when both ends are on the row.

    Only meaningful for a staged run, which is updated in place as it moves. An
    append-only row is stamped once, so both timestamps are the same instant and
    this correctly returns ``None`` rather than a duration of zero.
    """
    start, end = _iso_to_dt(row.get("created_at")), _iso_to_dt(row.get("updated_at"))
    if not start or not end:
        return None
    seconds = int((end - start).total_seconds())
    return seconds if seconds > 0 else None


def _image_of(row: dict[str, Any]) -> Optional[str]:
    """The picture the run made, if it made one.

    The Graphics Designer appends one asset per kept stage attempt; the last is
    the finished creative, which is what the row should lead with.
    """
    assets = row.get("assets")
    if not isinstance(assets, list):
        return None
    for asset in reversed(assets):
        if isinstance(asset, dict) and asset.get("url"):
            return str(asset["url"])
    return None


def _title_of(row: dict[str, Any]) -> str:
    """What the row is called, in the reader's words rather than the trail's.

    ``task`` is the human one-liner a handler wrote. ``run_summary`` is the
    staged shape's equivalent. Falling back to the action slug is better than an
    empty row, and better than inventing a sentence.
    """
    for key in ("task", "run_summary", "creative_summary"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    action = str(row.get("action") or "").strip()
    return action or "Untitled run"


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    state, raw = _state_of(row)
    created = str(row.get("created_at") or "")
    return {
        "id": str(row.get("id") or row.get("run_id") or ""),
        "run_id": str(row.get("run_id") or row.get("id") or ""),
        "agent_id": str(row.get("agent_id") or ""),
        "agent_name": str(row.get("agent_name") or ""),
        "brand": row.get("brand") or None,
        "brand_id": row.get("brand_id") or None,
        "action": str(row.get("action") or ""),
        "title": _title_of(row),
        "state": state,
        "status_raw": raw,
        "created_at": created,
        "updated_at": str(row.get("updated_at") or created),
        "day": str(row.get("day") or row.get("date") or created[:10]),
        "took_seconds": _took_seconds(row),
        "image": _image_of(row),
        "user": str(row.get("user") or ""),
    }


def _matches(run: dict[str, Any], agent: str, state: str, brand: str, q: str) -> bool:
    if agent != "all" and run["agent_id"] != agent:
        return False
    if state != "all" and run["state"] != state:
        return False
    if brand != "all" and (run["brand"] or "") != brand:
        return False
    if q:
        hay = f"{run['title']} {run['agent_name']} {run['brand'] or ''} {run['action']}".lower()
        if q not in hay:
            return False
    return True


@router.get("/runs")
def list_runs(
    limit: int = Query(default=120, ge=1, le=SCAN_LIMIT),
    agent: str = Query(default="all"),
    state: str = Query(default="all"),
    brand: str = Query(default="all"),
    q: str = Query(default="", max_length=200),
    user: dict = Depends(get_current_user),
) -> dict:
    """This caller's record, newest first, with the facets the panel needs.

    ``total`` is every run they have ever filed — read as a server-side count,
    not as the length of this page. ``window_complete`` says whether the facets
    and the week block describe everything or only the newest ``SCAN_LIMIT``
    rows, so the panel can qualify a figure it cannot fully stand behind.
    """
    uid = str(user.get("id") or "")
    rows = firestore_repo.list_runs_for_user(uid, limit=SCAN_LIMIT)
    if rows is None:
        raise HTTPException(503, "The record could not be read just now. Nothing is lost — try again shortly.")

    runs = [_shape(r) for r in rows]
    scanned = len(runs)
    window_complete = scanned < SCAN_LIMIT

    by_agent: Counter[str] = Counter()
    agent_names: dict[str, str] = {}
    by_brand: Counter[str] = Counter()
    by_state: Counter[str] = Counter()
    for r in runs:
        if r["agent_id"]:
            by_agent[r["agent_id"]] += 1
            agent_names.setdefault(r["agent_id"], r["agent_name"])
        if r["brand"]:
            by_brand[str(r["brand"])] += 1
        by_state[r["state"]] += 1

    # One seven-day window, used by every figure that says "this week". Two
    # windows on one page is how a headline ends up disagreeing with the list
    # beneath it.
    week_from = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    week: Counter[str] = Counter()
    week_by_agent: Counter[str] = Counter()
    for r in runs:
        if r["created_at"] >= week_from:
            week[r["state"]] += 1
            if r["agent_id"]:
                week_by_agent[r["agent_id"]] += 1

    term = q.strip().lower()
    shown = [r for r in runs if _matches(r, agent, state, brand, term)][:limit]

    # When the window did not fill, it holds every run this caller has, so the
    # length of it *is* the total. Asking Firestore to count what we have just
    # finished reading is a second round-trip for a number already in hand, and
    # this endpoint is on the critical path of three panels.
    total = scanned if window_complete else firestore_repo.count_runs_for_user(uid)

    return {
        "runs": shown,
        "total": total,
        "scanned": scanned,
        "scan_limit": SCAN_LIMIT,
        "window_complete": window_complete,
        "facets": {
            "agents": [
                {"id": aid, "name": agent_names.get(aid, aid), "count": c}
                for aid, c in by_agent.most_common()
            ],
            "brands": [{"name": b, "count": c} for b, c in by_brand.most_common()],
            "states": dict(by_state),
        },
        "week": {
            "from": week_from,
            "done": week.get("done", 0),
            "running": week.get("running", 0),
            "queued": week.get("queued", 0),
            "failed": week.get("failed", 0),
            "total": sum(week.values()),
            # Who the caller actually leaned on, over the same seven days every
            # other figure on the page uses.
            "by_agent": [
                {"id": aid, "name": agent_names.get(aid, aid), "count": c}
                for aid, c in week_by_agent.most_common()
            ],
        },
        "live": {
            "running": by_state.get("running", 0),
            "queued": by_state.get("queued", 0),
        },
    }
