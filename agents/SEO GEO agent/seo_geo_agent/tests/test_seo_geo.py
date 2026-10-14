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
    id owns the same ``geo-config-*`` documents and ``seo_gsc_grants/{id}``
    Search Console grant underneath."""
    insights.create_brand({"id": "acme", "name": "Acme", "domain": "acme.com",
                           "enabled": True})
    with pytest.raises(ValueError, match="already exists"):
        insights.create_brand({"id": "acme", "name": "Acme Legal",
                               "domain": "acmelegal.com", "enabled": True})
    acme = next(b for b in insights.list_brands() if b["id"] == "acme")
    assert acme["domain"] == "acme.com"   # the original, untouched


def test_switching_a_brand_off_keeps_every_document_it_owns(gsc_connect):
    """The self-serve remove. It flips one flag; it destroys nothing — the
    brand's Search Console grant included, so switching it back on resumes.

    ``delete_brand`` is the contrast this pins against: it drops the grant, the
    registry entry, ``run-`` and ``todos-``, and leaves the other
    ``…-{brand_id}`` families behind.
    """
    insights.create_brand({"id": "acme", "name": "Acme", "domain": "acme.com",
                           "enabled": True})
    insights.run_brand({"id": "acme", "name": "Acme", "domain": "acme.com"},
                       trigger="test")
    # stored after the run so the run never tries to open (and use) the token
    gsc_connect.store["acme"] = {"property": "sc-domain:acme.com", "refresh_token_enc": "sealed"}

    off = insights.set_brand_enabled("acme", False)
    assert off["enabled"] is False
    assert [b["id"] for b in insights.list_brands() if b.get("enabled", True)] != []
    assert "acme" not in {b["id"] for b in insights.list_brands() if b.get("enabled", True)}
    # nothing was destroyed, and the flip goes back
    assert gsc_connect.store["acme"]["refresh_token_enc"] == "sealed"
    assert insights.latest_run("acme") is not None
    assert insights.set_brand_enabled("acme", True)["enabled"] is True
    assert "acme" in {b["id"] for b in insights.list_brands() if b.get("enabled", True)}


def test_hard_deleting_a_brand_deletes_its_search_console_grant(gsc_connect):
    """A deleted brand must not leave a sealed live refresh token behind with no
    brand — and so no route — left that could disconnect it."""
    insights.create_brand({"id": "acme", "name": "Acme", "domain": "acme.com", "enabled": True})
    insights.create_brand({"id": "other", "name": "Other", "domain": "other.com", "enabled": True})
    gsc_connect.store["acme"] = {"property": "sc-domain:acme.com", "refresh_token_enc": "a"}
    gsc_connect.store["other"] = {"property": "sc-domain:other.com", "refresh_token_enc": "o"}

    insights.delete_brand("acme")

    assert "acme" not in gsc_connect.store
    assert "acme" not in {b["id"] for b in insights.list_brands()}
    assert gsc_connect.store["other"]["refresh_token_enc"] == "o"   # only that brand's grant


def test_a_grant_delete_that_fails_leaves_the_brand_to_retry(gsc_connect, monkeypatch):
    """The grant goes first. If that write fails the brand is still listed, so
    the Creator's delete can be retried — never a gone brand with a live token."""
    from app.services import firestore_repo

    insights.create_brand({"id": "acme", "name": "Acme", "domain": "acme.com", "enabled": True})
    gsc_connect.store["acme"] = {"property": "sc-domain:acme.com", "refresh_token_enc": "a"}

    def stalled(brand_id):
        raise TimeoutError("Firestore deadline exceeded")

    monkeypatch.setattr(firestore_repo, "delete_gsc_grant", stalled)
    with pytest.raises(TimeoutError):
        insights.delete_brand("acme")
    assert "acme" in {b["id"] for b in insights.list_brands()}
    assert "acme" in gsc_connect.store


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
    # 30 search + 2 site built, 25 shown: the 5 search to-dos build_todos used to
    # trim on its own are counted along with the 2 that made room for site ones
    assert run["todos_not_shown"] == 7


def test_a_site_review_on_its_own_puts_its_findings_on_the_stored_fix_list(monkeypatch):
    """"Analyze website" toasted "N finding(s) added to the fix list" while the
    review saved only itself — the findings joined at the next run. They now
    fold into the stored run the way page advice does: stable ids, statuses
    kept, the one merge_todos cut, the cut recounted, and blog-plan pointers
    at Fix-list positions re-pointed — never left at a different item."""
    assert insights.fold_site_review("legalsoft") == (False, 0)   # no run yet: no Fix list to add to

    brand = insights.list_brands()[0]
    rows = [row(query=f"legal assistant q{i:03d}", page=f"https://legalsoft.com/p{i}",
                impressions=2000, position=8.0) for i in range(24)]
    monkeypatch.setattr(insights, "gsc_fetch", lambda prop, s, e, service=None: rows)
    _review(monkeypatch, [_site_issue("No testimonials on the pricing page")])
    run = insights.run_brand(brand, trigger="test")
    assert (len(run["todos"]), run["todos_not_shown"], run["search_todos_not_shown"]) == (25, 0, 0)
    kept_id = next(t["id"] for t in run["todos"] if t["kind"] == "site")
    insights.set_todo_status("legalsoft", kept_id, "done")
    stored = state.load("run-legalsoft")
    stored["topics"] = [
        {"keyword": "legal assistant q001", "also_in_fix": 2,
         "why": f"Your seed · {topics.FIX_LIST_WHY.format(n=2)}"},
        {"keyword": "legal assistant q022", "also_in_fix": 23,
         "why": f"Your seed · {topics.FIX_LIST_WHY.format(n=23)}"},
    ]
    state.save("run-legalsoft", stored)

    _review(monkeypatch, [
        _site_issue("Pricing lacks social proof from real clients"),            # same page + category
        _site_issue("Home page never says who it serves", url="https://legalsoft.com/", category="intent"),
        _site_issue("Pricing copy is thin and generic", category="content"),
        _site_issue("Pricing page buries the main offer", category="conversion"),
        _site_issue("Home page menu hides the service pages", url="https://legalsoft.com/",
                    category="architecture"),
        _site_issue("Home page never mentions the cities served", url="https://legalsoft.com/",
                    category="local"),
    ])
    assert insights.fold_site_review("legalsoft") == (True, site_brain.SITE_TODO_SLOTS)

    after = insights.latest_run("legalsoft")
    site = [t for t in after["todos"] if t["kind"] == "site"]
    assert len(after["todos"]) == insights.MAX_TODOS and len(site) == site_brain.SITE_TODO_SLOTS
    assert [t["kind"] for t in after["todos"][:20]] == ["striking"] * 20      # search to-dos keep their order
    assert next(t for t in site if t["id"] == kept_id)["status"] == "done"     # same finding, same row
    assert all(t.get("label") for t in after["todos"])
    # 24 search + 6 site built, 25 shown: 4 search to-dos gave up their places, 1 finding didn't fit
    assert (after["todos_not_shown"], after["search_todos_not_shown"]) == (5, 4)
    first, cut = after["topics"]
    assert (first["also_in_fix"], first["why"]) == (2, f"Your seed · {topics.FIX_LIST_WHY.format(n=2)}")
    assert (cut["also_in_fix"], cut["why"]) == (None, "Your seed")             # its item no longer made the cut


def test_build_todos_cap_is_the_default_and_none_returns_every_one():
    rows = [row(query=f"q{i}", page=f"https://x.com/p{i}", impressions=2000, position=8.0)
            for i in range(30)]
    assert len(insights.build_todos("b", rows, [])) == insights.MAX_TODOS
    assert len(insights.build_todos("b", rows, [], cap=None)) == 30
    doc = {"snapshots": [{"at": "2026-07-22", "ranks": {
        f"kw {i}": {"position": None, "top": ["comp.com"]} for i in range(30)}}]}
    assert len(insights.build_rank_todos("b", doc)) == insights.MAX_TODOS
    assert len(insights.build_rank_todos("b", doc, cap=None)) == 30


def test_rank_mode_counts_the_to_dos_it_cannot_show(monkeypatch):
    """Without Search Console the fix list comes from rank snapshots; more
    tracked gaps than MAX_TODOS used to vanish at the 25th without a number."""
    brand = insights.list_brands()[0]
    fake_doc = {"snapshots": [{"at": "2026-07-22", "ranks": {
        f"legal keyword {i}": {"position": None, "top": ["comp.com"]} for i in range(30)}}],
        "suggested_competitors": ["comp.com"]}
    monkeypatch.setattr(insights.competitors, "rank_snapshot", lambda b: fake_doc)
    run = insights.run_brand(brand, trigger="test", today=date(2026, 7, 22))
    assert run["summary"]["mode"] == "rank-tracking"
    assert len(run["todos"]) == insights.MAX_TODOS
    assert run["todos_not_shown"] == 5


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


def test_a_keyword_that_falls_off_page_one_is_a_drop_and_is_counted():
    """B3: a keyword with a top-10 place last check and none now counted
    nowhere — not moved_down (no position to compare), and its to-do read as a
    content gap that was always there. It is the biggest drop there is."""
    doc = {"snapshots": [
        {"at": "2026-10-13", "ranks": {
            "kw gone": {"position": 4, "top": []},
            "kw slid": {"position": 3, "top": []},
            "kw near": {"position": 6, "top": []},
            "kw never": {"position": None, "top": ["comp.com"]},
        }},
        {"at": "2026-10-14", "ranks": {
            "kw gone": {"position": None, "top": ["a.com", "b.com", "c.com", "d.com"]},
            "kw slid": {"position": 7, "top": []},
            "kw near": {"position": 6, "top": []},
            "kw never": {"position": None, "top": ["comp.com"]},
        }},
    ]}
    summary = insights._rank_summary(doc)
    assert (summary["fell_off"], summary["moved_down"], summary["unranked"]) == (1, 1, 2)
    assert (summary["ranks_from"], summary["rank_check_failed"]) == ("2026-10-14", False)

    todos = insights.build_rank_todos("b", doc)
    assert [(t["kind"], t["query"]) for t in todos] == [
        ("rank_drop", "kw slid"),        # drops first, by position
        ("rank_drop", "kw gone"),        # the fall off page one, same rule, no position
        ("striking", "kw near"),         # #6
        ("striking", "kw slid"),         # #7
        ("unranked", "kw never"),        # a gap that was always there stays a gap
    ]
    gone = todos[1]
    assert gone["why"].startswith("Dropped out of Google's top 10 for 'kw gone' (was #4)")
    assert "a.com, b.com, c.com" in gone["why"] and "d.com" not in gone["why"]
    assert gone["id"] == insights.todo_id("b", "rank_drop", "", "kw gone")   # stable across runs
    assert gone["est_monthly_clicks"] is None
    assert any("fell out of the top 10" in b for b in insights._rank_bullets(summary, todos))


def _serper_answering(monkeypatch, outcome):
    """Every Serper call goes through the REAL ``serper_search`` against a mock
    transport answering ``outcome``: an HTTP status, or an httpx exception
    class to raise. Only that call sees a cloud flag and a key — the run around
    it stays on local files, offline. Returns the list of queries searched."""
    import httpx
    from types import SimpleNamespace

    from seo_geo_agent import sources

    searched: list[str] = []

    def handler(request):
        if isinstance(outcome, int):
            if outcome == 200:
                return httpx.Response(200, json={"organic": [
                    {"link": f"https://comp{i}.com/x", "title": "t", "position": i + 1} for i in range(10)]})
            return httpx.Response(outcome, json={"message": "Not enough credits"})
        raise outcome("serper is down", request=request)

    real = sources.serper_search

    def search(query, client=None):
        searched.append(query)
        with pytest.MonkeyPatch.context() as m:
            m.setattr(sources, "state", SimpleNamespace(use_cloud=lambda: True))
            m.setenv("SEO_SERPER_API_KEY", "test-key")
            return real(query, client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(sources, "serper_search", search)
    monkeypatch.setattr(sources, "serper_available", lambda: True)
    return searched


def _serper_outcomes():
    import httpx

    return [402, 429, 500, 503, httpx.ConnectError, httpx.ReadTimeout]


@pytest.mark.parametrize("outcome", _serper_outcomes())
def test_a_serper_refusal_or_outage_is_a_search_failure_never_a_raw_http_error(monkeypatch, outcome):
    """``serper_search`` called ``raise_for_status`` and every caller caught
    only ``CredentialMissing``: one dry spell (402), a 429 or a 5xx raised an
    ``HTTPStatusError`` straight through the run. It is now a
    ``SearchUnavailable`` — a ``CredentialMissing`` — whose text names no URL
    and no key, and whose note reads as a plain sentence."""
    from seo_geo_agent import sources

    _serper_answering(monkeypatch, outcome)
    with pytest.raises(sources.SearchUnavailable) as caught:
        sources.serper_search("legal virtual assistant")
    exc = caught.value
    assert isinstance(exc, sources.CredentialMissing)
    assert str(exc).startswith("Serper search failed")
    for leak in ("serper.dev", "https://", "test-key", "Not enough credits", "serper is down"):
        assert leak not in str(exc)                     # no URL, no key, no provider body
    note = sources.plain_note(f"Serper: {exc}", log=False)
    assert note != f"Serper: {exc}" and note.startswith("Serper: the ")   # one of the plain sentences
    assert exc.why.startswith("the search service")


def _ranks_two_checks():
    state.save("ranks-legalsoft", {"suggested_competitors": ["comp.com"], "snapshots": [
        {"at": "2026-10-12", "ranks": {"legal virtual assistant": {"position": 3, "top": ["comp.com"]},
                                       "law firm answering service": {"position": 5, "top": ["comp.com"]}}},
        {"at": "2026-10-13", "ranks": {"legal virtual assistant": {"position": 8, "top": ["comp.com"]},
                                       "law firm answering service": {"position": None, "top": ["comp.com"]}}},
    ]})


@pytest.mark.parametrize("outcome", _serper_outcomes())
def test_a_failed_rank_check_keeps_the_last_good_ranks_and_says_so(monkeypatch, outcome):
    """A Serper dry spell used to make ``run_brand`` raise for every brand:
    nothing saved, the dashboard frozen on the last good run. The run is now
    saved; its ranks are the last good check's, marked as carried over, and a
    "Rank tracking" note says they weren't updated. No snapshot is taken."""
    from seo_geo_agent import sources

    _ranks_two_checks()
    _serper_answering(monkeypatch, outcome)
    brand = insights.list_brands()[0]
    run = insights.run_brand(brand, trigger="cron", today=date(2026, 10, 14))

    summary = run["summary"]
    assert summary["mode"] == "rank-tracking"
    assert (summary["tracked"], summary["top10"], summary["top3"]) == (2, 1, 0)   # the 10-13 check, not zeros
    assert (summary["moved_down"], summary["fell_off"]) == (1, 1)
    assert summary["rank_check_failed"] is True and summary["ranks_from"] == "2026-10-13"
    rank_notes = [n for n in run["degraded"] if n.startswith("Rank tracking")]
    assert len(rank_notes) == 1 and "rankings weren't updated" in rank_notes[0]
    assert sources.plain_note(rank_notes[0], log=False) == rank_notes[0]   # survives every read as written
    assert "2026-10-13" in run["insights"][0]                               # the bullets say whose figures
    assert {t["kind"] for t in run["todos"]} == {"rank_drop", "striking"}    # the last good check's fix list
    assert len(state.load("ranks-legalsoft")["snapshots"]) == 2             # no snapshot from a failed check
    stored = insights.latest_run("legalsoft")
    assert stored["at"] == "2026-10-14" and stored["degraded"] == run["degraded"]


def test_a_failed_rank_check_with_nothing_to_carry_is_still_a_saved_run(monkeypatch):
    _serper_answering(monkeypatch, 402)
    brand = insights.list_brands()[0]
    run = insights.run_brand(brand, trigger="manual:t", today=date(2026, 10, 14))
    assert run["summary"]["rank_check_failed"] is True and run["summary"]["ranks_from"] is None
    assert run["summary"]["mode"] == "search-console" and run["gsc_rows"] == 0   # nothing measured, honestly empty
    assert run["todos"] == []
    assert any(n.startswith("Rank tracking: Google results couldn't be reached") for n in run["degraded"])
    assert insights.latest_run("legalsoft")["at"] == "2026-10-14"


def test_a_brand_with_no_seeds_survives_a_serper_failure_in_its_blog_plan(monkeypatch):
    """A brand with no keywords to track makes no rank call — but its blog plan
    checks the site review's gaps on Google, and that call raised too."""
    insights.upsert_brand({"id": "noseeds", "name": "No Seeds", "domain": "noseeds.com", "seeds": []})
    state.save("sitereview-noseeds", {"brand_id": "noseeds", "issues": [],
                                      "missing_topics": ["virtual receptionist pricing for law firms"]})
    searched = _serper_answering(monkeypatch, 429)
    brand = next(b for b in insights.list_brands() if b["id"] == "noseeds")
    run = insights.run_brand(brand, trigger="cron", today=date(2026, 10, 14))
    assert searched == ["virtual receptionist pricing for law firms"]   # the plan's check, and only one
    assert "Serper: the service's usage limit or credits ran out." in run["degraded"]
    assert [t["keyword"] for t in run["topics"]] == ["virtual receptionist pricing for law firms"]
    assert insights.latest_run("noseeds") is not None


def test_every_todo_in_a_run_carries_a_short_label(monkeypatch):
    """B1: the dashboard's "This week" rows point at the Fix list in at most
    eight words instead of copying an item's action. A run stored before
    labels existed gets them when read."""
    _review(monkeypatch, [_site_issue("No testimonials on the pricing page")])
    monkeypatch.setattr(insights.competitors, "rank_snapshot", lambda b: {"snapshots": [{"at": "2026-10-14",
        "ranks": {"legal virtual assistant": {"position": 6, "top": ["comp.com"]},
                  "law firm answering service": {"position": None, "top": ["comp.com"]}}}]})
    run = insights.run_brand(insights.list_brands()[0], trigger="t", today=date(2026, 10, 14))
    assert [t["label"] for t in run["todos"]] == [
        "Refresh: “legal virtual assistant”", "New page: “law firm answering service”", "Trust fix: /pricing"]
    assert all(len(t["label"].split()) <= insights.LABEL_WORDS for t in run["todos"])
    assert [t["label"] for t in insights.latest_run("legalsoft")["todos"]] == [t["label"] for t in run["todos"]]

    legacy = state.load("run-legalsoft")
    for todo in legacy["todos"]:
        del todo["label"]
    state.save("run-legalsoft", legacy)
    assert [t["label"] for t in insights.latest_run("legalsoft")["todos"]] == [t["label"] for t in run["todos"]]


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


@pytest.mark.parametrize(("keyword", "angle"), [
    # buyer intent (commercial / transactional): a page that sells or compares
    ("law firm intake services", "service page"),            # was "how-to guide" on staging
    ("outsourced legal billing", "service page"),
    ("virtual paralegal cost", "pricing explainer"),
    ("legal virtual assistant reviews and ratings", "comparison"),
    ("clio vs mycase", "comparison"),
    # informational: a guide, or a FAQ answer for a question that is not "how to"
    ("remote legal secretary", "how-to guide"),
    ("how to onboard a remote paralegal", "how-to guide"),
    ("what does a legal virtual assistant do", "FAQ answer"),
])
def test_the_angle_comes_from_the_search_intent(keyword, angle):
    assert topics._angle(keyword, keywords.intent_of(keyword)) == angle


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
    """Only a candidate the user did NOT type is avoided — a suggested seed here."""
    brand = {"id": "b", "domain": "x.com", "seeds": ["paralegal staffing help"],
             "seed_entries": [{"text": "paralegal staffing help", "source": "seed"},
                              {"text": "legal virtual assistant cost", "source": "suggested seed"}]}
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


# --------------- D4/D5: the blog plan defers to the Fix list, seeds stay live ---------------

#: Staging's Fix list #1-#4 (rank mode): the keywords those page moves target.
STAGING_FIX = [
    {"id": "d550b5585f0f", "kind": "striking", "query": "law firm intake services", "position": 10},
    {"id": "912fbf2195ee", "kind": "unranked", "query": "virtual paralegal services", "position": 0},
    {"id": "fbe67c81dbe3", "kind": "unranked", "query": "remote legal secretary", "position": 0},
    {"id": "41c6f7740285", "kind": "unranked", "query": "personal injury case management", "position": 0},
    {"id": "6ffdb5858bd2", "kind": "site", "query": "", "position": 0},
]


def test_fix_list_keywords_are_not_offered_again_as_blog_topics():
    """Staging's blog plan re-offered four Fix-list keywords as posts. A
    non-seed topic that IS one is dropped; a typed seed stays (hard
    requirement) and says the page move comes first."""
    brand = {"id": "b", "domain": "legalsoft.com", "seeds": [],
             "seed_entries": [{"text": "law firm intake services", "source": "seed"},
                              {"text": "personal injury case management", "source": "seed"},
                              {"text": "virtual paralegal services", "source": "suggested seed"}]}
    review = {"missing_topics": ["remote legal secretary", "virtual paralegal qualifications"],
              "covered_topics": []}
    ranked, _ = topics.build_topics(brand, [], [], search=None, site_review=review,
                                    fix_queries=topics.fix_query_index(STAGING_FIX))
    by_kw = {t["keyword"]: t for t in ranked}
    assert "virtual paralegal services" not in by_kw and "remote legal secretary" not in by_kw
    assert "virtual paralegal qualifications" in by_kw and by_kw["virtual paralegal qualifications"]["also_in_fix"] is None
    intake = by_kw["law firm intake services"]
    assert (intake["source"], intake["avoided"], intake["also_in_fix"]) == ("seed", False, 1)
    assert intake["why"] == ("You listed this as a seed keyword · Also on your Fix list as #1 — that page "
                             "update comes first.")
    assert by_kw["personal injury case management"]["also_in_fix"] == 4


def test_a_typed_seed_never_goes_to_avoided():
    """'legal virtual assistant' — we rank #1 with /pricing — was in Avoided:
    the user's stated input dropped from the plan. It stays live, with a note."""
    serp = {"organic": [{"link": "https://www.legalsoft.com/pricing", "title": "Pricing", "position": 1}]
            + [{"link": f"https://big{i}.com/x", "title": "t", "position": i + 2} for i in range(9)],
            "related": [], "paa": [], "aio_present": False}
    corpus = [{"url": "https://www.legalsoft.com/pricing", "title": "Legal Virtual Assistant Pricing",
               "target_query": "legal virtual assistant pricing"},
              {"url": "https://www.legalsoft.com/virtual-paralegal", "title": "Virtual Paralegal Services",
               "target_query": "virtual paralegal services"}]
    brand = {"id": "b", "domain": "legalsoft.com",
             "seeds": ["legal virtual assistant", "virtual paralegal services"]}
    ranked, _ = topics.build_topics(brand, [], [], search=lambda q: serp if "assistant" in q else STRONG_SERP,
                                    corpus_pages=corpus)
    by_kw = {t["keyword"]: t for t in ranked}
    lva, vps = by_kw["legal virtual assistant"], by_kw["virtual paralegal services"]
    assert lva["avoided"] is False and vps["avoided"] is False and lva["avoided_reason"] is None
    assert lva["why"] == ("You listed this as a seed keyword · You already rank #1 with /pricing — refresh "
                          "that page rather than writing a new post.")
    assert lva["refresh_url"] == "https://www.legalsoft.com/pricing"
    assert vps["why"].endswith("Your page /virtual-paralegal already targets this — refresh it rather than "
                               "writing a new post.")


def _serp_with_us(link: str, at: int) -> dict:
    organic = [{"link": f"https://big{i}.com/x", "title": "t", "position": i + 1} for i in range(10)]
    organic[at - 1] = {"link": link, "title": "Us", "position": at}
    return {"organic": organic, "related": [], "paa": [], "aio_present": False}


def test_a_seed_we_rank_top_three_for_says_refresh_once_with_its_page_and_rank():
    """Staging: 'outsourced legal billing' said "hard: we already rank #3 with
    /blog/outsourcing-legal-billing-guide-for-law-firms — refresh that page"
    with ``refresh_url`` null, beside 'legal virtual assistant' saying it in
    another voice with its page attached — and both wore a red "hard" chip."""
    billing = "https://www.legalsoft.com/blog/outsourcing-legal-billing-guide-for-law-firms"
    serps = {"outsourced legal billing": _serp_with_us(billing, 3),
             "legal virtual assistant": _serp_with_us("https://www.legalsoft.com/virtual-staffing", 1),
             "law firm intake services": _serp_with_us("https://www.legalsoft.com/intake", 2),
             "remote legal secretary": _serp_with_us("https://www.legalsoft.com/remote-legal-secretary", 7)}
    corpus = [{"url": "https://www.legalsoft.com/virtual-staffing", "title": "Legal Virtual Assistant Staffing",
               "target_query": "legal virtual assistant"}]
    brand = {"id": "b", "domain": "legalsoft.com", "seeds": list(serps)}
    ranked, _ = topics.build_topics(brand, [], [], search=lambda q: serps.get(q, STRONG_SERP),
                                    corpus_pages=corpus,
                                    fix_queries={frozenset(topics._tokens("law firm intake services")): 1})
    by_kw = {t["keyword"]: t for t in ranked}
    obl, lva = by_kw["outsourced legal billing"], by_kw["legal virtual assistant"]
    assert obl["why"] == ("You listed this as a seed keyword · You already rank #3 with "
                          "/blog/outsourcing-legal-billing-guide-for-law-firms — refresh that page rather than "
                          "writing a new post.")
    assert obl["refresh_url"] == billing
    assert lva["why"] == ("You listed this as a seed keyword · You already rank #1 with /virtual-staffing — "
                          "refresh that page rather than writing a new post.")
    assert lva["refresh_url"] == "https://www.legalsoft.com/virtual-staffing"
    # the chip says where we rank; "difficulty" keeps its values (it scores a NEW post)
    assert (obl["difficulty"], obl["difficulty_label"], obl["own_rank"]) == ("hard", "we rank #3", 3)
    assert (lva["difficulty"], lva["difficulty_label"], lva["own_rank"]) == ("hard", "we rank #1", 1)
    # on the Fix list: the page move lives there, the topic only points at it
    intake = by_kw["law firm intake services"]
    assert intake["why"] == ("You listed this as a seed keyword · Also on your Fix list as #1 — that page "
                             "update comes first.")
    assert (intake["refresh_url"], intake["difficulty_label"]) == (None, "we rank #2")
    # #7: a new post can still help — no refresh, no rank chip
    rls = by_kw["remote legal secretary"]
    assert (rls["refresh_url"], rls["difficulty_label"], rls["own_rank"]) == (None, None, 7)
    assert "refresh" not in rls["why"]
    for t in ranked:                                    # the why never says refresh without the page
        if "refresh" in t["why"]:
            assert t["refresh_url"], t["keyword"]


def test_difficulty_counts_our_own_page_in_the_top_ten():
    """Blog #9 said 'top 10 is all strong sites' while we ranked #10 ourselves."""
    serp = {"organic": [{"link": f"https://big{i}.com/x", "title": "t", "position": i + 1} for i in range(9)]
            + [{"link": "https://www.legalsoft.com/intake-specialist-remote", "title": "Us", "position": 10}],
            "related": [], "paa": [], "aio_present": False}
    assert topics._difficulty(serp, "legalsoft.com") == (
        "hard", ["we rank #10 with /intake-specialist-remote", "the rest of the top 10 is strong sites"])
    assert topics._difficulty(STRONG_SERP, "legalsoft.com") == ("hard", ["top 10 is all strong sites"])
    # top 3: "hard" for a new post, and no evidence — the topic's why says "refresh" itself
    assert topics._difficulty(_serp_with_us("https://www.legalsoft.com/x", 3), "legalsoft.com") == ("hard", [])


def test_company_names_are_not_blog_topics():
    """'On demand Paralegal Services LLC' came from Google suggestions."""
    def search(q):
        return {**STRONG_SERP, "related": ["On demand Paralegal Services LLC", "Smith & Associates paralegal",
                                           "zirtual paralegal reviews", "virtual paralegal services cost",
                                           "paralegal certification requirements"]}

    brand = {"id": "b", "domain": "legalsoft.com", "seeds": ["virtual paralegal services"]}
    ranked, _ = topics.build_topics(
        brand, [], [], search=search, max_checks=0,
        competitor_domains=["zirtual.com", "virtualparalegalservices.com", "paralegal.com"])
    kws = {t["keyword"] for t in ranked}
    assert "On demand Paralegal Services LLC" not in kws and "Smith & Associates paralegal" not in kws
    assert "zirtual paralegal reviews" not in kws                    # a ranking competitor's brand
    # an exact-match domain or a generic word that a competitor owns is not a brand
    assert {"virtual paralegal services cost", "paralegal certification requirements"} <= kws


def test_impact_is_the_topics_own_numbers_or_nothing():
    """'Builds topical authority — compounds into rankings for the whole
    cluster.' was on eight of ten topics."""
    rows = [row(query="legal intake checklist", impressions=900, position=15.0)]
    brand = {"id": "b", "domain": "x.com", "seeds": ["legal intake checklist", "remote legal secretary",
                                                    "virtual staffing contract terms"]}
    ranked, _ = topics.build_topics(brand, rows, [], search=None)
    by_kw = {t["keyword"]: t for t in ranked}
    assert by_kw["legal intake checklist"]["impact"] == (
        "Could bring ~99 clicks a month for “legal intake checklist” once it ranks on page one.")
    assert by_kw["remote legal secretary"]["impact"] == "" == by_kw["virtual staffing contract terms"]["impact"]
    lines = [t["impact"] for t in ranked if t["impact"]]
    assert len(lines) == len(set(lines))


def test_a_run_lists_each_move_once(monkeypatch):
    """One run, end to end: internal-link advice on three page rows becomes ONE
    Fix-list item naming the pages, and the typed seeds that ARE Fix-list
    keywords stay in the blog plan pointing at their Fix-list position."""
    import json

    from seo_geo_agent import pages as pages_mod

    brand = insights.list_brands()[0]   # legalsoft: seeds "legal virtual assistant", "law firm answering service"
    monkeypatch.setattr(insights.competitors, "rank_snapshot", lambda b: {"snapshots": [{"at": "2026-10-14",
        "ranks": {"legal virtual assistant": {"position": 6, "top": ["comp.com"]},
                  "law firm answering service": {"position": None, "top": ["comp.com"]}}}]})
    paths = ["/careers", "/legal-assistant", "/intake-specialist-remote"]
    state.save("corpus-legalsoft", {"brand_id": "legalsoft", "pages": [
        {"url": f"https://legalsoft.com{p}", "title": "T", "word_count": 800, "meta_description": "d",
         "h1_count": 1, "images_no_alt": 0} for p in paths]})
    monkeypatch.setattr(pages_mod, "llm_text", lambda s, p, **kw: json.dumps(
        [{"path": p, "recommendation": f"Build internal links from the homepage to {p}."} for p in paths]))

    run = insights.run_brand(brand, trigger="t", today=date(2026, 10, 14))
    assert [t["kind"] for t in run["todos"]] == ["striking", "unranked", "site"]
    advice = run["todos"][2]
    assert (advice["origin"], advice["category"], advice["pages"]) == ("page-advice", "internal-links", paths)
    assert not [p for p in pages_mod.latest("legalsoft")["pages"] if p["ai"]]   # off the rows
    by_kw = {t["keyword"]: t for t in run["topics"]}
    assert by_kw["legal virtual assistant"]["also_in_fix"] == 1
    assert by_kw["law firm answering service"]["also_in_fix"] == 2
    # statuses stick to the item, like any to-do
    insights.set_todo_status("legalsoft", advice["id"], "assigned")
    again = insights.run_brand(brand, trigger="t", today=date(2026, 10, 14))
    assert next(t for t in again["todos"] if t["id"] == advice["id"])["status"] == "assigned"


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
