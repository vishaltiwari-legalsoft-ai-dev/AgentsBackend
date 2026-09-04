"""GEO agent — the one transactional write primitive every GEO document uses.

Every GEO state doc that can be written by two overlapping callers — the spend
counters on ``geo-config-{brand}``, a day-doc of answers, the history series,
the prompt universe — goes through :func:`mutate`. Read-then-write without it is
a lost update: two overlapping poll steps both read ``used=1990`` against a
2000 cap, each fires ten paid calls, and both write 2000 — ten calls billed,
zero counted, repeatable forever.

Lives in its own module so ``geo_prompts`` (which ``geo_poll`` imports) can use
it without a circular import; ``geo_poll.mutate`` stays as a re-export for the
callers that already spell it that way.

The implementation moved to ``seo_geo_agent.state`` on 2026-09-04, unchanged.
It always transacted over a2's collection through a2's document-ref builder, and
a2's own brand registry — ``state.load("brands")``, one global document that
self-serve brand creation now writes — needed the same guarantee. Two copies of
"read-modify-write, atomically" is one copy too many, and the one that drifts is
the one that stops being atomic. This module keeps the name, the lock object and
the function-shaped seam, so every existing caller, re-export
(``geo_poll._mutate``) and monkeypatch keeps working exactly as before.
"""
from __future__ import annotations

from typing import Any, Callable

from seo_geo_agent import state

#: The same lock object ``state.mutate`` takes offline — ``geo_poll._LOCAL_LOCK``
#: is an alias of this, and a second lock would guard nothing.
LOCAL_LOCK = state.LOCAL_LOCK


def mutate(doc_id: str, change: Callable[[dict], tuple[dict, Any]]) -> Any:
    """Atomic read-modify-write of one state doc; returns ``change``'s result.

    ``change(current) -> (new_doc, result)`` runs INSIDE the transaction and may
    be retried on contention, so it must be a pure function of ``current``.

    A thin function rather than ``mutate = state.mutate``: the indirection is
    resolved per call, so a test that patches ``state.mutate`` (or ``state.load``
    on the offline path) still reaches callers that hold this name.
    """
    return state.mutate(doc_id, change)
