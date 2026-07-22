"""Engine tests: estimate math, to-do rules, status persistence, topic ranking."""
from datetime import date

from seo_geo_agent import insights, topics
from seo_geo_agent.sources import QueryStat


def row(query="legal virtual assistant", page="https://x.com/a", clicks=10,
        impressions=1000, ctr=None, position=8.0) -> QueryStat:
    return QueryStat(query=query, page=page, clicks=clicks, impressions=impressions,
                     ctr=ctr if ctr is not None else clicks / impressions, position=position)


# ------------------------------- estimate math -------------------------------

def test_ctr_curve_monotonic_with_tail():
    values = [insights.ctr_at(p) for p in range(1, 11)]
    assert values == sorted(values, reverse=True)
    assert insights.ctr_at(35) == insights.TAIL_CTR


def test_striking_distance_todo_math():
    todos = insights.build_todos("b", [row(position=8.0, impressions=1000, ctr=0.032)], [])
    assert len(todos) == 1
    t = todos[0]
    assert t["kind"] == "striking"
    # target = 8 - 3 = 5: gain = 1000 * (ctr@5 - ctr@8) = 1000 * (0.069 - 0.032) = 37
    assert t["est_monthly_clicks"] == 37
    assert "#5" in t["why"]


def test_ctr_gap_todo_fires_only_on_real_gap():
    underperformer = row(position=3.0, impressions=1000, ctr=0.02)
    healthy = row(query="other", position=3.0, impressions=1000, ctr=0.10)
    todos = insights.build_todos("b", [underperformer, healthy], [])
    assert [t["kind"] for t in todos] == ["ctr_gap"]
    # gain = 1000 * (0.8*0.11 - 0.02) = 68
    assert todos[0]["est_monthly_clicks"] == 68


def test_decaying_page_todo():
    prev = [row(clicks=100, position=3.0, ctr=0.10)]
    now = [row(clicks=30, position=3.0, ctr=0.10)]
    todos = insights.build_todos("b", now, prev)
    decay = [t for t in todos if t["kind"] == "decay"]
    assert len(decay) == 1
    assert decay[0]["est_monthly_clicks"] == 49  # 0.7 * (100 - 30)


def test_small_queries_ignored():
    assert insights.build_todos("b", [row(impressions=99, position=8.0)], []) == []


def test_todo_ids_stable_across_runs():
    a = insights.build_todos("b", [row()], [])[0]["id"]
    b = insights.build_todos("b", [row()], [])[0]["id"]
    assert a == b


# ----------------------------- brands + persistence -----------------------------

def test_default_brand_present():
    brands = insights.list_brands()
    assert brands[0]["id"] == "legalsoft"


def test_brand_upsert_and_delete_roundtrip():
    insights.upsert_brand({"id": "acme", "name": "Acme", "domain": "acme.com",
                           "gsc_property": "sc-domain:acme.com", "seeds": [], "enabled": True})
    assert any(b["id"] == "acme" for b in insights.list_brands())
    insights.delete_brand("acme")
    assert not any(b["id"] == "acme" for b in insights.list_brands())


def test_run_brand_offline_degrades_and_persists(monkeypatch):
    brand = insights.list_brands()[0]
    run = insights.run_brand(brand, trigger="test", today=date(2026, 7, 22))
    assert run["degraded"]  # offline: Search Console + Serper both unavailable
    assert run["todos"] == []
    assert insights.latest_run(brand["id"])["at"] == "2026-07-22"


def test_todo_status_survives_rerun(monkeypatch):
    brand = insights.list_brands()[0]
    monkeypatch.setattr(insights, "gsc_fetch", lambda prop, s, e, service=None: [row()])
    first = insights.run_brand(brand, trigger="test")
    tid = first["todos"][0]["id"]
    insights.set_todo_status(brand["id"], tid, "done")
    insights.run_brand(brand, trigger="test")  # data refresh must not wipe the mark
    latest = insights.latest_run(brand["id"])
    assert next(t for t in latest["todos"] if t["id"] == tid)["status"] == "done"


# --------------------------- rank-tracking mode ---------------------------

def test_build_rank_todos_orders_drops_first():
    doc = {"snapshots": [
        {"at": "2026-07-15", "ranks": {
            "kw a": {"position": 3, "top": []},
            "kw b": {"position": 6, "top": []},
            "kw c": {"position": None, "top": ["comp.com"]},
        }},
        {"at": "2026-07-22", "ranks": {
            "kw a": {"position": 8, "top": []},
            "kw b": {"position": 6, "top": []},
            "kw c": {"position": None, "top": ["comp.com"]},
        }},
    ]}
    todos = insights.build_rank_todos("b", doc)
    assert todos[0]["kind"] == "rank_drop"  # kw a fell 3 -> 8, urgency first
    kinds = [t["kind"] for t in todos]
    assert "striking" in kinds and "unranked" in kinds
    assert todos[0]["est_monthly_clicks"] is None  # honest: no impression data
    unranked = next(t for t in todos if t["kind"] == "unranked")
    assert "comp.com" in unranked["why"]


def test_run_brand_rank_mode_without_gsc(monkeypatch):
    brand = insights.list_brands()[0]
    fake_doc = {"snapshots": [{"at": "2026-07-22", "ranks": {
        "legal virtual assistant": {"position": 5, "top": ["comp.com"]},
    }}], "suggested_competitors": ["comp.com"]}
    monkeypatch.setattr(insights.competitors, "rank_snapshot", lambda b: fake_doc)
    run = insights.run_brand(brand, trigger="test", today=date(2026, 7, 22))
    assert run["summary"]["mode"] == "rank-tracking"
    assert run["summary"]["tracked"] == 1 and run["summary"]["top10"] == 1
    assert run["todos"][0]["kind"] == "striking"
    assert run["insights"]


# --------------------------------- topic lab ---------------------------------

WEAK_SERP = {
    "organic": [{"link": f"https://reddit.com/r/x{i}", "title": "t", "position": i + 1} for i in range(10)],
    "related": ["how much does a legal virtual assistant cost"],
    "paa": ["what does a legal virtual assistant do"],
    "aio_present": True,
}
STRONG_SERP = {
    "organic": [{"link": f"https://bigfirm{i}.com", "title": "t", "position": i + 1} for i in range(10)],
    "related": [],
    "paa": [],
    "aio_present": False,
}


def test_topics_rank_easy_rising_above_hard_falling():
    brand = {"id": "b", "domain": "x.com", "seeds": ["legal virtual assistant"]}
    rows = [row(query="legal virtual assistant cost", impressions=900, position=12.0)]
    prev = [row(query="legal virtual assistant cost", impressions=300, position=12.0)]

    def search(q):
        return WEAK_SERP if "cost" in q or "legal" in q else STRONG_SERP

    ranked, notes = topics.build_topics(brand, rows, prev, search=search)
    assert all("Serper" not in n for n in notes)  # only the offline-LLM ideation note is expected
    top = ranked[0]
    assert "cost" in top["keyword"]
    assert top["trend"] == "rising"
    assert top["difficulty"] == "easy win"
    assert top["volume_est"] == 900
    assert top["est_monthly_clicks"] == 99  # 900 * 0.11
    assert "AI Overview" in top["why"]


def test_topics_without_serper_still_built_from_gsc():
    brand = {"id": "b", "domain": "x.com", "seeds": ["legal virtual assistant"]}
    rows = [row(query="how to hire a legal virtual assistant", impressions=400, position=15.0)]
    ranked, notes = topics.build_topics(brand, rows, [], search=None)
    assert any("Serper" in n for n in notes)
    assert any("how to hire" in t["keyword"] for t in ranked)
    assert all(t["difficulty"] is None for t in ranked)


def test_question_angle_detection():
    assert topics._angle("how to hire a paralegal") == "FAQ answer"
    assert topics._angle("virtual assistant cost per hour") == "pricing guide"
    assert topics._angle("clio vs mycase") == "comparison"
