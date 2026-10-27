"""An in-memory Firestore for the PR Writer (a13) store and router tests.

Subcollections, ``array_contains``, multi-field ordering, cursors,
create-only writes and ``get_all``, patched over ``firestore_repo._db`` /
``_transact``. Transaction writes are queued and land only when the callback
returns, so a raise mid-transaction writes nothing, as in Firestore. The
offline guard in ``backend/conftest.py`` stays in force: nothing here can
reach a real database.

Shared by ``tests/test_pr_writer_store.py`` (the store) and
``app/routers/tests/test_pr_writer_*.py`` (the router over the real store).
"""
from __future__ import annotations

import json

from google.api_core import exceptions as gexc
from google.cloud import firestore as gfs

from app.services import firestore_repo as repo

def _copy(value):
    return json.loads(json.dumps(value))


class _Snap:
    def __init__(self, ref, data):
        self.reference, self.id = ref, ref.id
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return None if self._data is None else _copy(self._data)


class _Ref:
    def __init__(self, db, path):
        self._db, self.path = db, path
        self.id = path.rsplit("/", 1)[-1]

    def collection(self, name):
        return _Query(self._db, f"{self.path}/{name}")

    def get(self, transaction=None, **_):
        self._db.reads += 1
        return _Snap(self, self._db.docs.get(self.path))

    def set(self, data, merge=False):
        if merge and self.path in self._db.docs:
            self._db.docs[self.path].update(_copy(data))
        else:
            self._db.docs[self.path] = _copy(data)

    def create(self, data):
        if self.path in self._db.docs:
            raise gexc.AlreadyExists(f"{self.path} exists")
        self._db.docs[self.path] = _copy(data)

    def update(self, data):
        if self.path not in self._db.docs:
            raise gexc.NotFound(self.path)
        self._db.docs[self.path].update(_copy(data))


class _Query:
    def __init__(self, db, col, filters=(), orders=(), limit_n=None, after=None, fields=None):
        self._db, self._col = db, col
        self._filters, self._orders = tuple(filters), tuple(orders)
        self._limit, self._after, self._fields = limit_n, after, fields

    def _with(self, **kw):
        base = dict(filters=self._filters, orders=self._orders, limit_n=self._limit,
                    after=self._after, fields=self._fields)
        return _Query(self._db, self._col, **(base | kw))

    def document(self, doc_id):
        return _Ref(self._db, f"{self._col}/{doc_id}")

    def where(self, filter):
        assert filter.op_string in ("==", "array_contains"), filter.op_string
        return self._with(filters=self._filters + ((filter.field_path, filter.op_string, filter.value),))

    def order_by(self, field, direction=gfs.Query.ASCENDING):
        return self._with(orders=self._orders + ((field, direction == gfs.Query.DESCENDING),))

    def start_after(self, values):
        return self._with(after=dict(values))

    def select(self, fields):
        return self._with(fields=list(fields))

    def limit(self, n):
        return self._with(limit_n=n)

    def stream(self, transaction=None, **_):
        array = any(op == "array_contains" for _, op, _v in self._filters)
        if self._db.composite_missing and array and self._orders:
            raise RuntimeError("400 The query requires an index")
        self._db.streams.append((self._col, self._filters, self._orders))
        prefix = self._col + "/"
        rows = []
        for path, data in self._db.docs.items():
            if not path.startswith(prefix) or "/" in path[len(prefix):]:
                continue
            ok = all(
                (value in (data.get(f) or [])) if op == "array_contains" else data.get(f) == value
                for f, op, value in self._filters)
            if ok:
                rows.append((path.rsplit("/", 1)[-1], data))

        def val(row, field):
            return row[0] if field == "__name__" else row[1].get(field)

        for field, desc in reversed(self._orders):
            rows.sort(key=lambda r, f=field: (val(r, f) is None, val(r, f) or ""), reverse=desc)
        if self._after is not None:
            def is_after(row):
                for field, desc in self._orders:
                    if field not in self._after:
                        break
                    a, b = val(row, field), self._after[field]
                    if a != b:
                        return (a < b) if desc else (a > b)
                return False
            rows = [r for r in rows if is_after(r)]
        if self._limit is not None:
            rows = rows[: self._limit]
        self._db.reads += len(rows)
        out = []
        for doc_id, data in rows:
            shown = {k: v for k, v in data.items() if k in self._fields} if self._fields else data
            out.append(_Snap(_Ref(self._db, f"{self._col}/{doc_id}"), shown))
        return out


class _Db:
    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.streams: list = []
        self.composite_missing = False
        self.reads = 0

    def collection(self, name):
        return _Query(self, name)

    def doc(self, path):
        return self.docs.get(path)

    def get_all(self, refs):
        for ref in refs:
            yield ref.get()


class _Txn:
    def __init__(self):
        self.ops: list = []

    def set(self, ref, data, merge=False):
        self.ops.append(("set", ref, data, merge))

    def create(self, ref, data):
        self.ops.append(("create", ref, data, None))

    def update(self, ref, data):
        self.ops.append(("update", ref, data, None))


def install(monkeypatch) -> "_Db":
    """Patch ``firestore_repo`` onto a fresh in-memory database; returns it."""
    mem = _Db()

    def _transact(fn):
        txn = _Txn()
        result = fn(txn)
        for kind, ref, _data, _m in txn.ops:  # all-or-nothing, like a commit
            if kind == "create" and ref.path in mem.docs:
                raise gexc.AlreadyExists(f"{ref.path} exists")
        for kind, ref, data, merge in txn.ops:
            if kind == "set":
                ref.set(data, merge=merge)
            elif kind == "create":
                ref.create(data)
            else:
                ref.update(data)
        return result

    monkeypatch.setattr(repo, "_db", lambda: mem)
    monkeypatch.setattr(repo, "_transact", _transact)
    monkeypatch.setattr(repo, "_pr_runs_index_missing_at", None)
    return mem
