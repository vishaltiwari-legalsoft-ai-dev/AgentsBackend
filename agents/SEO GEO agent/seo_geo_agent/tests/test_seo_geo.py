"""Engine tests: estimate math, to-do rules, status persistence, topic ranking."""
import threading
from datetime import date

import pytest

from seo_geo_agent import insights, keywords, site_brain, state, topics
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


def test_a_brand_added_while_another_is_being_added_is_not_lost():
    """The lost update the transactional registry exists to stop.

    ``brands`` is ONE document holding the whole list. The old write was
    ``list_brands()`` -> append -> ``state.save``: two people adding a brand at
    the same time both read the same list, both append their own, and the second
    save overwrites the first — one brand silently gone. Harmless at one brand a
    quarter; not harmless now that any GEO editor can add one and twelve go in
    during a single sitting.

    Driven by holding the FIRST writer inside its write while the second one
    runs, which is the real window: the fix is that the read and the write are
    one indivisible step, so the second writer cannot read a list the first has
    already added to. Run this against ``load`` + ``save`` and "first" is gone.
    """
    inside = threading.Event()
    proceed = threading.Event()
    real_save = state.save

    def blocking_save(doc_id, data):
        if doc_id == insights.BRANDS_DOC and not inside.is_set():
            inside.set()
            proceed.wait(5)          # still mid-write, with the doc unchanged
        return real_save(doc_id, data)

    monkeypatch_save(blocking_save)
    try:
        first = threading.Thread(target=insights.create_brand, args=(
            {"id": "first", "name": "First", "domain": "first.com", "enabled": True},))
        first.start()
        assert inside.wait(5), "the first add never reached its write"

        second = threading.Thread(target=insights.create_brand, args=(
            {"id": "second", "name": "Second", "domain": "second.com", "enabled": True},))
        second.start()
        second.join(0.5)             # a transactional write makes it wait its turn
        proceed.set()
        first.join(5)
        second.join(5)
    finally:
        monkeypatch_save(real_save)
        proceed.set()

    ids = {b["id"] for b in insights.list_brands()}
    assert {"first", "second"} <= ids, (
        "a concurrent add overwrote the other one — the registry write is not "
        f"transactional; registry holds {sorted(ids)}"
    )


def monkeypatch_save(fn):
    """Swap ``state.save`` for the duration of a threaded test.

    Not ``monkeypatch``: pytest's fixture is not thread-safe to install from,
    and this has to be visible to a thread that is already running.
    """
    state.save = fn


@pytest.mark.parametrize(
    ("call", "args"),
    [
        (lambda b: insights.create_brand(b), ({"id": "z", "name": "Z", "domain": "z.com"},)),
        (lambda b: insights.upsert_brand(b), ({"id": "z", "name": "Z", "domain": "z.com"},)),
        (lambda b: insights.set_brand_enabled("legalsoft", False), (None,)),
        (lambda b: insights.delete_brand("legalsoft"), (None,)),
    ],
)
def test_every_registry_write_goes_through_the_transaction(call, args, monkeypatch):
    """Structural, and the one that survives a rewrite of the code above.

    The threaded test proves TODAY's implementation holds; this refuses the
    shape that cannot hold at all. A bare ``state.save`` on the registry
    document is a lost update in Firestore no matter how carefully the code
    around it reads, so nothing may write that document except ``mutate``.
    """
    seen: list[str] = []
    depth: list[int] = []
    real_mutate, real_save = state.mutate, state.save

    def traced_mutate(doc_id, change):
        seen.append(doc_id)
        depth.append(1)
        try:
            return real_mutate(doc_id, change)
        finally:
            depth.pop()

    def guarded_save(doc_id, data):
        # ``mutate`` writes through ``save`` on the offline path, which is fine —
        # it is inside the lock/transaction. A save that reaches the registry
        # document from OUTSIDE one is the defect.
        if doc_id == insights.BRANDS_DOC and not depth:
            raise AssertionError(
                f"{doc_id!r} was written with a bare state.save — the brand "
                "registry is one shared document and every write to it must go "
                "through state.mutate"
            )
        return real_save(doc_id, data)

    monkeypatch.setattr(state, "mutate", traced_mutate)
    monkeypatch.setattr(state, "save", guarded_save)
    call(args[0])
    assert insights.BRANDS_DOC in seen


def test_creating_a_brand_that_already_exists_is_refused():
    """A slug collision must not silently adopt somebody else's brand: the same
    id owns the same ``geo-config-*`` and ``gsc-auth-*`` documents underneath."""
    insights.create_brand({"id": "acme", "name": "Acme", "domain": "acme.com",
                           "enabled": True})
    with pytest.raises(ValueError, match="already exists"):
        insights.create_brand({"id": "acme", "name": "Acme Legal",
                               "domain": "acmelegal.com", "enabled": True})
    acme = next(b for b in insights.list_brands() if b["id"] == "acme")
    assert acme["domain"] == "acme.com"   # the original, untouched


def test_switching_a_brand_off_keeps_every_document_it_owns():
    """The self-serve remove. It flips one flag; it destroys nothing.

    ``delete_brand`` is the contrast this pins against: it drops the registry
    entry and takes ``run-`` and ``todos-`` with it, leaving eighteen other
    ``…-{brand_id}`` families — ``gsc-auth-{id}`` and its live Search Console
    refresh token among them — with no route left that can reach them.
    """
    insights.create_brand({"id": "acme", "name": "Acme", "domain": "acme.com",
                           "enabled": True})
    state.save("gsc-auth-acme", {"refresh_token": "x", "property": "sc-domain:acme.com"})
    insights.run_brand({"id": "acme", "name": "Acme", "domain": "acme.com"},
                       trigger="test")

    off = insights.set_brand_enabled("acme", False)
    assert off["enabled"] is False
    assert [b["id"] for b in insights.list_brands() if b.get("enabled", True)] != []
    assert "acme" not in {b["id"] for b in insights.list_brands() if b.get("enabled", True)}
    # nothing was destroyed, and the flip goes back
    assert state.load("gsc-auth-acme")["refresh_token"] == "x"
    assert insights.latest_run("acme") is not None
    assert insights.set_brand_enabled("acme", True)["enabled"] is True
    assert "acme" in {b["id"] for b in insights.list_brands() if b.get("enabled", True)}


def test_switching_an_unknown_brand_is_a_lookup_failure_not_a_silent_write():
    with pytest.raises(KeyError):
        insights.set_brand_enabled("never-existed", False)


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("acme.com", "acme.com"),
        ("ACME.com", "acme.com"),
        ("https://www.acme.com/", "acme.com"),
        # the case the old inline copy got wrong: a pasted URL kept its path,
        # so the brand's domain became "acme.com/pricing" and every
        # ``sc-domain:`` lookup and alias derived from it was nonsense
        ("https://acme.com/pricing?utm=x#top", "acme.com"),
        ("http://acme.com:8080/a", "acme.com"),
        ("  www.Acme.COM  ", "acme.com"),
    ],
)
def test_a_pasted_url_reduces_to_the_host(typed, expected):
    assert insights.normalize_domain(typed) == expected


@pytest.mark.parametrize("typed", ["", "   ", "not-a-domain", "https://", "/", "a b.com"])
def test_something_that_is_not_a_site_is_refused(typed):
    with pytest.raises(ValueError):
        insights.normalize_domain(typed)


def test_a_brand_id_is_a_slug_and_punctuation_alone_has_none():
    assert insights.slugify_brand_id("Acme Legal, Inc.") == "acme-legal-inc"
    with pytest.raises(ValueError, match="name"):
        insights.slugify_brand_id("!!!")


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


# ------------------------------ site-review to-dos ------------------------------

SITE_CORPUS = {"brand_id": "legalsoft", "page_count": 2, "degraded": [], "pages": [
    {"url": "https://legalsoft.com/", "title": "Home", "type": "home", "word_count": 900,
     "summary": "Virtual assistants for law firms.", "schema_types": ["Organization", "WebSite"]},
    {"url": "https://legalsoft.com/pricing", "title": "Pricing", "type": "other", "word_count": 180,
     "summary": "Plans and rates.", "schema_types": []},
]}


def _review(monkeypatch, issues: list[dict]) -> dict:
    payload = {"positioning": "p", "scorecard": {}, "strengths": [], "issues": issues,
               "suggested_seeds": [], "covered_topics": [], "missing_topics": []}
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: payload)
    return site_brain.expert_review(insights.list_brands()[0], SITE_CORPUS)


def _site_issue(insight, url="https://legalsoft.com/pricing", category="trust", priority="high"):
    return {"insight": insight, "evidence": f"{url} — no reviews shown",
            "action": "Add client reviews", "priority": priority, "category": category}


def test_site_todo_ids_survive_a_reworded_rerun(monkeypatch):
    """The model words the same finding differently every run; the id is the
    category + the page, so the Fix-list row is the same row."""
    _review(monkeypatch, [_site_issue("No testimonials on the pricing page")])
    first = site_brain.site_todos("legalsoft")
    _review(monkeypatch, [_site_issue("Pricing lacks social proof from real clients")])
    second = site_brain.site_todos("legalsoft")
    assert [t["id"] for t in first] == [t["id"] for t in second]
    assert second[0]["page"] == "https://legalsoft.com/pricing"     # the URL, not the evidence sentence
    assert second[0]["category"] == "trust"
    _review(monkeypatch, [_site_issue("Pricing lacks social proof", category="conversion")])
    assert site_brain.site_todos("legalsoft")[0]["id"] != first[0]["id"]   # a different finding


def test_site_todo_status_carries_over_a_reworded_rerun(monkeypatch):
    brand = insights.list_brands()[0]
    _review(monkeypatch, [_site_issue("No testimonials on the pricing page")])
    run = insights.run_brand(brand, trigger="test")
    tid = next(t["id"] for t in run["todos"] if t["kind"] == "site")
    insights.set_todo_status(brand["id"], tid, "done")

    _review(monkeypatch, [_site_issue("Pricing lacks social proof from real clients")])
    insights.run_brand(brand, trigger="test")
    site = [t for t in insights.latest_run(brand["id"])["todos"] if t["kind"] == "site"]
    assert [(t["id"], t["status"]) for t in site] == [(tid, "done")]


def test_status_set_under_the_old_wording_id_is_carried(monkeypatch):
    """Statuses saved before ids were keyed by page live under the wording-based
    id. A review stored then (no url field) and the re-run after it both keep them."""
    legacy_issue = {"insight": "No testimonials on the pricing page",
                    "evidence": "https://legalsoft.com/pricing (180w) — none",
                    "action": "Add client reviews", "priority": "high", "category": "trust"}
    state.save("sitereview-legalsoft", {"brand_id": "legalsoft", "issues": [legacy_issue]})
    legacy_id = site_brain._legacy_todo_id("legalsoft", legacy_issue)
    insights.set_todo_status("legalsoft", legacy_id, "assigned")

    todo = site_brain.site_todos("legalsoft")[0]
    assert todo["page"] == "https://legalsoft.com/pricing"
    assert todo["id"] == site_brain._todo_id("legalsoft", "trust", "https://legalsoft.com/pricing")
    assert todo["status"] == "assigned"

    _review(monkeypatch, [_site_issue("Pricing lacks social proof")])     # reworded re-run
    assert site_brain.site_todos("legalsoft")[0]["status"] == "assigned"


def test_merge_todos_reserves_site_slots_and_counts_the_cut():
    search = [{"id": f"s{i}"} for i in range(25)]
    site = [{"id": f"w{i}"} for i in range(8)]
    shown, not_shown = site_brain.merge_todos(search, site, 25)
    assert len(shown) == 25 and not_shown == 8
    assert [t["id"] for t in shown[-site_brain.SITE_TODO_SLOTS:]] == [f"w{i}" for i in range(5)]
    # room either side leaves goes to the other
    shown, not_shown = site_brain.merge_todos(search[:3], site, 25)
    assert len(shown) == 11 and not_shown == 0
    shown, not_shown = site_brain.merge_todos(search, site[:2], 25)
    assert [t["id"] for t in shown][-2:] == ["w0", "w1"] and not_shown == 2


def test_site_todos_are_never_cut_silently_by_max_todos(monkeypatch):
    """Search-data to-dos fill all 25 slots on their own; site findings used to be
    appended after them and sliced off without a trace."""
    brand = insights.list_brands()[0]
    rows = [row(query=f"legal assistant topic {i}", page=f"https://legalsoft.com/p{i}",
                impressions=2000, position=8.0) for i in range(30)]
    monkeypatch.setattr(insights, "gsc_fetch", lambda prop, s, e, service=None: rows)
    _review(monkeypatch, [_site_issue("No testimonials on the pricing page"),
                          _site_issue("Home page never says who it serves",
                                      url="https://legalsoft.com/", category="intent")])
    run = insights.run_brand(brand, trigger="test")
    assert len(run["todos"]) == insights.MAX_TODOS
    assert sum(t["kind"] == "site" for t in run["todos"]) == 2
    assert run["todos_not_shown"] == 2   # two search to-dos made room, and it is counted


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
    question = "how much does a legal virtual assistant cost"
    rows = [row(query=question, impressions=900, position=12.0)]
    prev = [row(query=question, impressions=300, position=12.0)]

    def search(q):
        if "cost" in q:
            return WEAK_SERP
        # the seed itself: a strong top 10, but Google still suggests the questions
        return {**STRONG_SERP, "related": WEAK_SERP["related"], "paa": WEAK_SERP["paa"]}

    ranked, notes = topics.build_topics(brand, rows, prev, search=search)
    assert all("Serper" not in n for n in notes)  # only the offline-LLM ideation note is expected
    top = ranked[0]
    assert top["keyword"] == question
    assert top["source"] == "google suggestion"
    assert top["from_seed"] == "legal virtual assistant"
    assert top["trend"] == "rising"
    assert top["difficulty"] == "easy win"
    assert top["volume_est"] == 900
    assert top["est_monthly_clicks"] == 99  # 900 * 0.11
    assert "AI Overview" in top["why"]
    assert top["why"].startswith('Google suggests this for your seed "legal virtual assistant"')
    seed = next(t for t in ranked if t["source"] == "seed")
    assert seed["difficulty"] == "hard" and seed["score"] < top["score"]


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


# ------------------------- blog plan: intent + gaps + cannibalization -------------------------

def test_build_topics_still_works_with_three_args():
    """Backward compat: existing 3-positional-arg call sites keep working."""
    brand = {"id": "b", "domain": "x.com", "seeds": ["legal virtual assistant"]}
    rows = [row(query="legal virtual assistant cost", impressions=900, position=12.0)]
    ranked, notes = topics.build_topics(brand, rows, [])
    assert isinstance(ranked, list)
    assert isinstance(notes, list)


def test_every_topic_has_intent():
    brand = {"id": "b", "domain": "x.com", "seeds": ["legal virtual assistant"]}
    rows = [row(query="legal virtual assistant cost", impressions=900, position=12.0)]
    ranked, _ = topics.build_topics(brand, rows, [], search=None)
    assert ranked
    for t in ranked:
        assert t["intent"] == keywords.intent_of(t["keyword"])


def test_cannibalizing_candidate_marked_avoided_and_sorted_last():
    brand = {"id": "b", "domain": "x.com",
             "seeds": ["legal virtual assistant cost", "paralegal staffing help"]}
    rows = [
        row(query="legal virtual assistant cost", impressions=900, position=3.0),
        row(query="paralegal staffing help", impressions=500, position=3.0),
    ]
    corpus_pages = [
        {"url": "https://x.com/pricing", "title": "Legal Virtual Assistant Cost",
         "target_query": "legal virtual assistant cost"},
    ]
    ranked, _ = topics.build_topics(brand, rows, [], search=None, corpus_pages=corpus_pages)
    avoided = [t for t in ranked if t.get("avoided")]
    live = [t for t in ranked if not t.get("avoided")]
    assert avoided, "expected at least one avoided (cannibalizing) topic"
    assert live, "expected at least one live (non-cannibalizing) topic"
    hit = next(t for t in avoided if t["keyword"] == "legal virtual assistant cost")
    assert hit["avoided_reason"] == "overlaps https://x.com/pricing"
    # avoided topics sort after live (non-avoided) ones
    live_indexes = [i for i, t in enumerate(ranked) if not t.get("avoided")]
    avoided_indexes = [i for i, t in enumerate(ranked) if t.get("avoided")]
    assert max(live_indexes) < min(avoided_indexes)


def test_avoided_topics_never_hidden():
    brand = {"id": "b", "domain": "x.com", "seeds": ["legal virtual assistant cost"]}
    rows = [row(query="legal virtual assistant cost", impressions=900, position=3.0)]
    corpus_pages = [
        {"url": "https://x.com/pricing", "title": "Legal Virtual Assistant Cost",
         "target_query": "legal virtual assistant cost"},
    ]
    without, _ = topics.build_topics(brand, rows, [], search=None)
    with_corpus, _ = topics.build_topics(brand, rows, [], search=None, corpus_pages=corpus_pages)
    # same candidate pool, no candidate is dropped just because it overlaps
    assert len(with_corpus) == len(without)


def test_competitor_topic_tagged_and_never_outranks_seed():
    brand = {"id": "b", "domain": "x.com", "seeds": ["legal virtual assistant"]}
    rows = [row(query="legal virtual assistant", impressions=900, position=3.0)]
    competitor_topics = ["outsourced paralegal staffing"]
    ranked, _ = topics.build_topics(brand, rows, [], search=None, competitor_topics=competitor_topics)
    comp = [t for t in ranked if t["source"] == "competitor-content"]
    assert comp, "expected a competitor-content sourced topic"
    assert comp[0]["keyword"].lower() == "outsourced paralegal staffing"
    seed_scores = [t["score"] for t in ranked if t["source"] == "seed"]
    assert all(comp[0]["score"] <= s for s in seed_scores)


def test_clamped_competitor_topic_gets_priority_recomputed_from_final_score():
    """Regression: priority must be derived from the score AFTER the competitor
    clamp, not the pre-clamp score — a topic clamped to sort at the bottom must
    not still display the higher priority label its raw score would have earned."""
    brand = {"id": "b", "domain": "x.com", "seeds": ["legal virtual assistant"]}
    rows = [
        row(query="legal virtual assistant", impressions=100, position=3.0),
        row(query="outsourced paralegal staffing", impressions=1000, position=3.0),
    ]
    prev = [row(query="legal virtual assistant", impressions=1000, position=3.0)]
    competitor_topics = ["outsourced paralegal staffing"]

    ranked, _ = topics.build_topics(brand, rows, prev, search=None, competitor_topics=competitor_topics)

    seed = next(t for t in ranked if t["source"] == "seed")
    comp = next(t for t in ranked if t["source"] == "competitor-content")

    # The seed is low on its OWN evidence — no demotion for being a seed any more:
    # 0.4*0.5 (no SERP) + 0.35*0.2 (falling) + 0.25*(100/1000) = 0.295.
    assert seed["score"] == 0.295
    assert seed["priority"] == "low"
    # the competitor topic's raw score (~0.695, would-be "high") gets clamped
    # below the seed floor; its displayed priority must reflect that final score
    assert comp["score"] < seed["score"]
    assert comp["priority"] == "low"
    # and it still sorts after every seed topic
    assert ranked.index(seed) < ranked.index(comp)


def test_competitor_clamp_preserves_relative_order():
    """Regression: when multiple competitor-content topics get clamped below the
    seed floor, they must not all collapse to one identical clamped score —
    their relative order (by pre-clamp score) has to survive the clamp."""
    brand = {"id": "b", "domain": "x.com", "seeds": ["legal virtual assistant"]}
    rows = [
        row(query="legal virtual assistant", impressions=5, position=8.0),
        row(query="outsourced paralegal staffing", impressions=900, position=8.0),
        row(query="virtual receptionist backup service", impressions=300, position=8.0),
    ]
    competitor_topics = ["outsourced paralegal staffing", "virtual receptionist backup service"]

    ranked, _ = topics.build_topics(brand, rows, [], search=None, competitor_topics=competitor_topics)

    seed = next(t for t in ranked if t["source"] == "seed")
    comp_high = next(t for t in ranked if t["keyword"].lower() == "outsourced paralegal staffing")
    comp_low = next(t for t in ranked if t["keyword"].lower() == "virtual receptionist backup service")

    # both competitor topics outscore the seed pre-clamp and get pulled below its floor
    assert comp_high["score"] < seed["score"]
    assert comp_low["score"] < seed["score"]
    # but they must not collapse to the same clamped value — the higher-volume
    # topic keeps its edge over the lower-volume one
    assert comp_high["score"] > comp_low["score"]


def test_live_topics_cap_at_ten():
    brand = {"id": "b", "domain": "x.com", "seeds": []}
    rows = [
        row(query=f"how to fix issue {i}", impressions=60 + i, position=15.0)
        for i in range(15)
    ]
    ranked, _ = topics.build_topics(brand, rows, [], search=None)
    live = [t for t in ranked if not t.get("avoided")]
    assert len(live) <= 10
    assert len(ranked) <= 10  # nothing was avoided here, so the cap applies to the whole list


# ------------------- blog plan: the user's seeds are hard requirements -------------------

TEN_SEEDS = [
    "legal virtual assistant", "law firm answering service", "paralegal outsourcing",
    "legal intake specialist", "virtual receptionist for lawyers", "legal billing assistant",
    "court filing service", "legal transcription service", "law firm bookkeeping",
    "estate planning assistant",
]
NATO = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
        "india", "juliet", "kilo", "lima", "mike", "november", "oscar"]


def test_all_ten_user_seeds_become_candidates():
    """The router keeps up to 10 seeds; the lab used to read only the first 5."""
    calls = []

    def search(q):
        calls.append(q)
        return STRONG_SERP

    brand = {"id": "b", "domain": "x.com", "seeds": TEN_SEEDS}
    ranked, _ = topics.build_topics(brand, [], [], search=search, max_checks=0)
    assert sorted(t["keyword"] for t in ranked if t["source"] == "seed") == sorted(TEN_SEEDS)
    # Google *expansion* stays capped for cost — only the first five seeds pay for it.
    assert calls == TEN_SEEDS[: topics.MAX_SEEDS]


def test_every_user_seed_keeps_a_live_topic_past_the_cap():
    seeds = ["estate planning assistant", "court filing service", "legal transcription service"]
    rows = [row(query=f"how to choose vendor {w}", impressions=100 + 10 * i, position=15.0)
            for i, w in enumerate(NATO)]
    prev = [row(query=f"how to choose vendor {w}", impressions=10, position=15.0) for w in NATO]

    ranked, _ = topics.build_topics({"id": "b", "domain": "x.com", "seeds": seeds},
                                    rows, prev, search=None)

    live = [t for t in ranked if not t["avoided"]]
    assert len(live) == topics.MAX_LIVE_TOPICS  # the cap holds — it makes room, not more rows
    assert {t["keyword"] for t in live if t["source"] == "seed"} == set(seeds)
    # the 15 rising search-data questions outscore every seed; the cap dropped the
    # lowest-scored of them to make room, and kept the 7 strongest
    data = [t for t in live if t["source"] == "search data"]
    assert len(data) == 7
    assert min(t["score"] for t in data) > max(t["score"] for t in live if t["source"] == "seed")
    assert {t["keyword"] for t in data} == {f"how to choose vendor {w}" for w in NATO[-7:]}
    # still score-ordered for display
    assert [t["score"] for t in live] == sorted((t["score"] for t in live), reverse=True)


def test_a_seed_is_represented_by_its_best_child_when_that_child_is_shown():
    """'The seed itself, or its best child': a child already in the top 10 keeps
    the seed represented, so the cap does not evict anything to re-add the seed."""
    def search(q):
        if q == "legal virtual assistant":   # the seed: hard, but Google expands it
            return {**STRONG_SERP, "related": ["legal virtual assistant pricing guide"]}
        return WEAK_SERP

    rows = [row(query=f"how to choose vendor {w}", impressions=100 + 10 * i, position=15.0)
            for i, w in enumerate(NATO[:9])]
    ranked, _ = topics.build_topics({"id": "b", "domain": "x.com", "seeds": ["legal virtual assistant"]},
                                    rows, [], search=search, max_checks=20)
    live = [t for t in ranked if not t["avoided"]]
    assert len(live) == topics.MAX_LIVE_TOPICS
    child = next(t for t in live if t["keyword"] == "legal virtual assistant pricing guide")
    assert child["from_seed"] == "legal virtual assistant"
    assert child["source"] == "google suggestion"
    assert "legal virtual assistant" not in {t["keyword"] for t in ranked}  # 11th, and represented


def test_seeds_are_not_demoted_for_being_seeds(monkeypatch):
    monkeypatch.setattr(topics.sources, "llm_json",
                        lambda system, prompt: ["paralegal outsourcing checklist"])
    brand = {"id": "b", "domain": "x.com", "seeds": ["legal virtual assistant"]}
    ranked, _ = topics.build_topics(brand, [], [], search=None)
    seed = next(t for t in ranked if t["source"] == "seed")
    idea = next(t for t in ranked if t["source"] == "new idea")
    # same (absent) evidence -> same score; the old -0.08 put the seed at 0.37
    assert seed["score"] == idea["score"] == 0.45


def test_preselection_is_by_source_then_insertion_not_alphabet():
    """No Search Console: every volume is 0. The old (volume, key) reverse sort
    handed the scarce SERP checks to 'why…'/'who…' questions alphabetically."""
    related = ["best legal assistant software", "legal assistant salary guide",
               "why hire a legal assistant", "who needs a legal assistant"]

    def run():
        checks = []

        def search(q):
            if q == "legal virtual assistant":   # the seed's expansion call
                return {**STRONG_SERP, "related": related}
            checks.append(q)
            return STRONG_SERP

        topics.build_topics({"id": "b", "domain": "x.com", "seeds": ["legal virtual assistant"]},
                            [], [], search=search, max_checks=3,
                            site_review={"missing_topics": ["legal intake checklist"]})
        return checks

    first = run()
    assert first == ["legal intake checklist", "best legal assistant software",
                     "legal assistant salary guide"]
    assert run() == first   # deterministic


def test_volume_matching_no_longer_inflates_head_terms():
    rows = [
        row(query="legal assistant", impressions=10000),
        row(query="legal assistant salary guide 2026", impressions=70),
        row(query="paralegals near me", impressions=500),
        row(query="paralegals", impressions=40),
    ]
    # a short, huge head query no longer lends its impressions to longer topics
    assert topics._match_impressions("legal assistant salary guide", rows) == 70
    # a one-word topic counts its exact query only, not every query using the word
    assert topics._match_impressions("paralegals", rows) == 40
    # a multi-word topic still collects the longer queries that contain it
    assert topics._match_impressions("legal assistant", rows) == 10070

    brand = {"id": "b", "domain": "x.com", "seeds": ["legal assistant salary guide"]}
    ranked, _ = topics.build_topics(brand, rows, [], search=None)
    seed = next(t for t in ranked if t["source"] == "seed")
    assert seed["est_monthly_clicks"] == 8   # 70 * 0.11 — was 1,108 with the head query absorbed


@pytest.mark.parametrize("anchor", ["site gap", "suggested seed"])
def test_competitor_clamp_applies_without_typed_seeds(anchor):
    rows = [row(query="outsourced paralegal staffing", impressions=1000, position=8.0)]
    if anchor == "site gap":
        brand = {"id": "b", "domain": "x.com", "seeds": []}
        review = {"missing_topics": ["legal intake checklist"]}
    else:
        brand = {"id": "b", "domain": "x.com", "seeds": [],
                 "seed_entries": [{"text": "legal intake checklist", "source": "suggested seed"}]}
        review = {"covered_topics": ["virtual receptionists"]}
    ranked, _ = topics.build_topics(brand, rows, [], search=None,
                                    competitor_topics=["outsourced paralegal staffing"],
                                    site_review=review)
    anchor_topic = next(t for t in ranked if t["source"] == anchor)
    comp = next(t for t in ranked if t["source"] == "competitor-content")
    assert comp["score"] < anchor_topic["score"]   # raw ~0.695 vs 0.375
    assert ranked.index(anchor_topic) < ranked.index(comp)


def test_site_gap_candidates_carry_their_source():
    review = {"positioning": "Virtual assistants for US law firms.",
              "missing_topics": ["legal intake checklist", "law firm pricing comparisons"],
              "covered_topics": []}
    ranked, notes = topics.build_topics({"id": "b", "domain": "x.com", "seeds": []},
                                        [], [], search=None, site_review=review)
    gaps = [t for t in ranked if t["source"] == "site gap"]
    assert {t["keyword"] for t in gaps} == {"legal intake checklist", "law firm pricing comparisons"}
    assert all(t["from_seed"] is None for t in gaps)
    assert topics.NO_REVIEW_NOTE not in notes and topics.EMPTY_REVIEW_NOTE not in notes


def test_covered_topics_are_dropped_but_never_a_typed_seed(monkeypatch):
    monkeypatch.setattr(topics.sources, "llm_json",
                        lambda system, prompt: ["outsourcing paralegal", "legal intake checklist"])
    review = {"covered_topics": ["paralegal outsourcing"], "missing_topics": []}
    ranked, _ = topics.build_topics({"id": "b", "domain": "x.com", "seeds": ["paralegal outsourcing"]},
                                    [], [], search=None, site_review=review,
                                    competitor_topics=["outsourcing for paralegal"])
    keywords_out = {t["keyword"].lower() for t in ranked}
    assert "paralegal outsourcing" in keywords_out          # the user's own seed stays
    assert "outsourcing paralegal" not in keywords_out      # the site already covers it
    assert "outsourcing for paralegal" not in keywords_out  # ...whatever source offers it
    assert "legal intake checklist" in keywords_out
    assert next(t for t in ranked if t["keyword"] == "paralegal outsourcing")["source"] == "seed"
