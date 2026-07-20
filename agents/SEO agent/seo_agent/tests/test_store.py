from seo_agent import store


class _FakeSnap:
    """Stand-in for a Firestore DocumentSnapshot."""

    def __init__(self, data: dict | None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _FakeDocRef:
    def __init__(self, data: dict | None):
        self._data = data

    def get(self):
        return _FakeSnap(self._data)


class _FakeCollection:
    """Minimal Firestore collection stand-in keyed by doc id."""

    def __init__(self, docs: dict):
        self._docs = docs

    def document(self, doc_id):
        return _FakeDocRef(self._docs.get(doc_id))

    def stream(self):
        return [_FakeSnap(d) for d in self._docs.values()]


def _fake_collection_router(mapping: dict):
    """``store._collection`` replacement: routes by collection name to a
    ``_FakeCollection`` seeded with that collection's docs (default: empty)."""

    def _get(name: str):
        return _FakeCollection(mapping.get(name, {}))

    return _get


def _benchmark(bid: str, keyword: str) -> dict:
    return {
        "id": bid, "keyword": keyword, "location": "US", "brand": "legalsoft",
        "created_at": f"2026-07-20T00:00:0{bid[-1]}Z", "serp_fetched_at": "t",
        "term_targets": [{"term": "intake", "weight": 1.0, "min_count": 1, "max_count": 3}],
        "topics_ai": True,
    }


def test_benchmark_roundtrip_and_meta_listing():
    store.save_benchmark(_benchmark("b00000000001", "kw one"))
    store.save_benchmark(_benchmark("b00000000002", "kw two"))
    got = store.get_benchmark("b00000000001")
    assert got["keyword"] == "kw one"
    assert got["term_targets"][0]["term"] == "intake"
    metas = store.list_benchmarks()
    assert [m["keyword"] for m in metas] == ["kw two", "kw one"]   # newest first
    assert "term_targets" not in metas[0]                          # meta only


def test_get_missing_returns_none():
    assert store.get_benchmark("nope") is None
    assert store.get_geo_run("nope") is None


def test_geo_runs_filter_by_brand():
    store.save_geo_run({"id": "g1", "brand": "legalsoft", "week": "2026-W29",
                        "captured_at": "2026-07-13T00:00:00Z", "score": 6.0})
    store.save_geo_run({"id": "g2", "brand": "medvirtual", "week": "2026-W30",
                        "captured_at": "2026-07-20T00:00:00Z", "score": 7.0})
    assert [r["id"] for r in store.list_geo_runs()] == ["g2", "g1"]
    assert [r["id"] for r in store.list_geo_runs(brand="legalsoft")] == ["g1"]


def test_config_roundtrip_empty_default():
    assert store.load_config() == {}
    store.save_config({"w_term_coverage": 0.5, "brands": {"legalsoft": {"domain": "legalsoft.com"}}})
    assert store.load_config()["w_term_coverage"] == 0.5


def test_new_id_is_12_hex():
    nid = store.new_id()
    assert len(nid) == 12
    int(nid, 16)  # parses as hex


# --------------------------------------------------------------------------- #
# I1: Cloud Run disk is ephemeral — reads must fall back to / merge Firestore.
# --------------------------------------------------------------------------- #

def test_get_benchmark_falls_back_to_cloud_when_absent_from_disk(monkeypatch):
    cloud_doc = {"id": "cloud-b1", "keyword": "cloud kw", "created_at": "2026-07-19T00:00:00Z",
                 "term_targets": [], "topics_ai": True}
    monkeypatch.setattr(store, "_use_cloud", lambda: True)
    monkeypatch.setattr(store, "_collection",
                        _fake_collection_router({store._BENCH_COLLECTION: {"cloud-b1": cloud_doc}}))
    assert store.get_benchmark("cloud-b1") == cloud_doc
    assert store.get_benchmark("truly-missing") is None


def test_list_benchmarks_merges_cloud_docs_absent_from_disk(monkeypatch):
    store.save_benchmark(_benchmark("bdisk0000001", "disk kw"))
    cloud_doc = {"id": "bcloud000001", "keyword": "cloud kw", "created_at": "2026-07-21T00:00:00Z",
                 "term_targets": [], "topics_ai": True}
    monkeypatch.setattr(store, "_use_cloud", lambda: True)
    monkeypatch.setattr(store, "_collection",
                        _fake_collection_router({store._BENCH_COLLECTION: {"bcloud000001": cloud_doc}}))
    metas = store.list_benchmarks()
    keywords = {m["keyword"] for m in metas}
    assert {"disk kw", "cloud kw"} <= keywords


def test_get_geo_run_falls_back_to_cloud_when_absent_from_disk(monkeypatch):
    cloud_run = {"id": "cloud-g1", "brand": "legalsoft", "week": "2026-W31",
                "captured_at": "2026-07-19T00:00:00Z", "score": 8.0}
    monkeypatch.setattr(store, "_use_cloud", lambda: True)
    monkeypatch.setattr(store, "_collection",
                        _fake_collection_router({store._GEO_COLLECTION: {"cloud-g1": cloud_run}}))
    assert store.get_geo_run("cloud-g1") == cloud_run
    assert store.get_geo_run("truly-missing") is None


def test_list_geo_runs_merges_cloud_docs_absent_from_disk(monkeypatch):
    store.save_geo_run({"id": "gdisk1", "brand": "legalsoft", "week": "2026-W29",
                        "captured_at": "2026-07-13T00:00:00Z", "score": 6.0})
    cloud_run = {"id": "gcloud1", "brand": "legalsoft", "week": "2026-W30",
                "captured_at": "2026-07-20T00:00:00Z", "score": 7.0}
    monkeypatch.setattr(store, "_use_cloud", lambda: True)
    monkeypatch.setattr(store, "_collection",
                        _fake_collection_router({store._GEO_COLLECTION: {"gcloud1": cloud_run}}))
    ids = {r["id"] for r in store.list_geo_runs(brand="legalsoft")}
    assert {"gdisk1", "gcloud1"} <= ids


def test_get_benchmark_cloud_exception_returns_none_without_raising(monkeypatch):
    monkeypatch.setattr(store, "_use_cloud", lambda: True)

    def _boom(name):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(store, "_collection", _boom)
    assert store.get_benchmark("not-on-disk-or-cloud") is None  # degrades, doesn't raise


def test_get_geo_run_cloud_exception_returns_none_without_raising(monkeypatch):
    monkeypatch.setattr(store, "_use_cloud", lambda: True)

    def _boom(name):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(store, "_collection", _boom)
    assert store.get_geo_run("not-on-disk-or-cloud") is None  # degrades, doesn't raise


def test_list_benchmarks_cloud_exception_degrades_to_disk(monkeypatch):
    store.save_benchmark(_benchmark("bxdiskonly01", "disk only kw"))
    monkeypatch.setattr(store, "_use_cloud", lambda: True)

    def _boom(name):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(store, "_collection", _boom)
    metas = store.list_benchmarks()
    assert any(m["keyword"] == "disk only kw" for m in metas)  # cloud failure didn't wipe results


def test_list_geo_runs_cloud_exception_degrades_to_disk(monkeypatch):
    store.save_geo_run({"id": "gxdiskonly1", "brand": "legalsoft", "week": "2026-W25",
                        "captured_at": "2026-07-01T00:00:00Z", "score": 5.0})
    monkeypatch.setattr(store, "_use_cloud", lambda: True)

    def _boom(name):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(store, "_collection", _boom)
    runs = store.list_geo_runs()
    assert any(r["id"] == "gxdiskonly1" for r in runs)


# --------------------------------------------------------------------------- #
# I5: load_config TTL cache — the /score hot path shouldn't hit Firestore per
# keystroke-pause debounce.
# --------------------------------------------------------------------------- #

class _CountingConfigCollection:
    def __init__(self, data: dict):
        self.data = data
        self.reads = 0

    def document(self, doc_id):
        outer = self

        class _Ref:
            def get(_self):
                outer.reads += 1
                return _FakeSnap(outer.data)

            def set(_self, payload):
                outer.data = payload

        return _Ref()


def test_load_config_ttl_cache_avoids_repeated_cloud_reads(monkeypatch):
    fake = _CountingConfigCollection({"w_term_coverage": 0.6})
    monkeypatch.setattr(store, "_use_cloud", lambda: True)
    monkeypatch.setattr(store, "_collection", lambda name: fake)
    store._config_cache = None

    first = store.load_config()
    second = store.load_config()
    assert first == second == {"w_term_coverage": 0.6}
    assert fake.reads == 1  # second call served from cache within TTL

    store.save_config({"w_term_coverage": 0.7})  # invalidates the cache
    third = store.load_config()
    assert third == {"w_term_coverage": 0.7}
    assert fake.reads == 2
