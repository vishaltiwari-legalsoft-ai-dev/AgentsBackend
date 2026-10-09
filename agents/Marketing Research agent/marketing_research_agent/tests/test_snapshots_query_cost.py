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


# --- bounded sweep reads: an in-memory store that EVALUATES the query ---------

_OPS = {"==": lambda a, b: a == b, ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b}


class _Doc:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _EvalQuery:
    """Applies where/order_by/limit/select like Firestore, and records every
    streamed query so a test can assert what was billed."""

    def __init__(self, docs, log, filters=(), order=None, limit=None, fields=None):
        self.docs, self.log = docs, log
        self.filters, self.order, self._limit, self.fields = list(filters), order, limit, fields

    def _with(self, **kw):
        state = dict(filters=self.filters, order=self.order, limit=self._limit,
                     fields=self.fields)
        state.update(kw)
        return _EvalQuery(self.docs, self.log, **state)

    def where(self, filter=None, **_kw):  # noqa: A002
        return self._with(filters=self.filters + [
            (filter.field_path, filter.op_string, filter.value)])

    def order_by(self, field, direction="ASCENDING"):
        return self._with(order=(field, direction))

    def limit(self, n):
        return self._with(limit=n)

    def select(self, fields):
        return self._with(fields=list(fields))

    def stream(self):
        rows = [d for d in self.docs
                if all(f in d and _OPS[op](d[f], v) for f, op, v in self.filters)]
        if self.order:
            rows.sort(key=lambda d: d.get(self.order[0]) or "",
                      reverse=self.order[1] == "DESCENDING")
        if self._limit is not None:
            rows = rows[: self._limit]
        if self.fields:
            rows = [{k: d[k] for k in self.fields if k in d} for d in rows]
        self.log.append({"filters": self.filters, "limit": self._limit,
                         "fields": self.fields, "returned": len(rows)})
        return iter(_Doc(d) for d in rows)


class _EvalDb:
    def __init__(self, docs, log):
        self.docs, self.log = docs, log

    def collection(self, name):
        assert name == "mr_snapshots"
        return _EvalQuery(self.docs, self.log)


def _day(d, slugs, *, at="T23:46:33", gap_s=0.1):
    """One capture run on ``d``: a doc per slug, ``gap_s`` apart."""
    from datetime import datetime, timedelta
    t0 = datetime.fromisoformat(f"{d}{at}+00:00")
    out = []
    for i, slug in enumerate(slugs):
        s = _snap(slug, d)
        s["captured_at"] = (t0 + timedelta(seconds=i * gap_s)).isoformat()
        out.append(s)
    return out


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    from app.services import firestore_repo

    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))   # no local copies
    docs: list[dict] = []
    log: list[dict] = []
    monkeypatch.setattr(snapshots, "_use_cloud", lambda: True)
    monkeypatch.setattr(firestore_repo, "_db", lambda: _EvalDb(docs, log))
    return docs, log


def _a_busy_history():
    """60 days of three vendors + roll-up, so a whole-collection read would be
    240 docs and obviously different from a bounded one."""
    from datetime import date, timedelta
    docs = []
    start = date(2026, 7, 10)
    for i in range(60):
        d = (start + timedelta(days=i)).isoformat()
        docs += _day(d, ["meta-360-ra", "hawksem-ls-google", "website",
                         "marketing-2026-overall-report"])
    return docs


def test_the_portfolio_bar_never_reads_the_whole_collection(cloud):
    """``portfolio()`` streamed every doc in ``mr_snapshots`` (~1,650 docs /
    ~46 MB in 2026-10) on every Vendors-tab load to find one day's sweep."""
    docs, log = cloud
    docs += _a_busy_history()
    out = snapshots.portfolio()
    assert out is not None and out["date"] == "2026-09-07"
    assert all(q["filters"] or q["limit"] == 1 for q in log), f"an unbounded stream: {log}"
    assert sum(q["returned"] for q in log) == 2 * 4 + 2, log   # two sweeps + two date probes
    # the full-doc read is the newest day only; the prior sweep is a projection
    full = [q for q in log if q["fields"] is None]
    assert [q["filters"] for q in full] == [[("date", "==", "2026-09-07")]]


def test_finding_a_months_newest_sweep_is_one_doc_on_a_single_field(cloud):
    """No composite index: the month's newest date is a range + order on the
    SAME field (``date``), limit 1, projected to ``date``."""
    docs, log = cloud
    docs += _a_busy_history()
    sw = snapshots.vendor_sweep("2026-08")
    assert sw["date"] == "2026-08-31"
    probe, day = log
    assert {f for f, _op, _v in probe["filters"]} == {"date"}
    assert probe["limit"] == 1 and probe["fields"] == ["date"] and probe["returned"] == 1
    assert day["filters"] == [("date", "==", "2026-08-31")] and day["returned"] == 4


def test_a_sweep_read_that_fails_is_an_outage_not_an_empty_month(monkeypatch, tmp_path):
    from app.services import firestore_repo

    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    monkeypatch.setattr(snapshots, "_use_cloud", lambda: True)

    def _dead():
        raise RuntimeError("503 the datastore is unavailable")

    monkeypatch.setattr(firestore_repo, "_db", _dead)
    with pytest.raises(snapshots.SnapshotStoreError):
        snapshots.vendor_sweep("2026-09")
    with pytest.raises(snapshots.SnapshotStoreError):
        snapshots.portfolio()


def test_the_month_picker_reads_projections_only_never_the_collection(cloud):
    """``sweep_months`` runs on a panel load: every query it bills is a
    ``date`` probe (limit 1) or a projected ``date ==`` membership read."""
    docs, log = cloud
    docs += _a_busy_history()                       # 2026-07-10 .. 2026-09-07
    assert snapshots.sweep_months() == ["2026-09", "2026-08", "2026-07"]
    assert log and all(q["filters"] or q["limit"] == 1 for q in log), log
    assert all(q["fields"] for q in log), "a full-document read was billed"
    probes = [q for q in log if q["limit"] == 1]
    days = [q for q in log if q["limit"] is None]
    assert len(probes) == 2 + 3 and len(days) == 3   # newest+oldest, 1 per month
    assert all(q["filters"][0][:2] == ("date", "==") for q in days)
    assert sum(q["returned"] for q in log) == 5 + 3 * 4


def test_the_month_picker_raises_on_a_dead_store(monkeypatch, tmp_path):
    from app.services import firestore_repo

    monkeypatch.setenv("MR_SNAPSHOTS_DIR", str(tmp_path))
    monkeypatch.setattr(snapshots, "_use_cloud", lambda: True)

    def _dead():
        raise RuntimeError("503 the datastore is unavailable")

    monkeypatch.setattr(firestore_repo, "_db", _dead)
    with pytest.raises(snapshots.SnapshotStoreError):
        snapshots.sweep_months()
