from seo_agent import store


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
