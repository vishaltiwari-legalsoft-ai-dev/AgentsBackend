"""The activity trail — what the console records about the work its agents do.

===========================================================================
THE RULE
===========================================================================

An endpoint records **one** activity when it is a unit of work somebody could
later be asked to account for. Concretely, when it:

  * starts a job — a run, a sweep, a conversation                 → unit ``JOB``
  * produces a deliverable by spending an external model or API
    call: a report, a draft, an analysis, an answer, a creative   → unit ``OUTPUT``
  * hands the user a file to keep — an export, a download         → unit ``OUTPUT``
  * fires from cron                                               → unit ``JOB``
  * durably changes configuration, or deletes data, that outlives
    the request — brands, sources, prompts, competitors, targets,
    tracked skills, credentials                                   → unit ``CHANGE``

An endpoint records **nothing** when:

  * it only reads. Every GET that renders a panel fires on page load and on
    every poll tick; a row per read would grow the trail faster than the work
    it is supposed to describe. Expensive to compute is not the same as
    chargeable.
  * it is a step *inside* a job whose start and end are already recorded — a
    browser run's step, a Graphics Designer stage patch, a preview that
    persists nothing. **The job is the unit, not the request.** This is the
    clause that keeps a 30-step browser run at one row and a strategist chat
    at zero, and it is the reason an in-job model call can legitimately be
    silent.
  * it serves artifact bytes so a page can render an image.

Every endpoint under an agent router that is not a plain GET must therefore
either declare a trail (see :class:`ActivityTrail` below) or carry
:func:`silent` with the reason it does not. ``tests/test_agent_run_logging.py``
holds the whole service to that, by walking the real application's routes and
by driving endpoints and reading back what landed — not by searching files for
a substring.

===========================================================================
WHERE IT LANDS
===========================================================================

Three stores, all best-effort — logging must never break the request it rides
on, and a store that is down must never be reported to the caller as a failed
request:

  1. ``agent_runs__<agent_id>`` — the agent's OWN table (auto-created on first
     write, auto-discovered by the admin Database panel),
  2. ``runs``                   — the master who-did-what table across agents,
  3. ``creative_events``        — the Home usage dashboard counters. ``JOB``
     writes a ``session``, ``OUTPUT`` writes a ``generate``, and ``CHANGE``
     writes nothing at all: renaming a competitor is not a creative produced,
     and counting it as one made the dashboard overstate output.

===========================================================================
TWO SHAPES OF THE SAME TRAIL
===========================================================================

``EVENT`` (:meth:`ActivityTrail.records`) — one document per unit of work,
append-only. The default, and what six routers use.

``STAGED`` (:meth:`ActivityTrail.stages`) — one document per *run*, created
when the run opens and updated in place as it moves through its stages. The
Graphics Designer works this way because a single creative is 15-plus requests
across four stages; an event row for each would be twenty documents describing
one picture. It is a declared shape of this same trail, writing the same three
stores through :func:`firestore_repo.start_run` / ``update_run``, not a private
bypass — which is what it used to be, when the enforcement test special-cased
one file name to let it through.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional, TypeVar

from fastapi import Depends
from fastapi.params import Depends as DependsType

from app.security import get_current_user
from app.services import firestore_repo

logger = logging.getLogger("agentos.trail")

# Cron/scheduler calls carry no signed-in user; log them under this identity so
# automated activity stays visible but distinguishable from real users.
CRON_USER: dict = {"id": "cron", "email": "cron@scheduler", "session_id": "", "timezone": "UTC"}

_MAX_TASK_CHARS = 500

# --------------------------------------------------------------------------- #
# Units of work. The unit decides what the Home dashboard counts; see THE RULE.
# --------------------------------------------------------------------------- #
JOB = "job"        # a run / sweep / conversation was started
OUTPUT = "output"  # a deliverable was produced, or a file handed over
CHANGE = "change"  # durable configuration or data changed — audit only

_UNIT_USAGE: dict[str, Optional[str]] = {JOB: "session", OUTPUT: "generate", CHANGE: None}

#: Who the row is attributed to. ``CALLER`` resolves the signed-in user through
#: the normal dependency; ``CRON`` is for the key-authenticated scheduler paths,
#: which have no user to resolve.
CALLER = "caller"
CRON = "cron"

F = TypeVar("F", bound=Callable[..., Any])


# --------------------------------------------------------------------------- #
# The writer
# --------------------------------------------------------------------------- #

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
    usage_action: Optional[str] = "generate",
    count: int = 1,
) -> None:
    """Log one unit of agent work: who did it, on which agent, and what task.

    ``user`` is the ``get_current_user`` dict (or :data:`CRON_USER`); ``action``
    is a short machine slug ("ask", "report:monthly_summary"); ``task`` the
    human one-liner the admin panel shows; ``run_id`` links multiple events of
    one long-running job. ``usage_action`` feeds the Home dashboard: "session"
    when the event starts a run/conversation, "generate" when it produces
    output, and ``None`` when the event is an audit record that produced
    nothing (a config edit, a deletion).

    Routers reach this through :class:`ActivityTrail` rather than calling it
    directly, so that the decision to record lives on the route declaration.
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
        logger.warning("trail: could not write agent_runs__%s", agent_id, exc_info=True)
    try:
        firestore_repo._db().collection("runs").document(event_id).set(
            {**doc, "run_status": status, "run_summary": f"{action}: {task}"[:_MAX_TASK_CHARS]}
        )
    except Exception:
        logger.warning("trail: could not write the master runs row", exc_info=True)
    if usage_action is None:
        return
    firestore_repo.log_usage_event(
        user_id=str(user.get("id") or ""),
        email=str(user.get("email") or ""),
        agent_id=agent_id,
        category=category,
        action=usage_action,
        count=count,
        brand=brand,
    )


# --------------------------------------------------------------------------- #
# The row a single request contributes
# --------------------------------------------------------------------------- #

class Activity:
    """The row this request will add to the trail.

    Built by the trail dependency before the handler runs and written once
    after the handler returns. A handler fills in the human detail with
    :meth:`note`; if it never does, the row still lands, carrying the summary
    declared at the route — so an endpoint cannot be declared as recorded and
    then quietly record nothing.
    """

    def __init__(self, trail: "ActivityTrail", user: dict, action: str,
                 summary: str, unit: str) -> None:
        self._trail = trail
        self._user = user
        self.action = action
        self.summary = summary
        self.unit = unit
        self.brand: Optional[str] = None
        self.brand_id: Optional[str] = None
        self.run_id: Optional[str] = None
        self.status = "completed"
        self.count = 1
        self._skipped: Optional[str] = None

    def note(
        self,
        summary: Optional[str] = None,
        *,
        action: Optional[str] = None,
        brand: Optional[str] = None,
        brand_id: Optional[str] = None,
        run_id: Optional[str] = None,
        status: Optional[str] = None,
        count: Optional[int] = None,
    ) -> None:
        """Fill in what this particular request turned out to be.

        Everything is optional and additive: pass only what the handler learned
        while doing the work. Safe to call more than once — the last value for
        a field wins.
        """
        if summary is not None:
            self.summary = summary
        if action is not None:
            self.action = action
        if brand is not None:
            self.brand = brand
        if brand_id is not None:
            self.brand_id = brand_id
        if run_id is not None:
            self.run_id = run_id
        if status is not None:
            self.status = status
        if count is not None:
            self.count = int(count)

    def skip(self, reason: str) -> None:
        """Record nothing for this request, deliberately.

        For the endpoints that only *sometimes* close a unit of work — a
        browser step that did not end the run, a cron fire with nothing due.
        The reason is for whoever reads the handler; it is never surfaced to
        the caller and is not a failure.
        """
        self._skipped = reason

    # -- internals ---------------------------------------------------------- #

    def _write(self) -> None:
        record_activity(
            self._user,
            agent_id=self._trail.agent_id,
            agent_name=self._trail.agent_name,
            category=self._trail.category,
            action=self.action,
            task=self.summary,
            brand=self.brand,
            brand_id=self.brand_id,
            run_id=self.run_id,
            status=self.status,
            usage_action=_UNIT_USAGE[self.unit],
            count=self.count,
        )

    def _flush(self) -> None:
        """Write the row. Never raises: a trail that is down is a problem for
        the logs to carry, not for the caller's request to fail on."""
        if self._skipped is not None:
            return
        try:
            self._write()
        except Exception:  # noqa: BLE001 - see the docstring
            logger.exception(
                "trail: failed to record %s/%s for agent %s",
                self.action, self.unit, self._trail.agent_id,
            )


class StagedActivity(Activity):
    """The ``STAGED`` shape: one document per run, updated as stages complete.

    :meth:`opened` creates the run's row; :meth:`advanced` moves one stage on
    it. Both buffer and are applied on the way out with the usage event, so a
    handler that raises half-way leaves no half-written run behind.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._ops: list[tuple[str, dict]] = []

    def opened(self, run_id: str, *, stages: list[str], brand: Optional[str] = None,
               brand_id: Optional[str] = None) -> None:
        """A new staged run began — open its row with the stage slots it will fill."""
        self.run_id = run_id
        self.note(brand=brand, brand_id=brand_id)
        self._ops.append(("open", {"stages": list(stages)}))

    def advanced(self, run_id: str, *, stage: int, stage_status: str,
                 asset: Optional[dict] = None, summary: Optional[str] = None,
                 run_status: Optional[str] = None) -> None:
        """One stage of an open run moved (generated / approved / completed)."""
        self.run_id = run_id
        self._ops.append(("advance", {
            "stage_index": stage - 1, "stage_status": stage_status,
            "asset": asset, "summary": summary, "run_status": run_status,
        }))

    def _write(self) -> None:
        for kind, payload in self._ops:
            if kind == "open":
                firestore_repo.start_run(
                    run_id=str(self.run_id),
                    agent_id=self._trail.agent_id,
                    agent_name=self._trail.agent_name,
                    user_id=str(self._user.get("id") or ""),
                    user=str(self._user.get("email") or ""),
                    session_id=str(self._user.get("session_id") or ""),
                    timezone=str(self._user.get("timezone") or "UTC"),
                    brand=self.brand,
                    brand_id=self.brand_id,
                    stages=payload["stages"],
                )
            else:
                firestore_repo.update_run(
                    run_id=str(self.run_id),
                    agent_id=self._trail.agent_id,
                    **payload,
                )
        usage_action = _UNIT_USAGE[self.unit]
        if usage_action is None:
            return
        firestore_repo.log_usage_event(
            user_id=str(self._user.get("id") or ""),
            email=str(self._user.get("email") or ""),
            agent_id=self._trail.agent_id,
            category=self._trail.category,
            action=usage_action,
            count=self.count,
            brand=self.brand_id or self.brand,
        )


# --------------------------------------------------------------------------- #
# The declaration
# --------------------------------------------------------------------------- #

class _Marker:
    """What route introspection reads to know an endpoint declares the trail."""

    __slots__ = ("trail", "action", "unit", "shape")

    def __init__(self, trail: "ActivityTrail", action: str, unit: str, shape: str) -> None:
        self.trail, self.action, self.unit, self.shape = trail, action, unit, shape


class ActivityTrail:
    """One agent's trail. Bound once per router, used at every declaration.

    Replaces the six near-identical ``_track`` adapters that each re-typed the
    same four constants and the same swallow-everything try/except::

        trail = ActivityTrail(agent_id="a9", agent_name="Blog Writer", category="copy")

        @router.post("/blog/runs")
        def create_run(body: RunIn,
                       user: dict = Depends(get_current_user),
                       act: Activity = trail.records("run_created", "New blog run", unit=JOB)):
            ...
            act.note(f"New blog run: “{topic}”", run_id=run["id"])
            return run

    The declaration is a FastAPI dependency, so it sits in the endpoint's
    signature where the route is declared and is visible to route
    introspection — an endpoint the rule covers cannot be added without it,
    and cannot be quietly stripped of it either.
    """

    def __init__(self, *, agent_id: str, agent_name: str, category: str) -> None:
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.category = category

    # -- declarations ------------------------------------------------------- #

    def records(self, action: str, summary: str, *, unit: str = OUTPUT,
                actor: str = CALLER) -> DependsType:
        """Declare this endpoint as one unit of work, in the ``EVENT`` shape.

        ``summary`` is the human line the admin panel shows if the handler
        notes nothing more specific.
        """
        return self._declare(Activity, action, summary, unit, actor, "event")

    def stages(self, action: str, summary: str, *, unit: str = OUTPUT,
               actor: str = CALLER) -> DependsType:
        """Declare this endpoint as part of a staged run (the ``STAGED`` shape)."""
        return self._declare(StagedActivity, action, summary, unit, actor, "staged")

    # -- internals ---------------------------------------------------------- #

    def _declare(self, cls: type[Activity], action: str, summary: str, unit: str,
                 actor: str, shape: str) -> DependsType:
        if unit not in _UNIT_USAGE:
            raise ValueError(f"unknown activity unit: {unit!r}")
        if actor not in (CALLER, CRON):
            raise ValueError(f"unknown activity actor: {actor!r}")

        if actor == CRON:
            def dependency() -> Iterator[Activity]:
                act = cls(self, dict(CRON_USER), action, summary, unit)
                yield act
                # Reached only when the handler returned. If it raised, the
                # exception is thrown in at the yield above and propagates from
                # here — a request that failed is not a unit of work, so
                # nothing is recorded.
                act._flush()
        else:
            def dependency(user: dict = Depends(get_current_user)) -> Iterator[Activity]:  # type: ignore[misc]
                act = cls(self, user, action, summary, unit)
                yield act
                act._flush()

        dependency.__name__ = f"trail_{self.agent_id}_{action}".replace(":", "_")
        dependency.__doc__ = f"Activity trail for {self.agent_name}: {action} ({unit})."
        dependency.__activity_trail__ = _Marker(self, action, unit, shape)  # type: ignore[attr-defined]
        return Depends(dependency)


def silent(reason: str) -> Callable[[F], F]:
    """Declare an endpoint as deliberately outside the trail, with the reason.

    The counterpart to :meth:`ActivityTrail.records`: every mutating endpoint
    on an agent router carries one or the other, so "this one does not log" is
    a statement someone wrote down next to the code rather than an omission
    nobody noticed. It wraps nothing and costs nothing at request time — it
    marks the function and returns it unchanged.
    """
    reason = (reason or "").strip()
    if len(reason) < 15:
        raise ValueError(
            "silent() needs the actual reason this endpoint records nothing, "
            f"not {reason!r} — see THE RULE in app/services/run_tracking.py"
        )

    def mark(fn: F) -> F:
        fn.__activity_silence__ = reason  # type: ignore[attr-defined]
        return fn

    return mark
