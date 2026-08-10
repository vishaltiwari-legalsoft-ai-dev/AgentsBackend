"""Per-agent activity logging — the usage trail behind the admin Database panel.

RULE (mandatory for every agent): each agent router MUST log its meaningful
work through ``record_activity`` — one call per user-visible unit of work (a
run started, a report built, a question answered, a poll executed). That call
is what populates:

  1. ``agent_runs__<agent_id>``  — the agent's OWN table (auto-created on first
     write, auto-discovered by the admin Database panel), and
  2. ``runs``                    — the master who-did-what table across agents,
  3. ``creative_events``         — the Home usage dashboard counters.

An agent without these calls is invisible in the Database panel, which is a
bug. ``tests/test_agent_run_logging.py`` enforces the rule: any router that
declares an ``*_AGENT_ID`` constant must feed this module (or, like the
Graphics Designer, the richer staged ``firestore_repo.start_run`` tables).

All writes are best-effort — logging must never break the request it rides on.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from app.services import firestore_repo

# Cron/scheduler calls carry no signed-in user; log them under this identity so
# automated activity stays visible but distinguishable from real users.
CRON_USER: dict = {"id": "cron", "email": "cron@scheduler", "session_id": "", "timezone": "UTC"}

_MAX_TASK_CHARS = 500


def record_activity(
    user: dict,
    *,
    agent_id: str,
    agent_name: str,
    category: str,
    action: str,
    task: str,
    brand: Optional[str] = None,
    brand_id: Optional[str] = None,
    run_id: Optional[str] = None,
    status: str = "completed",
    usage_action: str = "generate",
    count: int = 1,
) -> None:
    """Log one unit of agent work: who did it, on which agent, and what task.

    ``user`` is the ``get_current_user`` dict (or ``CRON_USER``); ``action`` is
    a short machine slug ("ask", "report:monthly_summary"); ``task`` the human
    one-liner the admin panel shows; ``run_id`` links multiple events of one
    long-running job. ``usage_action`` feeds the Home dashboard: "session" when
    the event starts a run/conversation, "generate" when it produces output.
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    event_id = uuid.uuid4().hex
    task = (task or "").strip()[:_MAX_TASK_CHARS]
    doc = {
        "id": event_id,
        "run_id": run_id or event_id,
        "date": now_iso[:10],
        "day": now.strftime("%Y-%m-%d"),
        "year_month": now.strftime("%Y-%m"),
        "created_at": now_iso,
        "updated_at": now_iso,
        "session_id": str(user.get("session_id") or ""),
        "timezone": str(user.get("timezone") or "UTC"),
        "user_id": str(user.get("id") or ""),
        "user": str(user.get("email") or ""),
        "agent_id": agent_id,
        "agent_name": agent_name,
        "action": action,
        "task": task,
        "brand": brand,
        "brand_id": brand_id,
        "status": status,
    }
    try:
        _db = firestore_repo._db  # late-bound so tests can stub it
        _db().collection(firestore_repo.agent_runs_collection(agent_id)).document(
            event_id
        ).set(doc)
    except Exception:  # logging is best-effort — never fail the user's request
        pass
    try:
        firestore_repo._db().collection("runs").document(event_id).set(
            {**doc, "run_status": status, "run_summary": f"{action}: {task}"[:_MAX_TASK_CHARS]}
        )
    except Exception:
        pass
    firestore_repo.log_usage_event(
        user_id=str(user.get("id") or ""),
        email=str(user.get("email") or ""),
        agent_id=agent_id,
        category=category,
        action=usage_action,
        count=count,
        brand=brand,
    )
