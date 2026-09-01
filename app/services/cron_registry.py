"""The scheduled-jobs registry, merged with live Cloud Scheduler state.

Why a registry exists at all
----------------------------
Cloud Scheduler knows *that* a job fires and whether its last attempt worked.
It does not know *why* the job exists, which agent it belongs to, or why its
cadence was chosen — and the SEO cron being dead for eleven days (08-07 →
08-18) proved that a job silently absent from the scheduler is the failure
mode nobody sees. So the curated list below is the contract: every scheduled
job this service depends on, with its purpose and expected schedule written
down. The merge then gives every row an ``origin`` that makes both directions
of drift loud:

* ``live_registered`` — registered and live: the normal row;
* ``registry_only``  — registered, but no live job answered for it: either a
  dead cron (the scheduler listed fine and the job was not there) or an
  unconfirmed expectation (the scheduler could not be read at all — the
  envelope's ``scheduler_ok`` tells the two apart);
* ``live_only``      — a live job the registry never heard of: someone created
  a job by hand and nobody wrote down why.

Auth model
----------
Application Default Credentials, exactly like every other Google API this
codebase calls (see ``drive_source.build_drive_service`` and
``scripts/provision_staging.py``): locally the JSON key at
``GOOGLE_APPLICATION_CREDENTIALS`` (exported from ``.env`` by ``app.config``),
on Cloud Run the attached service-account identity. No new auth path.

Honesty
-------
A scheduler fetch that fails raises :class:`SchedulerUnavailable`. The router
turns that into ``scheduler_ok: false`` plus registry rows explicitly marked
``registry_only`` — expectation, clearly labelled as unconfirmed — never into
registry rows dressed as live state.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from app.services.agent_config import AGENT_LABELS

logger = logging.getLogger("agentos.cron_registry")

# --------------------------------------------------------------------------- #
# Cloud Scheduler location + deadlines
# --------------------------------------------------------------------------- #

SCHEDULER_PROJECT = "helpful-charmer-498509-v5"
SCHEDULER_LOCATION = "us-central1"
SCHEDULER_JOBS_URL = (
    "https://cloudscheduler.googleapis.com/v1/"
    f"projects/{SCHEDULER_PROJECT}/locations/{SCHEDULER_LOCATION}/jobs"
)
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

#: Socket deadline per request. This runs in a sync handler, i.e. on one of
#: anyio's 40 worker threads — a hung Google call takes a slot out of
#: circulation, so the deadline is explicit rather than inherited.
SCHEDULER_TIMEOUT_SECONDS = 15
#: Token refresh inside AuthorizedSession otherwise inherits google-auth's
#: 120s default — same reasoning as ``storage._TimedRequest``.
AUTH_REFRESH_TIMEOUT_SECONDS = 10
#: Cloud Scheduler pages at up to 500 jobs; this project has ~3. More than a
#: handful of pages means something is deeply wrong, so the loop refuses
#: rather than returning a partial listing as if it were complete.
MAX_LIST_PAGES = 5

#: How long one successful scheduler fetch is served to subsequent requests.
#: The page is a read-only ops panel; 60s staleness is invisible next to
#: schedules measured in minutes and days, and it keeps a page load from
#: hammering the API.
CACHE_TTL_SECONDS = 60

#: Row origins — see the module docstring.
ORIGIN_LIVE_REGISTERED = "live_registered"
ORIGIN_LIVE_ONLY = "live_only"
ORIGIN_REGISTRY_ONLY = "registry_only"


# --------------------------------------------------------------------------- #
# The registry — every scheduled job this service depends on
# --------------------------------------------------------------------------- #

#: ``expected`` is the schedule the live scheduler carried when the entry was
#: pinned (read from the API on 2026-09-02, not guessed). When a row is
#: ``registry_only`` this is what the panel shows — an expectation, labelled
#: as one. Note mr's job *name* says 3min from an earlier cadence; the
#: schedule is the truth.
CRON_REGISTRY: tuple[dict[str, Any], ...] = (
    {
        "id": "geo-poll-daily",
        "agent_id": "a10",
        "name": "AI answer poll",
        "endpoint": "POST /api/geo/cron/poll",
        "purpose": (
            "Asks the AI engines every brand's buyer questions and records "
            "who gets named and cited."
        ),
        "why_time": (
            "Overnight at 2:00 AM IST, so the console opens each morning on "
            "answers measured today, and the long sweep runs when nobody is "
            "waiting on it."
        ),
        "expected": {"cron": "0 2 * * *", "timezone": "Asia/Kolkata"},
    },
    {
        "id": "seo-sweep-daily",
        "agent_id": "a2",
        "name": "Rankings sweep",
        "endpoint": "POST /api/seo-geo/cron/run",
        "purpose": (
            "Re-reads live rankings and page health for every brand so the "
            "SEO desk reflects today's results."
        ),
        "why_time": (
            "At 3:30 AM IST, after the AI answer poll has finished, so the "
            "two overnight sweeps never run at the same time."
        ),
        "expected": {"cron": "30 3 * * *", "timezone": "Asia/Kolkata"},
    },
    {
        "id": "mr-refresh-3min",
        "agent_id": "a6",
        "name": "Tracker refresh",
        "endpoint": "POST /api/mr/cron/refresh",
        "purpose": (
            "Pulls the marketing tracker sheet and re-checks spend, pacing "
            "and lead flags for the workspace."
        ),
        "why_time": (
            "Every few minutes through the day, so a tracker change or a bad "
            "lead batch surfaces within minutes rather than at tomorrow's "
            "open."
        ),
        "expected": {"cron": "*/15 * * * *", "timezone": "America/Los_Angeles"},
    },
)


class SchedulerUnavailable(RuntimeError):
    """Cloud Scheduler could not be read. Callers must surface this as
    ``scheduler_ok: false`` — never present the registry as live state."""


# --------------------------------------------------------------------------- #
# Live state
# --------------------------------------------------------------------------- #

def _session():
    """An authenticated requests session via ADC — the pattern
    ``scripts/provision_staging.py`` already uses, with the token-refresh
    deadline stated instead of inherited."""
    import google.auth
    import google.auth.transport.requests as greq

    creds, _project = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
    return greq.AuthorizedSession(
        creds, refresh_timeout=AUTH_REFRESH_TIMEOUT_SECONDS
    )


#: Scheduler states the panel distinguishes. DISABLED (the API's word for a
#: job knocked out by e.g. a deleted target) reads as PAUSED to the user —
#: "not firing" is the fact that matters. Anything else maps to null rather
#: than inventing a third state the frontend never heard of.
_STATE_MAP = {"ENABLED": "ENABLED", "PAUSED": "PAUSED", "DISABLED": "PAUSED"}


def _shape_live(job: dict[str, Any]) -> dict[str, Any]:
    """One Cloud Scheduler job resource → the live side of a merged row.

    ``status`` is a ``google.rpc.Status``: absent/empty or ``code`` 0 means the
    last attempt succeeded; any non-zero code (e.g. 7 PERMISSION_DENIED) means
    it failed. A job that has never fired has no ``lastAttemptTime`` and
    honestly reports ``last_attempt: null`` — "no attempt yet" is not the same
    claim as "the last attempt worked".
    """
    status = job.get("status") or {}
    code = int(status.get("code") or 0)
    last_time = job.get("lastAttemptTime") or None
    cron = job.get("schedule") or None
    return {
        "id": str(job.get("name") or "").rsplit("/", 1)[-1],
        "schedule": (
            {"cron": cron, "timezone": job.get("timeZone") or None}
            if cron else None
        ),
        "state": _STATE_MAP.get(str(job.get("state") or "")),
        "last_attempt": (
            {"time": last_time, "ok": code == 0} if last_time else None
        ),
        "next_time": job.get("scheduleTime") or None,
    }


def fetch_live_jobs() -> dict[str, dict[str, Any]]:
    """``{job name: live-side fields}`` from the Cloud Scheduler REST API.

    Any failure — credentials, network, a non-2xx, unparseable JSON — raises
    :class:`SchedulerUnavailable` with the reason. There is no partial or
    fallback return.
    """
    try:
        session = _session()
        jobs: dict[str, dict[str, Any]] = {}
        page_token: Optional[str] = None
        for _page in range(MAX_LIST_PAGES):
            params = {"pageToken": page_token} if page_token else None
            resp = session.get(
                SCHEDULER_JOBS_URL, params=params, timeout=SCHEDULER_TIMEOUT_SECONDS
            )
            resp.raise_for_status()
            payload = resp.json()
            for job in payload.get("jobs", []):
                shaped = _shape_live(job)
                if shaped["id"]:
                    jobs[shaped["id"]] = shaped
            page_token = payload.get("nextPageToken")
            if not page_token:
                return jobs
        raise SchedulerUnavailable(
            f"more than {MAX_LIST_PAGES} pages of scheduler jobs — refusing to "
            "return a partial listing."
        )
    except SchedulerUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — every failure shape maps to one honest error
        logger.warning("Cloud Scheduler list failed: %s", exc)
        raise SchedulerUnavailable(str(exc)) from exc


# --------------------------------------------------------------------------- #
# Cache — one successful fetch serves the page for CACHE_TTL_SECONDS
# --------------------------------------------------------------------------- #

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "live": None}


def _now() -> float:
    return time.monotonic()


def invalidate_cache() -> None:
    """Drop the cached fetch (tests, and any future manual-refresh action)."""
    with _cache_lock:
        _cache["at"], _cache["live"] = 0.0, None


def live_jobs_cached() -> dict[str, dict[str, Any]]:
    """The live listing, at most :data:`CACHE_TTL_SECONDS` old.

    Only success is cached: a failed fetch raises and leaves the cache as it
    was, so the next request retries instead of serving a remembered error.
    The network call happens outside the lock — the lock protects the dict,
    not the fetch, and two concurrent misses doing one redundant fetch each is
    cheaper than every request queueing behind a 15s socket.
    """
    with _cache_lock:
        live = _cache["live"]
        if live is not None and _now() - _cache["at"] < CACHE_TTL_SECONDS:
            return live
    live = fetch_live_jobs()
    with _cache_lock:
        _cache["at"], _cache["live"] = _now(), live
    return live


# --------------------------------------------------------------------------- #
# The merge
# --------------------------------------------------------------------------- #

#: The registry side of a row, nulled — what a ``live_only`` row carries.
_REGISTRY_NULLS: dict[str, Any] = {
    "name": None, "agent_id": None, "agent_label": None, "endpoint": None,
    "purpose": None, "why_time": None,
}
#: The live side of a row, nulled — what a ``registry_only`` row carries
#: (except ``schedule``, which is filled with the registry's expectation).
_LIVE_NULLS: dict[str, Any] = {
    "schedule": None, "state": None, "last_attempt": None, "next_time": None,
}


def _curated_side(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry["id"],
        "name": entry["name"],
        "agent_id": entry["agent_id"],
        # The same label dict the run tables and admin panel use, so the
        # frontend's agent lookup matches instead of drifting on a synonym.
        "agent_label": AGENT_LABELS.get(entry["agent_id"], entry["agent_id"]),
        "endpoint": entry["endpoint"],
        "purpose": entry["purpose"],
        "why_time": entry["why_time"],
    }


def _registry_only_row(entry: dict[str, Any]) -> dict[str, Any]:
    """A registered job with no live state: the expectation, labelled as one.

    ``schedule`` is the registry's *expected* cron+timezone — real curated
    knowledge, not a claim about the scheduler — while everything only the
    scheduler could know (state, attempts, next fire) stays null.
    """
    return {
        **_curated_side(entry),
        **_LIVE_NULLS,
        "schedule": dict(entry["expected"]),
        "origin": ORIGIN_REGISTRY_ONLY,
    }


def registry_only_jobs() -> list[dict[str, Any]]:
    """Every registered job as an expectation-only row — what the panel shows
    when the scheduler cannot be read at all (``scheduler_ok: false``)."""
    return [_registry_only_row(entry) for entry in CRON_REGISTRY]


def merge_jobs(live: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Registry ∪ live, in registry order, with every row's origin stated.

    Pure — the seam the merge tests use. Rows for registry entries come first
    (curated order is presentation order; a dead cron stays exactly where the
    reader expects it), then ``live_only`` jobs sorted by name.
    """
    rows: list[dict[str, Any]] = []
    for entry in CRON_REGISTRY:
        job = live.get(entry["id"])
        if job is None:
            rows.append(_registry_only_row(entry))
        else:
            rows.append({
                **_curated_side(entry),
                "schedule": job["schedule"],
                "state": job["state"],
                "last_attempt": job["last_attempt"],
                "next_time": job["next_time"],
                "origin": ORIGIN_LIVE_REGISTERED,
            })
    registered = {entry["id"] for entry in CRON_REGISTRY}
    for name in sorted(set(live) - registered):
        job = live[name]
        rows.append({
            "id": job["id"],
            **_REGISTRY_NULLS,
            "schedule": job["schedule"],
            "state": job["state"],
            "last_attempt": job["last_attempt"],
            "next_time": job["next_time"],
            "origin": ORIGIN_LIVE_ONLY,
        })
    return rows


def merged_jobs() -> list[dict[str, Any]]:
    """What the endpoint serves on the happy path: the registry merged with
    (cached) live state.

    Raises :class:`SchedulerUnavailable` when live state cannot be read —
    deliberately not caught here, so the caller must decide how to present
    the outage (the router says ``scheduler_ok: false`` and serves
    :func:`registry_only_jobs`).
    """
    return merge_jobs(live_jobs_cached())
