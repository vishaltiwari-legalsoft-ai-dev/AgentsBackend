"""Cron Manager — the scheduled jobs, read-only.

One route: the curated registry (``app/services/cron_registry.py``) merged
with live Cloud Scheduler state, so a Creator can see in one table what fires,
when, why, and whether the last attempt worked — and, more importantly, see a
registered job that no longer exists in the scheduler (``origin:
"registry_only"`` while ``scheduler_ok`` is true — the dead cron that cost the
SEO sweep eleven silent days) or a live job nobody wrote down
(``origin: "live_only"``).

When Cloud Scheduler itself cannot be read, the answer is still 200 — but
``scheduler_ok: false``, a plain-sentence ``scheduler_error``, and every job
rendered as an expectation-only row for the frontend to mark "Unconfirmed".
That is the honest-partial design: the curated expectations are real knowledge
worth showing, and the envelope says exactly which claim is not being made. A
5xx is reserved for genuine server faults.

Creator-only: this is a panel about the deployment itself, the same audience
as Settings → Secrets. Read-only on purpose in v1 — pausing/resuming a
production job is a deliberate act that belongs in the console until there is
a design for doing it safely from here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.security import require_creator
from app.services import cron_registry

logger = logging.getLogger("agentos.cron")

router = APIRouter()

#: Ceiling on the reason echoed to the panel — one plain sentence, not a dump.
_ERROR_DETAIL_MAX_CHARS = 160


def _outage_sentence(exc: Exception) -> str:
    """One plain sentence for the panel; the full story goes to the log.

    The exception text is flattened to a single line and truncated — a
    requests error can carry a URL and a nested cause chain, and the panel
    needs "what happened", not a trace.
    """
    reason = " ".join(str(exc).split())[:_ERROR_DETAIL_MAX_CHARS] or "unknown error"
    return (
        f"Cloud Scheduler could not be read ({reason}); showing each "
        "registered job's expected schedule instead."
    )


@router.get("/cron/jobs")
def list_cron_jobs(_creator: dict = Depends(require_creator)) -> dict:
    """Every scheduled job: registered, live, or worryingly only one of the two.

    The scheduler fetch is cached in-process for 60 seconds (see
    ``cron_registry.live_jobs_cached``), so page loads share one API call.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    try:
        jobs = cron_registry.merged_jobs()
    except cron_registry.SchedulerUnavailable as exc:
        logger.error("cron manager could not read Cloud Scheduler: %s", exc)
        return {
            "generated_at": generated_at,
            "scheduler_ok": False,
            "scheduler_error": _outage_sentence(exc),
            "jobs": cron_registry.registry_only_jobs(),
        }
    return {
        "generated_at": generated_at,
        "scheduler_ok": True,
        "scheduler_error": None,
        "jobs": jobs,
    }
