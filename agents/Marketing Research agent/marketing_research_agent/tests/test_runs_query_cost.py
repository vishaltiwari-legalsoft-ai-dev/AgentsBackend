"""``mr_runs`` reads must be scoped to ONE workspace, server-side.

The defect: ``_cloud_list`` was a bare ``.stream()`` — no ``where``, no
``limit`` — and ``list_runs`` filtered by user in Python afterwards. Firestore
bills per document RETURNED, so every MR read paid for (and shipped) every other
workspace's runs, and ``_load_dataset`` did it three times per request.

These tests record the query that is actually built, so the filter cannot quietly
migrate back into Python.
"""
import pytest

from marketing_research_agent import runs


class _FakeQuery:
    """Records the filters applied to it; streams nothing."""

    def __init__(self, log):
        self.log = log

    def where(self, filter=None, **_kw):  # noqa: A002 - matches the client's kwarg
        self.log.append((filter.field_path, filter.op_string, filter.value))
        return self

    def stream(self):
        return iter(())


@pytest.fixture
def query_log(monkeypatch):
    log: list[tuple] = []
    monkeypatch.setattr(runs, "_collection", lambda: _FakeQuery(log))
    return log


def test_the_user_filter_is_pushed_into_the_query(query_log):
    runs._cloud_list("u1")
    assert ("user_id", "==", "u1") in query_log, (
        f"no server-side user filter — the query was {query_log}")


def test_the_kind_filter_is_pushed_into_the_query(query_log):
    runs._cloud_list("u1", "dataset")
    assert ("user_id", "==", "u1") in query_log
    assert ("kind", "==", "dataset") in query_log, (
        f"no server-side kind filter — the query was {query_log}")


def test_several_kinds_become_one_in_filter_not_a_full_scan(query_log):
    runs._cloud_list("u1", ("dataset", "official_spend", "lead_analysis"))
    ops = {(f, op) for f, op, _ in query_log}
    assert ("kind", "in") in ops, f"expected an 'in' filter, got {query_log}"
    assert ("user_id", "==") in ops


def test_an_unscoped_call_is_still_possible_but_explicit(query_log):
    """``list_runs()`` with no user is the admin/whole-collection case. It must
    stay reachable — just never the default any endpoint takes."""
    runs._cloud_list()
    assert query_log == []


def test_kind_is_also_applied_to_the_local_copies(tmp_path, monkeypatch):
    """The disk half bypasses the query, so the Python filter must stay too —
    otherwise a local run of the wrong kind leaks into a filtered read."""
    monkeypatch.setenv("MR_OFFLINE", "1")
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    runs.save_run({"id": "d1", "kind": "dataset", "user_id": "u1"})
    runs.save_run({"id": "o1", "kind": "official_spend", "user_id": "u1"})
    runs.save_run({"id": "d2", "kind": "dataset", "user_id": "u2"})
    assert {r["id"] for r in runs.list_runs("u1", kind="dataset")} == {"d1"}
    assert {r["id"] for r in runs.list_runs("u1")} == {"d1", "o1"}
