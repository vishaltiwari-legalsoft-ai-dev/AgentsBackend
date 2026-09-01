#!/usr/bin/env python
"""One-off: remove test-identity rows and abandoned in-progress runs.

Two different messes, both visible in the admin Database panel and both
distorting every "how much has this agent run" number:

**Test-identity rows.** ``runs`` carries 520-odd documents written by callers
that do not exist: ``u1`` and ``admin1`` are literally the fixture identities in
``app/routers/tests/conftest.py``, and ``u-smoke``/``creator1``/``local-verify``/
``probe``/``e2e-user`` are the rest of the smoke and verification harnesses. They
are here because the offline guard leaked more than once — the incidents are
written up in ``backend/conftest.py`` — so suites reached production Firestore.
Real callers are either ``cron`` or a 32-hex Firestore user id, which is what
makes these safely separable.

**Abandoned in-progress runs.** 641 of a1's 699 runs sit at
``run_status == "in_progress"`` more than a day after they started. Graphics
Designer is a four-stage interactive pipeline, so users genuinely drop out of it
— but nothing ever closes those rows, so the collection only grows and the panel
reports work that stopped weeks ago as if it were live. Anything still inside the
cutoff is left alone precisely because it may be a real run in flight.

Safety
------
Dry run by default: it counts and shows samples, and exits. Deleting needs both
``--apply`` and ``--i-understand-this-is-permanent``, and the target project and
database are printed first so a wrong target is visible before anything is
touched. Firestore deletes are NOT recoverable without PITR, which the
2026-08-19 audit found switched off.

Anything whose ``user_id`` is neither a known-synthetic identity nor real-shaped
is reported under "unrecognised" and **never deleted** — a new test identity
should show up as a question, not disappear silently.

Usage (from ``backend/``)::

    .venv/Scripts/python scripts/purge_test_and_stale_runs.py
    .venv/Scripts/python scripts/purge_test_and_stale_runs.py --apply \\
        --i-understand-this-is-permanent
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services import firestore_repo  # noqa: E402

#: Identities that only ever existed inside a test, smoke run or manual probe.
SYNTHETIC_USER_IDS = frozenset({
    "u1",                 # conftest DEFAULT_CALLER
    "admin1",             # conftest ADMIN_CALLER
    "creator1",           # creator-gated router suites
    "u-smoke",            # smoke harness
    "local-verify",       # the phantom-workspace verification runs
    "local-verify-gd2",
    "probe",
    "probe-uid",
    "e2e-user",
})

#: The scheduler's own identity — a real writer, never removed.
CRON_USER_ID = "cron"

#: Firestore auto-ids for real users, as issued by ``get_or_create_google_user``.
_REAL_USER_ID = re.compile(r"^[0-9a-f]{32}$")

#: Collections holding run rows keyed by ``user_id``.
RUN_COLLECTIONS = ("runs", "creative_events")

#: Per-agent mirrors of ``runs``; same rows, same identities.
AGENT_RUN_PREFIX = "agent_runs__"

#: Firestore refuses a batch over 500 writes; stay under it.
BATCH_SIZE = 400

#: An in-progress run younger than this may still be live. Left alone.
STALE_AFTER = timedelta(days=1)


def _is_real(user_id: str) -> bool:
    return user_id == CRON_USER_ID or bool(_REAL_USER_ID.match(user_id))


def _plain(text: object) -> str:
    """Console-safe text.

    Run summaries carry emoji and en-dashes straight from brand names and agent
    prose, and this script's whole job is to be read on a Windows console whose
    default codepage is cp1252. A survey that crashes while *printing* what it
    found is worse than useless — it has already done the expensive part.
    """
    return str(text).encode("ascii", "replace").decode("ascii")


def _created(doc: dict) -> datetime | None:
    raw = doc.get("created_at") or doc.get("updated_at") or ""
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _collections() -> list[str]:
    db = firestore_repo._db()
    names = [c.id for c in db.collections()]
    return [n for n in names if n in RUN_COLLECTIONS or n.startswith(AGENT_RUN_PREFIX)]


def survey(cutoff: datetime) -> tuple[dict, dict, Counter, list]:
    """Classify every run row without touching anything."""
    db = firestore_repo._db()
    synthetic: dict[str, list] = {}
    stale: dict[str, list] = {}
    unrecognised: Counter = Counter()
    samples: list[str] = []
    #: agent_id -> run ids whose per-agent mirror must go with the rail row.
    mirrors: dict[str, set[str]] = {}

    for name in _collections():
        for doc in db.collection(name).stream():
            d = doc.to_dict() or {}
            uid = str(d.get("user_id") or "")
            if uid in SYNTHETIC_USER_IDS:
                synthetic.setdefault(name, []).append(doc.reference)
                if len(samples) < 6:
                    samples.append(_plain(f"    [test]  {name}/{doc.id}  user_id={uid!r} "
                                          f"summary={str(d.get('run_summary'))[:60]!r}"))
                continue
            if not _is_real(uid):
                unrecognised[uid] += 1
                continue
            status = str(d.get("run_status") or d.get("status") or "")
            created = _created(d)
            if status == "in_progress" and created and created < cutoff:
                stale.setdefault(name, []).append(doc.reference)
                mirrors.setdefault(str(d.get("agent_id") or ""), set()).add(
                    str(d.get("run_id") or doc.id)
                )
                if len(samples) < 12:
                    samples.append(_plain(f"    [stale] {name}/{doc.id}  created={created:%Y-%m-%d} "
                                          f"summary={str(d.get('run_summary'))[:60]!r}"))

    # Only ``runs`` carries ``run_status``; the per-agent mirrors do not (every
    # real-user row in ``agent_runs__a1`` has it as None). So a stale run found
    # above has a twin here that no status filter would ever reach, and deleting
    # one side alone would leave the admin panel listing a run whose rail entry
    # is gone. Pair them by run id.
    for agent_id, run_ids in mirrors.items():
        if not agent_id:
            continue
        collection = f"{AGENT_RUN_PREFIX}{agent_id}"
        for run_id in run_ids:
            ref = db.collection(collection).document(run_id)
            if ref.get().exists:
                stale.setdefault(collection, []).append(ref)

    return synthetic, stale, unrecognised, samples


def _delete(refs: list) -> int:
    db = firestore_repo._db()
    batch = db.batch()
    pending = deleted = 0
    for ref in refs:
        batch.delete(ref)
        pending += 1
        deleted += 1
        if pending >= BATCH_SIZE:
            batch.commit()
            batch = db.batch()
            pending = 0
    if pending:
        batch.commit()
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="actually delete (default is a dry run that only counts)")
    parser.add_argument("--i-understand-this-is-permanent", action="store_true",
                        dest="confirmed", help="required alongside --apply")
    parser.add_argument("--stale-days", type=float, default=STALE_AFTER.days,
                        help="in-progress runs older than this are abandoned (default 1)")
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.stale_days)
    print(f"project={settings.gcp_project_id!r} database={settings.firestore_database!r}")
    print(f"cutoff: in-progress runs created before {cutoff:%Y-%m-%d %H:%M} UTC\n")

    synthetic, stale, unrecognised, samples = survey(cutoff)
    n_syn = sum(len(v) for v in synthetic.values())
    n_stale = sum(len(v) for v in stale.values())

    print(f"test-identity rows: {n_syn}")
    for name, refs in sorted(synthetic.items()):
        print(f"    {name:26} {len(refs)}")
    print(f"\nabandoned in-progress runs: {n_stale}")
    for name, refs in sorted(stale.items()):
        print(f"    {name:26} {len(refs)}")

    if unrecognised:
        print("\nunrecognised user_ids — NOT deleted, classify them first:")
        for uid, n in unrecognised.most_common():
            print(f"    {uid!r:34} {n}")

    if samples:
        print("\nsamples:")
        print("\n".join(samples))

    if not args.apply:
        print(f"\nDRY RUN — nothing deleted. {n_syn + n_stale} documents would go.")
        print("Re-run with --apply --i-understand-this-is-permanent to perform it.")
        return 0
    if not args.confirmed:
        print("\nRefusing: --apply also needs --i-understand-this-is-permanent.")
        return 2

    removed = 0
    for group in (synthetic, stale):
        for refs in group.values():
            removed += _delete(refs)
    print(f"\nDeleted {removed} documents.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
