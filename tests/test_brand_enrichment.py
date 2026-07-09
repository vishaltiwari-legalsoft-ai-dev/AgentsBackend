# backend/tests/test_brand_enrichment.py
from app.services import firestore_repo


class _FakeDoc:
    def __init__(self, store, doc_id):
        self._store, self._id = store, doc_id

    def set(self, payload, merge=False):
        cur = self._store.setdefault(self._id, {})
        if merge:
            _deep_merge(cur, payload)
        else:
            self._store[self._id] = payload

    def get(self):
        class _Snap:
            exists = self._id in self._store
            id = self._id

            def to_dict(inner):
                return dict(self._store.get(self._id, {}))
        return _Snap()


def _deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


class _FakeCol:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return _FakeDoc(self._store, doc_id)


class _FakeDb:
    def __init__(self):
        self.brands = {}

    def collection(self, name):
        return _FakeCol(self.brands)


def test_update_brand_metadata_merges_without_clobbering(monkeypatch):
    db = _FakeDb()
    db.brands["b1"] = {"brand_name": "Acme",
                        "brand_metadata": {"source_folder": "Acme", "fonts": ["Old Font"]}}
    monkeypatch.setattr(firestore_repo, "_db", lambda: db)

    firestore_repo.update_brand_metadata("b1", {"primary_colors": ["#1A2B3C"],
                                                 "fonts": ["Inter Bold"]})

    meta = db.brands["b1"]["brand_metadata"]
    assert meta["source_folder"] == "Acme"          # untouched key preserved
    assert meta["primary_colors"] == ["#1A2B3C"]    # new key added
    assert meta["fonts"] == ["Inter Bold"]          # owned key updated
    assert db.brands["b1"]["brand_name"] == "Acme"  # sibling doc keys preserved
