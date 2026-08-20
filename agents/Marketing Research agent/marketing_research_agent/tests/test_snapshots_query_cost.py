"""``mr_snapshots`` reads must not ship the whole collection for one vendor.

``mr_snapshots`` carries NO tenant key (doc id = ``{slug}_{date}``), so it CANNOT
be scoped per workspace without a schema change and a backfill — that is
deliberately out of scope here (see docs/db-target-design.html). What is safe and
correct today is pushing the filters the caller already asked for — vendor slug
and month — into the query instead of applying them after the documents are on
the wire.
"""
import pytest

from marketing_research_agent import snapshots


class _FakeQuery:
    def __init__(self, log):
        self.log = log

    def where(self, filter=None, **_kw):  # noqa: A002 - matches the client's kwarg
        self.log.append((filter.field_path, filter.op_string, filter.value))
        return self

    def stream(self):
        return iter(())


class _FakeDb:
    def __init__(self, log):
        self.log = log

    def collection(self, _name):
        return _FakeQuery(self.log)


@pytest.fixture
def query_log(monkeypatch):
    from app.services import firestore_repo

    log: list[tuple] = []
    monkeypatch.setattr(firestore_repo, "_db", lambda: _FakeDb(log))
    return log


def test_a_vendor_read_filters_on_the_slug(query_log):
    snapshots._cloud_list("meta-360-ra")
    assert ("vendor_slug", "==", "meta-360-ra") in query_log, query_log


def test_a_month_read_filters_on_the_month(query_log):
    snapshots._cloud_list(None, "2026-08")
    assert ("month", "==", "2026-08") in query_log, query_log


def test_the_export_path_asks_for_one_vendor_month(query_log):
    snapshots._cloud_list("meta-360-ra", "2026-08")
    fields = {f for f, _op, _v in query_log}
    assert fields == {"vendor_slug", "month"}, query_log


def test_an_unfiltered_listing_is_still_possible(query_log):
    snapshots._cloud_list()
    assert query_log == []


def _snap(slug="meta-360-ra", d="2026-02-07"):
    return {"vendor": "Meta 360 RA", "vendor_slug": slug, "gid": 1, "date": d,
            "month": d[:7], "captured_at": f"{d}T18:00:00+00:00",
            "raw": {"team_overall": [], "channels": {}},
            "canonical": {"team_overall": {"spend": {"performance": 100.0,
                                                     "investment": None}},
                          "channels": {}},
            "prev_month_raw": {"team_overall": [], "channels": {}}}


def test_the_portfolio_bar_fetches_the_store_once_not_twice(monkeypatch, tmp_path):
    """``portfolio()`` listed the vendors AND then looked up the roll-up, and
    each of those was its own full scan of the same collection."""
    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_OFFLINE", "1")
    snapshots.save_snapshot(_snap())            # a vendor, so the bar has rows
    snapshots.save_snapshot(_snap("overall"))   # and a roll-up to look up
    fetches = [0]

    def _counted(*_a, **_kw):
        fetches[0] += 1
        return []

    monkeypatch.setattr(snapshots, "_use_cloud", lambda: True)
    monkeypatch.setattr(snapshots, "_cloud_list", _counted)
    out = snapshots.portfolio()
    assert out is not None, "fixture must reach the roll-up lookup"
    assert fetches[0] == 1, f"{fetches[0]} full snapshot fetches for one bar"
