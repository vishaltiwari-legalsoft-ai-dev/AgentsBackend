"""How the record is read out of Firestore, and what it does when it cannot be.

`list_runs_for_user` has three jobs beyond "return the rows", and each is here
because getting it wrong is invisible in a green suite:

1. **It filters before it orders.** The composite index it wants may not exist,
   and the fallback must carry the same `user_id` filter — a fallback that
   widens the query is a cross-tenant read that only appears in production.

2. **It does not pay for a refusal twice.** Firestore rejects an unindexed
   ordered query *before* running it, so an un-memoised fallback burns a full
   round-trip on every page load. Measured at roughly a second of the 2.3 the
   endpoint took on an empty collection.

3. **It comes back.** Remembering the refusal for ever would mean creating the
   index changes nothing until someone redeploys, so the memo expires.
"""

from __future__ import annotations

from typing import Any

import app  # noqa: F401 - side effect: registers agent roots on sys.path
import pytest
from google.api_core import exceptions as gexc

from app.services import firestore_repo


class FakeQuery:
    """The two shapes of query this module builds, and a count over either."""

    def __init__(self, store: "FakeStore", *, ordered: bool = False) -> None:
        self._store = store
        self._ordered = ordered

    def order_by(self, field: str, direction: Any = None) -> "FakeQuery":
        self._store.ordered_attempts += 1
        return FakeQuery(self._store, ordered=True)

    def limit(self, n: int) -> "FakeQuery":
        self._store.limits.append(n)
        q = FakeQuery(self._store, ordered=self._ordered)
        q._n = n  # type: ignore[attr-defined]
        return q

    def stream(self):
        if self._ordered and not self._store.index_exists:
            raise gexc.FailedPrecondition("The query requires an index.")
        self._store.streams += 1
        rows = self._store.rows
        if self._ordered:
            rows = sorted(rows, key=lambda r: r["created_at"], reverse=True)
        return [FakeDoc(r) for r in rows]

    def count(self) -> "FakeQuery":
        return self

    def get(self):
        return [[type("V", (), {"value": len(self._store.rows)})()]]


class FakeDoc:
    def __init__(self, data: dict) -> None:
        self._data = data
        self.id = data["id"]

    def to_dict(self) -> dict:
        return dict(self._data)


class FakeStore:
    def __init__(self, rows: list[dict], *, index_exists: bool) -> None:
        self.rows = rows
        self.index_exists = index_exists
        self.filters: list[tuple] = []
        self.ordered_attempts = 0
        self.streams = 0
        self.limits: list[int] = []

    # the two objects the repo reaches through
    def collection(self, name: str) -> "FakeStore":
        assert name == "runs"
        return self

    def where(self, filter: Any = None) -> FakeQuery:  # noqa: A002 - Firestore's own kwarg
        self.filters.append((filter.field, filter.op, filter.value))
        return FakeQuery(self)


class Filter:
    def __init__(self, field: str, op: str, value: Any) -> None:
        self.field, self.op, self.value = field, op, value


def _row(i: int, user: str, when: str) -> dict:
    return {"id": f"r{i}", "run_id": f"r{i}", "user_id": user, "created_at": when}


@pytest.fixture()
def store(monkeypatch):
    """Install a fake Firestore and reset the module's index memo."""
    made: dict[str, FakeStore] = {}

    def _install(rows: list[dict], *, index_exists: bool) -> FakeStore:
        s = FakeStore(rows, index_exists=index_exists)
        made["s"] = s
        monkeypatch.setattr(firestore_repo, "_db", lambda: s)
        return s

    monkeypatch.setattr(firestore_repo.firestore, "FieldFilter", Filter)
    monkeypatch.setattr(firestore_repo, "_runs_index_missing_at", None, raising=False)
    yield _install
    monkeypatch.setattr(firestore_repo, "_runs_index_missing_at", None, raising=False)


def test_the_ordered_read_is_used_when_the_index_exists(store) -> None:
    s = store([_row(1, "u1", "2026-08-01"), _row(2, "u1", "2026-08-03")], index_exists=True)

    rows = firestore_repo.list_runs_for_user("u1", limit=10)

    assert [r["id"] for r in rows] == ["r2", "r1"]
    assert s.ordered_attempts == 1
    assert s.filters == [("user_id", "==", "u1")]


def test_the_fallback_carries_the_same_user_filter(store) -> None:
    """A fallback that widened the query would be a cross-tenant read that only
    ever appears in production, where the index is the thing that is missing."""
    s = store([_row(1, "u1", "2026-08-01"), _row(2, "u1", "2026-08-05")], index_exists=False)

    rows = firestore_repo.list_runs_for_user("u1", limit=10)

    assert [r["id"] for r in rows] == ["r2", "r1"], "sorted in process, newest first"
    assert s.filters == [("user_id", "==", "u1")], "the filter is not dropped by the fallback"


def test_a_refused_ordered_read_is_not_attempted_again_on_the_next_call(store) -> None:
    s = store([_row(1, "u1", "2026-08-01")], index_exists=False)

    firestore_repo.list_runs_for_user("u1")
    firestore_repo.list_runs_for_user("u1")
    firestore_repo.list_runs_for_user("u1")

    assert s.ordered_attempts == 1, (
        "Firestore refuses an unindexed ordered query before running it, so "
        "re-attempting it burns a round-trip on every page load"
    )


def test_the_memo_expires_so_creating_the_index_takes_effect(store, monkeypatch) -> None:
    s = store([_row(1, "u1", "2026-08-01")], index_exists=False)
    firestore_repo.list_runs_for_user("u1")
    assert s.ordered_attempts == 1

    # The index is built. Without an expiry the process would keep falling back
    # until someone redeployed it.
    clock = [0.0]
    monkeypatch.setattr(firestore_repo.time, "monotonic", lambda: clock[0])
    firestore_repo._runs_index_missing_at = 0.0
    s.index_exists = True

    clock[0] = firestore_repo._RUNS_INDEX_RETRY_SECONDS - 1
    firestore_repo.list_runs_for_user("u1")
    assert s.ordered_attempts == 1, "still inside the retry window"

    clock[0] = firestore_repo._RUNS_INDEX_RETRY_SECONDS + 1
    firestore_repo.list_runs_for_user("u1")
    assert s.ordered_attempts == 2, "the window passed, so the fast path is tried again"


def test_a_caller_with_no_id_reads_nothing_at_all(store) -> None:
    """An unauthenticated-shaped caller must not fall through to a bare
    collection read. `[]` here is a refusal, not an empty result."""
    s = store([_row(1, "u1", "2026-08-01")], index_exists=True)

    assert firestore_repo.list_runs_for_user("") == []
    assert s.streams == 0 and s.filters == []
