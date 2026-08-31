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
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable

from seo_geo_agent import state

# Offline state is plain JSON files, so a process-local lock is the whole
# guarantee there; that is enough for tests and single-process local dev.
LOCAL_LOCK = threading.Lock()


def mutate(doc_id: str, change: Callable[[dict], tuple[dict, Any]]) -> Any:
    """Atomic read-modify-write of one state doc; returns ``change``'s result.

    ``change(current) -> (new_doc, result)`` runs INSIDE the transaction and may
    be retried on contention, so it must be a pure function of ``current``.
    """
    if state.use_cloud():
        from google.cloud import firestore

        from app.services import firestore_repo

        # state owns the collection naming — reuse its ref builder rather than
        # keep a second copy of it here. Same cached client the txn runs on.
        ref = state._firestore_doc(doc_id)
        transaction = firestore_repo._db().transaction()

        @firestore.transactional
        def _apply(txn) -> Any:
            snap = ref.get(transaction=txn)
            current = snap.to_dict() if snap.exists else {}
            new_doc, result = change(current or {})
            # same JSON round-trip state.save uses: no dataclasses/dates/sets
            txn.set(ref, json.loads(json.dumps(new_doc, default=str)))
            return result

        return _apply(transaction)

    with LOCAL_LOCK:
        new_doc, result = change(state.load(doc_id) or {})
        state.save(doc_id, new_doc)
        return result
