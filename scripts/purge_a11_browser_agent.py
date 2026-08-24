#!/usr/bin/env python
"""One-off: delete the Browser Agent (a11) data left behind by its removal.

The code is gone, so nothing reads these documents any more — they just sit in
Firestore costing storage and showing up in the admin Database panel as an agent
that no longer exists. This script is the deliberate, reviewable way to remove
them; it is NOT wired into the app and nothing imports it.

Four places held a11 data:

======================  ===================================================
what                    where
======================  ===================================================
agent state             collection ``browser_agent`` (every document:
                        ``run-*``, ``runs-index``, ``skill-*``,
                        ``skills-index``, ``digest-*``, ``digests-index``,
                        ``config-*``, ``config-global``)
per-agent run table     collection ``agent_runs__a11`` (every document)
master run rail         collection ``runs``, documents with
                        ``agent_id == "a11"`` — shared with every other
                        agent, so this is a FILTERED delete, never a wipe
usage analytics         collection ``creative_events``, documents with
                        ``agent_id == "a11"`` — likewise filtered
======================  ===================================================

Safe by default: it reports what it would delete and exits. Deleting requires
both ``--apply`` and ``--i-understand-this-is-permanent``, and it prints the
project + database it is pointed at first so a wrong target is visible before
anything is touched. Firestore document deletes are NOT recoverable without
PITR, which the 2026-08-19 audit found switched off — so read the header line.

Usage (from ``backend/``)::

    .venv/Scripts/python scripts/purge_a11_browser_agent.py            # dry run
    .venv/Scripts/python scripts/purge_a11_browser_agent.py --apply \\
        --i-understand-this-is-permanent

Credentials come from the ambient service account, exactly as the app's own
Firestore client does — see ``app/services/firestore_repo._db``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Import the app the same way the service does, so project/database come from
# one place and this script can never point somewhere the app does not.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services import firestore_repo  # noqa: E402

AGENT_ID = "a11"

#: Collections that existed only for a11 — every document goes.
WHOLE_COLLECTIONS = ("browser_agent", f"agent_runs__{AGENT_ID}")

#: Shared collections — only the rows this agent wrote go. Never wipe these.
FILTERED_COLLECTIONS = ("runs", "creative_events")

#: Firestore refuses a batch over 500 writes; stay under it.
BATCH_SIZE = 400


def _count_whole(name: str) -> int:
    return sum(1 for _ in firestore_repo._db().collection(name).stream())


def _count_filtered(name: str) -> int:
    query = firestore_repo._db().collection(name).where("agent_id", "==", AGENT_ID)
    return sum(1 for _ in query.stream())


def _delete(docs) -> int:
    """Delete a document stream in batches. Returns how many were removed."""
    db = firestore_repo._db()
    batch = db.batch()
    pending = 0
    deleted = 0
    for doc in docs:
        batch.delete(doc.reference)
        pending += 1
        deleted += 1
        if pending >= BATCH_SIZE:
            batch.commit()
            batch = db.batch()
            pending = 0
    if pending:
        batch.commit()
    return deleted


def survey() -> dict[str, int]:
    """How many documents each step would remove."""
    found = {name: _count_whole(name) for name in WHOLE_COLLECTIONS}
    found.update({name: _count_filtered(name) for name in FILTERED_COLLECTIONS})
    return found


def purge() -> dict[str, int]:
    """Do it. Returns how many documents each step actually removed."""
    db = firestore_repo._db()
    removed: dict[str, int] = {}
    for name in WHOLE_COLLECTIONS:
        removed[name] = _delete(db.collection(name).stream())
    for name in FILTERED_COLLECTIONS:
        removed[name] = _delete(
            db.collection(name).where("agent_id", "==", AGENT_ID).stream()
        )
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply", action="store_true",
        help="actually delete (default is a dry run that only counts)",
    )
    parser.add_argument(
        "--i-understand-this-is-permanent", dest="confirmed", action="store_true",
        help="required alongside --apply; Firestore deletes do not come back",
    )
    args = parser.parse_args()

    project = settings.require("gcp_project_id")
    database = settings.firestore_database
    print(f"target: project={project} database={database}")
    print(f"agent:  {AGENT_ID} (Browser Agent, removed from the codebase)")
    print()

    counts = survey()
    for name in WHOLE_COLLECTIONS:
        print(f"  {name:<24} {counts[name]:>6} document(s)  [whole collection]")
    for name in FILTERED_COLLECTIONS:
        print(f"  {name:<24} {counts[name]:>6} document(s)  [agent_id == {AGENT_ID}]")
    total = sum(counts.values())
    print(f"  {'TOTAL':<24} {total:>6}")
    print()

    if not total:
        print("Nothing to delete.")
        return 0

    if not args.apply:
        print("Dry run — nothing deleted. Re-run with:")
        print("    --apply --i-understand-this-is-permanent")
        return 0

    if not args.confirmed:
        print("Refusing: --apply needs --i-understand-this-is-permanent too.")
        return 1

    removed = purge()
    print("deleted:")
    for name, n in removed.items():
        print(f"  {name:<24} {n:>6}")
    print(f"  {'TOTAL':<24} {sum(removed.values()):>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
