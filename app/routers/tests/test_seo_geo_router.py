"""Integration tests for the SEO agent router (/api/seo-geo). Fully offline.

The caller is a *non*-creator by default, which several tests below depend on:
they assert the 403 first and only then escalate. Both the default caller and
the escalation go through the shared harness in ``conftest.py``, whose autouse
guard restores the overrides map around every test — so escalating is safe
mid-test and needs no teardown here.
"""

import os

os.environ["SEO_OFFLINE"] = "1"

import pytest

from app.routers.tests.conftest import client

USER = {"id": "u1", "email": "t@legalsoft.com", "is_admin": False, "is_creator": False}
CREATOR = {**USER, "is_creator": True}


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch, as_caller):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_LOCAL_DIR", str(tmp_path))
    monkeypatch.delenv("SEO_CRON_KEY", raising=False)
    as_caller(USER)


@pytest.fixture()
def as_creator(as_caller):
    """Escalate the current test to a creator. Safe to call mid-test — the
    conftest guard restores whatever was in place before the test ran."""
    return lambda: as_caller(CREATOR)


def test_overview_lists_default_brand_and_sources():
    body = client.get("/api/seo-geo/overview").json()
    # offline: no grant is read, connect cannot work, nothing has read Search Console
    assert body["sources"] == {"gsc": False, "gsc_connect": False, "serp": False}
    assert body["brands"][0]["brand"]["id"] == "legalsoft"
    assert body["brands"][0]["last_run"] is None
    assert body["brands"][0]["gsc"] == {
        "connected": False, "mismatch": False, "property": None, "connected_by": None,
        "connected_at": None, "service_account_property": "sc-domain:legalsoft.com",
        "reading": False,
    }


def test_brand_create_requires_creator(as_creator):
    payload = {"name": "Acme", "domain": "acme.com"}
    assert client.post("/api/seo-geo/brands", json=payload).status_code == 403
    as_creator()
    body = client.post("/api/seo-geo/brands", json=payload).json()
    assert any(b["id"] == "acme" and b["gsc_property"] == "sc-domain:acme.com" for b in body["brands"])


def test_brand_domain_validation(as_creator):
    as_creator()
    r = client.post("/api/seo-geo/brands", json={"name": "Bad", "domain": "not-a-domain"})
    assert r.status_code == 422


def test_brand_domain_strips_www_and_protocol(as_creator):
    as_creator()
    body = client.post("/api/seo-geo/brands",
                       json={"name": "Berry", "domain": "https://www.BerryVirtual.com/"}).json()
    berry = next(b for b in body["brands"] if b["id"] == "berry")
    assert berry["domain"] == "berryvirtual.com"
    assert berry["gsc_property"] == "sc-domain:berryvirtual.com"


def test_run_and_detail_roundtrip():
    r = client.post("/api/seo-geo/run/legalsoft")
    assert r.status_code == 200, r.text
    assert r.json()["degraded"]  # offline: no Search Console
    detail = client.get("/api/seo-geo/brands/legalsoft").json()
    assert detail["run"]["trigger"] == "manual:t@legalsoft.com"


def test_unknown_brand_404():
    assert client.post("/api/seo-geo/run/nope").status_code == 404
    assert client.get("/api/seo-geo/brands/nope").status_code == 404


def test_todo_status_update_validates():
    assert client.post("/api/seo-geo/todos/legalsoft/abc123", json={"status": "later"}).status_code == 422
    r = client.post("/api/seo-geo/todos/legalsoft/abc123", json={"status": "assigned"})
    assert r.json() == {"id": "abc123", "status": "assigned"}


def test_keyword_lab_offline_runs_heuristic():
    r = client.post("/api/seo-geo/keywords/legalsoft/run")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["clusters"] and body["degraded"]
    assert client.get("/api/seo-geo/keywords/legalsoft").json()["lab"]["brand_id"] == "legalsoft"


def test_competitors_flow(as_creator):
    assert client.get("/api/seo-geo/competitors/legalsoft").json()["tracked"] == []
    assert client.put("/api/seo-geo/competitors/legalsoft", json={"domains": ["comp.com"]}).status_code == 403
    as_creator()
    r = client.put("/api/seo-geo/competitors/legalsoft", json={"domains": [" Comp.com "]})
    assert r.json()["tracked"] == ["comp.com"]
    t = client.post("/api/seo-geo/competitors/legalsoft/track")
    assert t.status_code == 200 and t.json()["degraded"]  # offline: no Serper, no fetches


def test_competitors_answer_carries_the_rank_history():
    """B4: the dashboard's "page one over the last checks" line needs one point
    per stored snapshot, oldest first, and the date of the latest check."""
    from seo_geo_agent import state as seo_state

    body = client.get("/api/seo-geo/competitors/legalsoft").json()
    assert (body["checked_at"], body["history"]) == (None, [])
    seo_state.save("ranks-legalsoft", {"suggested_competitors": [], "snapshots": [
        {"at": "2026-10-12", "ranks": {"a kw": {"position": 2, "top": []}, "b kw": {"position": None, "top": []}}},
        {"at": "2026-10-13", "ranks": {"a kw": {"position": 5, "top": []}, "b kw": {"position": 9, "top": []}}},
        {"at": "2026-10-14", "ranks": {"a kw": {"position": 1, "top": []}, "b kw": {"position": None, "top": []},
                                       "c kw": {"position": 3, "top": []}}},
    ]})
    body = client.get("/api/seo-geo/competitors/legalsoft").json()
    assert body["checked_at"] == "2026-10-14"
    assert body["history"] == [
        {"at": "2026-10-12", "tracked": 2, "top3": 1, "top10": 1, "unranked": 1},
        {"at": "2026-10-13", "tracked": 2, "top3": 0, "top10": 2, "unranked": 0},
        {"at": "2026-10-14", "tracked": 3, "top3": 2, "top10": 2, "unranked": 1},
    ]
    assert [s["keyword"] for s in body["shifts"]] == ["a kw", "c kw", "b kw"]   # unchanged


def test_competitor_profiles_endpoints_offline():
    assert client.get("/api/seo-geo/competitors/legalsoft/profiles").json()["profiles"] is None
    r = client.post("/api/seo-geo/competitors/legalsoft/profiles/refresh")
    assert r.status_code == 503  # offline: no rank snapshots to build profiles from


def test_live_analysis_endpoints_offline_503():
    assert client.post("/api/seo-geo/serp/legalsoft", json={"query": "x"}).status_code == 503
    assert client.post("/api/seo-geo/briefs/legalsoft", json={"keyword": "x"}).status_code == 503
    assert client.post("/api/seo-geo/audit/legalsoft/run").status_code == 503
    assert client.get("/api/seo-geo/briefs/legalsoft").json()["briefs"] == []
    assert client.get("/api/seo-geo/audit/legalsoft").json()["report"] is None


def test_oauth_start_offline_and_disconnect_gate():
    r = client.get("/api/seo-geo/oauth/start/legalsoft")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "gsc_offline"
    assert client.get("/api/seo-geo/oauth/start/nope").status_code == 404
    assert client.post("/api/seo-geo/oauth/disconnect/legalsoft").status_code == 403  # creator only
    detail = client.get("/api/seo-geo/brands/legalsoft").json()
    assert detail["gsc"]["connected"] is False and detail["gsc"]["property"] is None


def test_site_review_endpoints_offline():
    assert client.get("/api/seo-geo/site-review/legalsoft").json()["review"] is None
    assert client.post("/api/seo-geo/site-review/legalsoft").status_code == 503  # offline crawl
    detail = client.get("/api/seo-geo/brands/legalsoft").json()
    assert detail["plan"] == [] and detail["site_review"] is None


def test_site_review_with_a_wrong_shape_answer_is_503_not_500(monkeypatch):
    """A model answer that is a JSON list used to hit ``raw.get`` -> AttributeError
    -> an unexplained 500. It is a failed review: an honest 503 with the reason."""
    from seo_geo_agent import site_brain

    monkeypatch.setattr(site_brain, "build_corpus",
                        lambda brand, fetch=None, sitemap=None: {"brand_id": brand["id"], "page_count": 0,
                                                                 "pages": [], "degraded": []})
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: ["not", "an", "object"])
    r = client.post("/api/seo-geo/site-review/legalsoft")
    assert r.status_code == 503
    assert r.json()["detail"] == {
        "code": "seo_ai_bad_answer",
        "message": "The AI model's answer couldn't be used, so nothing was saved — run the site review again."}
    assert client.get("/api/seo-geo/site-review/legalsoft").json()["review"] is None


def test_analyze_website_adds_its_findings_to_the_fix_list_now(monkeypatch):
    """The console toasts "N finding(s) added to the fix list" after Analyze
    website, but the review saved only itself: the findings joined the Fix list
    at the next run. They join the stored run now; with no run yet, the answer
    says so instead of letting the toast claim it."""
    from seo_geo_agent import site_brain, state as seo_state

    corpus = {"brand_id": "legalsoft", "page_count": 1, "degraded": [], "pages": [
        {"url": "https://legalsoft.com/pricing", "title": "Pricing", "type": "other", "word_count": 180,
         "summary": "Plans and rates.", "schema_types": []}]}
    monkeypatch.setattr(site_brain, "build_corpus", lambda brand, fetch=None, sitemap=None: corpus)
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: {
        "positioning": "p", "scorecard": {}, "strengths": [], "suggested_seeds": [], "covered_topics": [],
        "missing_topics": [], "issues": [{
            "insight": "No testimonials on the pricing page", "action": "Add client reviews",
            "evidence": "https://legalsoft.com/pricing — no reviews shown", "priority": "high",
            "category": "trust"}]})

    first = client.post("/api/seo-geo/site-review/legalsoft")
    assert first.status_code == 200, first.text
    assert (first.json()["fix_list_updated"], first.json()["findings_on_fix_list"]) == (False, 0)

    seo_state.save("run-legalsoft", {"brand_id": "legalsoft", "at": "2026-10-14", "trigger": "t", "degraded": [],
                                     "summary": {}, "insights": [], "topics": [], "ga": None,
                                     "todos": [_todo("t1", "Refresh the page targeting 'x'")]})
    body = client.post("/api/seo-geo/site-review/legalsoft").json()
    assert (body["fix_list_updated"], body["findings_on_fix_list"]) == (True, 1)
    assert body["issues"][0]["category"] == "trust"                       # the review itself, as before
    todos = client.get("/api/seo-geo/brands/legalsoft").json()["run"]["todos"]
    assert [t["kind"] for t in todos] == ["striking", "site"]
    assert (todos[1]["page"], todos[1]["label"]) == ("https://legalsoft.com/pricing", "Trust fix: /pricing")
    assert "fix_list_updated" not in seo_state.load("sitereview-legalsoft")   # in the answer, never stored


#: What a provider failure looks like inside a CredentialMissing: an OpenRouter
#: error dict, a Google ``HttpError`` repr, an env var name.
RAW_FAILURES = {
    "seo_ai_unavailable": "LLM unavailable: Error code: 401 - {'error': {'message': 'User not found.', 'code': 401}}",
    "seo_search_unconfigured": "SEO_SERPER_API_KEY not set",
    "seo_search_unavailable": "Serper search failed: out of credits (HTTP 402)",
    "seo_needs_refresh": "Run a data refresh first — competitor discovery needs rank snapshots",
    "seo_unavailable": "Google Analytics rejected properties/123: <HttpError 403 when requesting "
                       "https://analyticsdata.googleapis.com/v1beta/properties/123:runReport returned "
                       "\"User does not have sufficient permissions\">",
}
#: (route, body, the agent module and function behind it) — every SEO route that answers 503.
LIVE_ROUTES = [
    ("/api/seo-geo/site-review/legalsoft", None, "site_brain", "analyze"),
    ("/api/seo-geo/serp/legalsoft", {"query": "virtual paralegal"}, "competitors", "serp_deep_dive"),
    ("/api/seo-geo/competitors/legalsoft/profiles/refresh", None, "competitors", "build_profiles"),
    ("/api/seo-geo/briefs/legalsoft", {"keyword": "virtual paralegal"}, "briefs", "build_brief"),
    ("/api/seo-geo/update-plan/legalsoft", {"page": "https://legalsoft.com/x"}, "briefs", "update_plan"),
    ("/api/seo-geo/audit/legalsoft/run", None, "audit", "site_audit"),
    ("/api/seo-geo/ask/legalsoft", {"question": "what next?"}, "advisor", "ask"),
    # a refresh degrades per source and answers 200; only one that produced
    # nothing at all reaches this
    ("/api/seo-geo/run/legalsoft", None, "insights", "run_brand"),
]


@pytest.mark.parametrize(("route", "body", "module", "func"), LIVE_ROUTES)
@pytest.mark.parametrize("code", sorted(RAW_FAILURES))
def test_a_live_route_that_fails_answers_a_code_and_a_sentence_never_the_exception(
        monkeypatch, caplog, route, body, module, func, code):
    """These routes answered ``detail=str(exc)``: provider reprs, error dicts and
    env var names reached the console. Now the OAuth routes' shape — a stable
    code and one plain sentence — and the raw text is in the log only."""
    import importlib

    from seo_geo_agent.sources import CredentialMissing

    raw = RAW_FAILURES[code]

    def fail(*args, **kwargs):
        raise CredentialMissing(raw)

    monkeypatch.setattr(importlib.import_module(f"seo_geo_agent.{module}"), func, fail)
    with caplog.at_level("WARNING", logger="agentos.seo_geo"):
        r = client.post(route, json=body) if body is not None else client.post(route)
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert set(detail) == {"code", "message"} and detail["code"] == code
    for leak in ("HttpError", "googleapis", "Error code", "User not found", "SEO_SERPER_API_KEY",
                 "{'error'", "properties/123", "competitor discovery"):
        assert leak not in r.text
    assert raw[:40] in caplog.text                       # the raw detail: server log only


def test_offline_live_routes_say_so_in_a_coded_sentence():
    r = client.post("/api/seo-geo/audit/legalsoft/run")
    assert r.status_code == 503
    assert r.json()["detail"] == {"code": "seo_offline",
                                  "message": "This server runs without live services, so the site audit "
                                             "can't run here."}


def _todo(tid, action, why="w", status="todo"):
    return {"id": tid, "kind": "striking", "page": "https://legalsoft.com/p", "query": "q",
            "action": action, "why": why, "est_monthly_clicks": 10, "position": 8,
            "impressions": 100, "status": status}


def test_this_week_strip_never_repeats_fix_list_first():
    """The strip sits right above the Fix list. Its first move used to be the Fix
    list's #1, so the same action showed twice, one under the other."""
    from seo_geo_agent import state as seo_state

    first = "Refresh the content and add internal links for 'legal virtual assistant'"
    seo_state.save("run-legalsoft", {
        "brand_id": "legalsoft", "at": "2026-10-14", "trigger": "t", "degraded": [], "summary": {},
        "insights": [], "topics": [], "ga": None, "todos": [
            _todo("t1", first),
            _todo("t2", "Refresh the content and add internal links for “legal virtual assistant”"),
            _todo("t3", "Rewrite the title and meta description targeting 'paralegal services'"),
        ]})
    seo_state.save("keywords-legalsoft", {"clusters": [
        {"name": "legal virtual assistant", "tier": "high", "recommendation": "same move as #1"},
        {"name": "law firm answering service", "tier": "high", "recommendation": "a new page"},
    ]})
    seo_state.save("audit-legalsoft", {"issues": [
        {"issue": "Broken or unreachable pages", "severity": "high", "fix": "Redirect them"}],
        "site_checks": []})

    plan = client.get("/api/seo-geo/brands/legalsoft").json()["plan"]
    actions = [p["action"] for p in plan]
    assert actions == [
        "Rewrite the title and meta description targeting 'paralegal services'",  # #2 restates #1
        "Go after “law firm answering service”",                                  # cluster 1 restates #1
        "Fix: Broken or unreachable pages",
    ]
    assert first not in actions


def test_this_week_strip_skips_fix_list_first_even_when_it_is_the_only_todo():
    from seo_geo_agent import state as seo_state

    seo_state.save("run-legalsoft", {
        "brand_id": "legalsoft", "at": "2026-10-14", "trigger": "t", "degraded": [], "summary": {},
        "insights": [], "topics": [], "ga": None, "todos": [_todo("t1", "Fix the pricing page")]})
    assert client.get("/api/seo-geo/brands/legalsoft").json()["plan"] == []


def test_this_week_strip_items_point_at_the_fix_list_instead_of_copying_it():
    """Staging's strip copied Fix #2's action and why word for word. A strip
    item that IS a Fix-list item now carries its id, its 1-based position and a
    label of at most eight words; the console renders "See Fix list #n"."""
    from seo_geo_agent import state as seo_state

    unranked = {"id": "912fbf2195ee", "kind": "unranked", "page": "", "query": "virtual paralegal services",
                "action": "Create or strengthen a page targeting 'virtual paralegal services'",
                "why": "Not in the top 10 — currently owned by virtualparalegalservices.com, agilelegal.com",
                "est_monthly_clicks": None, "position": 0, "impressions": None, "status": "todo"}
    seo_state.save("run-legalsoft", {
        "brand_id": "legalsoft", "at": "2026-10-14", "trigger": "t", "degraded": [], "summary": {},
        "insights": [], "topics": [], "ga": None, "todos": [
            {**unranked, "id": "d550b5585f0f", "kind": "striking", "query": "law firm intake services",
             "action": "Refresh the page targeting 'law firm intake services'", "position": 10},
            unranked]})
    seo_state.save("keywords-legalsoft", {"clusters": [
        {"name": "law firm answering service pricing for small and solo firms", "tier": "high",
         "recommendation": "a new page"}]})
    seo_state.save("audit-legalsoft", {"issues": [
        {"issue": "Broken or unreachable pages", "severity": "high", "fix": "Redirect them"}], "site_checks": []})

    plan = client.get("/api/seo-geo/brands/legalsoft").json()["plan"]
    fix, kw, audit_item = plan
    assert (fix["ref_todo_id"], fix["ref_index"]) == ("912fbf2195ee", 2)
    assert fix["label"] == "New page: “virtual paralegal services”"
    assert (fix["action"], fix["detail"]) == (unranked["action"], unranked["why"])   # kept for older consoles
    assert (kw["ref_todo_id"], kw["ref_index"], audit_item["ref_todo_id"]) == (None, None, None)
    assert kw["label"] == "Go after “law firm answering service pricing for…"
    assert audit_item["label"] == "Fix: Broken or unreachable pages"
    assert all(len(p["label"].split()) <= 8 for p in plan)


def test_brand_detail_rechecks_the_stored_review_and_says_failures_plainly():
    """D1 + D9 on the payload staging served: Fix #5's false "no form" claim
    and a raw ``<HttpError 403 …>`` in run.degraded."""
    from seo_geo_agent import state as seo_state

    seo_state.save("sitereview-legalsoft", {"brand_id": "legalsoft", "issues": [{
        "insight": "Contact page has no visible contact form, phone number, or calendar link",
        "evidence": "https://www.legalsoft.com/contact-us (920w) — 'Lacks visible contact form'",
        "action": "Embed a form", "priority": "high", "category": "conversion",
        "url": "https://www.legalsoft.com/contact-us"}], "grounding": {"kept": 1, "dropped": []}})
    seo_state.save("corpus-legalsoft", {"pages": [{"url": "https://www.legalsoft.com/contact-us",
                                                   "title": "Contact", "word_count": 920}]})
    seo_state.save("run-legalsoft", {
        "brand_id": "legalsoft", "at": "2026-10-14", "trigger": "t", "summary": {}, "insights": [],
        "topics": [], "ga": None, "todos": [], "degraded": [
            "Search Console: Search Console rejected sc-domain:legalsoft.com: <HttpError 403 when "
            "requesting https://searchconsole.googleapis.com/webmasters/v3/sites/x returned \"User does "
            "not have sufficient permission for site 'sc-domain:legalsoft.com'.\">"]})

    body = client.get("/api/seo-geo/brands/legalsoft").json()
    review = body["site_review"]
    assert review["issues"] == []
    assert review["grounding"]["dropped"][0]["reason"] == "unverifiable-absence"
    assert body["run"]["degraded"] == [
        "Search Console: no permission for property 'sc-domain:legalsoft.com' — the account this server "
        "reads with hasn't been given access."]
    overview = client.get("/api/seo-geo/overview").json()
    assert "HttpError" not in str(overview["brands"][0]["last_run"]["degraded"])


def test_a_missing_topic_in_the_blog_plan_is_pointed_at_not_repeated():
    """Staging repeated four site-review gaps word for word as blog topics. The
    review read by the console says where the plan has each one; the strings
    stay strings for a console that renders them as text."""
    from seo_geo_agent import state as seo_state

    gaps = ["Pricing transparency and cost comparison calculators", "Staff profiles and team credentials"]
    seo_state.save("sitereview-legalsoft", {"brand_id": "legalsoft", "issues": [],
                                            "grounding": {"kept": 0, "dropped": []}, "missing_topics": gaps})
    seo_state.save("run-legalsoft", {
        "brand_id": "legalsoft", "at": "2026-10-14", "trigger": "t", "summary": {}, "insights": [], "ga": None,
        "todos": [], "degraded": [], "topics": [
            {"keyword": "law firm intake services", "source": "seed", "avoided": False},
            {"keyword": "Pricing transparency and cost comparison calculators", "source": "site gap",
             "avoided": False}]})

    want = [{"topic": gaps[0], "in_blog_plan": 2}, {"topic": gaps[1], "in_blog_plan": None}]
    review = client.get("/api/seo-geo/brands/legalsoft").json()["site_review"]
    assert review["missing_topics"] == gaps
    assert review["missing_topics_detail"] == want
    assert client.get("/api/seo-geo/site-review/legalsoft").json()["review"]["missing_topics_detail"] == want


def test_pages_refresh_moves_site_wide_advice_onto_the_stored_fix_list(monkeypatch):
    import json

    from seo_geo_agent import pages as seo_pages, state as seo_state

    paths = ["/careers", "/legal-assistant", "/intake-specialist-remote"]
    seo_state.save("corpus-legalsoft", {"brand_id": "legalsoft", "pages": [
        {"url": f"https://legalsoft.com{p}", "title": "T", "word_count": 800, "meta_description": "d",
         "h1_count": 1, "images_no_alt": 0} for p in paths]})
    seo_state.save("run-legalsoft", {"brand_id": "legalsoft", "at": "2026-10-14", "trigger": "t",
                                     "degraded": [], "summary": {}, "insights": [], "topics": [], "ga": None,
                                     "todos": [_todo("t1", "Refresh the page targeting 'x'")]})
    monkeypatch.setattr(seo_pages, "llm_text", lambda s, p, **kw: json.dumps(
        [{"path": p, "recommendation": f"Build internal links from the homepage to {p}."} for p in paths]))

    body = client.post("/api/seo-geo/pages/legalsoft/refresh").json()
    assert not [p for p in body["pages"] if p["ai"]]
    assert [d["reason"] for d in body["grounding"]["dropped"]] == ["moved-to-fix-list"] * 3
    todos = client.get("/api/seo-geo/brands/legalsoft").json()["run"]["todos"]
    assert [t["id"] for t in todos][:1] == ["t1"]
    advice = todos[1]
    assert (advice["kind"], advice["origin"], advice["pages"]) == ("site", "page-advice", paths)
    # a second refresh replaces the item, never stacks a copy
    client.post("/api/seo-geo/pages/legalsoft/refresh")
    assert len(client.get("/api/seo-geo/brands/legalsoft").json()["run"]["todos"]) == 2


def test_pages_endpoints():
    assert client.get("/api/seo-geo/pages/legalsoft").json()["pages"] is None
    r = client.post("/api/seo-geo/pages/legalsoft/refresh")
    assert r.status_code == 409, r.text  # no corpus yet
    assert r.json()["detail"] == "Run the site analysis first"

    from seo_geo_agent import state as seo_state

    seo_state.save("corpus-legalsoft", {"brand_id": "legalsoft", "pages": [{
        "url": "https://legalsoft.com/pricing", "title": "Pricing", "word_count": 800,
        "meta_description": "d", "h1_count": 1, "images_no_alt": 0,
    }]})
    r = client.post("/api/seo-geo/pages/legalsoft/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ai"] is False  # offline: no LLM
    assert body["pages"][0]["path"] == "/pricing"
    assert client.get("/api/seo-geo/pages/legalsoft").json()["pages"]["pages"][0]["path"] == "/pricing"


def test_pages_refresh_forwards_gsc_failure_note():
    """A zeroed-out Pages tab must carry the reason it's zero, not read as a
    real all-zero measurement — offline mode means GSC degrades every time."""
    from seo_geo_agent import state as seo_state

    seo_state.save("corpus-legalsoft", {"brand_id": "legalsoft", "pages": [{
        "url": "https://legalsoft.com/pricing", "title": "Pricing", "word_count": 800,
        "meta_description": "d", "h1_count": 1, "images_no_alt": 0,
    }]})
    r = client.post("/api/seo-geo/pages/legalsoft/refresh")
    assert r.status_code == 200, r.text
    assert any(n.startswith("Search Console:") for n in r.json()["notes"])


def test_pages_refresh_forwards_ga_failure_note():
    from seo_geo_agent import insights, state as seo_state

    brand = next(b for b in insights.list_brands() if b["id"] == "legalsoft")
    insights.upsert_brand({**brand, "ga4_property": "properties/999"})
    seo_state.save("corpus-legalsoft", {"brand_id": "legalsoft", "pages": [{
        "url": "https://legalsoft.com/pricing", "title": "Pricing", "word_count": 800,
        "meta_description": "d", "h1_count": 1, "images_no_alt": 0,
    }]})
    r = client.post("/api/seo-geo/pages/legalsoft/refresh")
    assert r.status_code == 200, r.text
    assert any(n.startswith("Google Analytics:") for n in r.json()["notes"])


def test_ask_expert_offline_503():
    r = client.post("/api/seo-geo/ask/legalsoft", json={"question": "kya karna chahiye?"})
    assert r.status_code == 503
    assert client.post("/api/seo-geo/ask/legalsoft", json={"question": "  "}).status_code == 422


def test_draft_score_endpoint():
    r = client.post("/api/seo-geo/draft-score/legalsoft",
                    json={"text": "Buy now.", "keyword": "legal virtual assistant"})
    assert r.status_code == 200
    assert r.json()["verdict"] == "rework"


def test_cron_inert_without_key_then_gated(monkeypatch):
    assert client.post("/api/seo-geo/cron/run").status_code == 503
    monkeypatch.setenv("SEO_CRON_KEY", "s3cret")
    assert client.post("/api/seo-geo/cron/run", headers={"x-cron-key": "wrong"}).status_code == 403
    r = client.post("/api/seo-geo/cron/run", headers={"x-cron-key": "s3cret"})
    assert r.status_code == 200, r.text  # every brand ran → clean 200
    body = r.json()
    assert body["brands"]["legalsoft"]["ok"] is True
    assert body["status"] == "ok" and body["ok"] == 1 and body["failed"] == 0


def test_cron_all_brands_failed_is_not_200(monkeypatch):
    """C9: the sweep answered 200 even when every brand errored, so Cloud
    Scheduler recorded success, never retried, and the sweep could stay dead for
    weeks with the only evidence inside a body nobody reads."""
    from seo_geo_agent import insights

    monkeypatch.setenv("SEO_CRON_KEY", "s3cret")

    def _boom(brand, trigger=""):
        raise RuntimeError("GSC token revoked")

    monkeypatch.setattr(insights, "run_brand", _boom)
    r = client.post("/api/seo-geo/cron/run", headers={"x-cron-key": "s3cret"})
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["status"] == "failed" and body["ok"] == 0 and body["failed"] == 1
    assert body["brands"]["legalsoft"] == {"ok": False, "error": "GSC token revoked"}


def test_cron_partial_sweep_is_207(monkeypatch, as_creator):
    """One broken brand out of two is neither success nor total failure: 207 so a
    human can act, and still 2xx so a permanently bad brand can't put the job in
    a retry loop."""
    from seo_geo_agent import insights

    as_creator()
    assert client.post("/api/seo-geo/brands",
                       json={"name": "Acme", "domain": "acme.com"}).status_code == 200
    monkeypatch.setenv("SEO_CRON_KEY", "s3cret")
    real = insights.run_brand

    def _one_bad(brand, trigger=""):
        if brand["id"] == "acme":
            raise RuntimeError("GSC token revoked")
        return real(brand, trigger=trigger)

    monkeypatch.setattr(insights, "run_brand", _one_bad)
    r = client.post("/api/seo-geo/cron/run", headers={"x-cron-key": "s3cret"})
    assert r.status_code == 207, r.text
    body = r.json()
    assert body["status"] == "partial" and body["ok"] == 1 and body["failed"] == 1


def _serper(monkeypatch, status: int | None = None) -> list[str]:
    """Serper for these tests, no network: a plain SERP for every query, or —
    with ``status`` — the failure ``serper_search`` raises for that HTTP status.
    Returns the queries searched."""
    import httpx

    from seo_geo_agent import sources

    searched: list[str] = []

    def search(query, client=None):
        searched.append(query)
        if status is not None:
            request = httpx.Request("POST", sources.SERPER_ENDPOINT)
            refused = httpx.Response(status, request=request)
            raise sources._serper_failure(httpx.HTTPStatusError("refused", request=request, response=refused))
        return {"organic": [{"link": f"https://comp{i}.com/x", "title": "t", "position": i + 1} for i in range(10)],
                "related": [], "paa": [], "aio_present": False}

    monkeypatch.setattr(sources, "serper_available", lambda: True)
    monkeypatch.setattr(sources, "serper_search", search)
    return searched


def test_the_sweep_takes_one_rank_snapshot_per_brand_in_either_mode(monkeypatch):
    """B5: a rank-tracking run takes the night's snapshot, and the sweep then
    took a second one minutes later — the two latest snapshots were the same
    night (every shift ~0), the history covered half the nights, Serper spend
    doubled. A Search Console brand's run takes none, so the sweep still does."""
    from seo_geo_agent import insights, state as seo_state
    from seo_geo_agent.sources import CredentialMissing, QueryStat

    insights.upsert_brand({"id": "acme", "name": "Acme", "domain": "acme.com", "enabled": True,
                           "gsc_property": "sc-domain:acme.com", "seeds": ["acme widgets"]})

    def search_console(prop, start, end, service=None):
        if prop != "sc-domain:acme.com":
            raise CredentialMissing("Search Console rejected the property")
        return [QueryStat(query="acme widgets", page="https://acme.com/w", clicks=40, impressions=900,
                          ctr=0.044, position=6.0)]

    monkeypatch.setattr(insights, "gsc_fetch", search_console)
    _serper(monkeypatch)
    monkeypatch.setenv("SEO_CRON_KEY", "s3cret")
    r = client.post("/api/seo-geo/cron/run", headers={"x-cron-key": "s3cret"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert insights.latest_run("acme")["summary"]["mode"] == "search-console"
    assert insights.latest_run("legalsoft")["summary"]["mode"] == "rank-tracking"
    assert (body["brands"]["legalsoft"]["ranks"], body["brands"]["acme"]["ranks"]) == ("updated by the run", "updated")
    assert {b: len(seo_state.load(f"ranks-{b}")["snapshots"]) for b in ("legalsoft", "acme")} == {
        "legalsoft": 1, "acme": 1}
    assert body["rank_checks_failed"] == 0


def test_a_serper_dry_spell_saves_every_run_and_the_sweep_carries_on(monkeypatch, caplog):
    """A dry spell (402) raised out of run_brand for every brand that searched:
    nothing saved, the dashboard frozen on the last good run, the sweep a 502.
    Each run is now saved with the reason, its last good ranks carried over;
    the sweep moves on, counts the brands whose rankings weren't updated and
    logs them — and takes no second try that night."""
    from seo_geo_agent import insights, state as seo_state

    seo_state.save("ranks-legalsoft", {"snapshots": [{"at": "2026-10-13", "ranks": {
        "legal virtual assistant": {"position": 4, "top": ["comp.com"]}}}]})
    insights.upsert_brand({"id": "acme", "name": "Acme", "domain": "acme.com", "enabled": True,
                           "seeds": ["acme widgets"]})
    searched = _serper(monkeypatch, status=402)
    monkeypatch.setenv("SEO_CRON_KEY", "s3cret")
    with caplog.at_level("WARNING", logger="agentos.seo_geo"):
        r = client.post("/api/seo-geo/cron/run", headers={"x-cron-key": "s3cret"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["ok"], body["failed"], body["rank_checks_failed"]) == ("ok", 2, 0, 2)
    assert body["brands"]["legalsoft"]["ranks"] == "not updated: the run's rank check failed"
    assert "rankings not updated for 2 brand(s): acme, legalsoft" in caplog.text
    for brand_id, ranks_from in (("legalsoft", "2026-10-13"), ("acme", None)):
        run = client.get(f"/api/seo-geo/brands/{brand_id}").json()["run"]
        assert (run["summary"]["rank_check_failed"], run["summary"]["ranks_from"]) == (True, ranks_from)
        assert any(n.startswith("Rank tracking: Google results couldn't be reached") for n in run["degraded"])
    assert len(seo_state.load("ranks-legalsoft")["snapshots"]) == 1
    assert searched.count("legal virtual assistant") == 2   # the rank check + the blog plan — no sweep retry


def test_a_refresh_during_a_serper_dry_spell_answers_200_with_the_note(monkeypatch):
    """A manual Refresh in a dry spell answered a 500. It answers 200: the run
    is saved, the ranks are the last good check's and say so."""
    from seo_geo_agent import state as seo_state

    seo_state.save("ranks-legalsoft", {"snapshots": [{"at": "2026-10-13", "ranks": {
        "legal virtual assistant": {"position": 4, "top": ["comp.com"]}}}]})
    _serper(monkeypatch, status=429)
    r = client.post("/api/seo-geo/run/legalsoft")
    assert r.status_code == 200, r.text
    summary = r.json()["summary"]
    assert (summary["rank_check_failed"], summary["ranks_from"]) == (True, "2026-10-13")
    assert (summary["tracked"], summary["top10"]) == (1, 1)
    assert [n for n in r.json()["degraded"] if n.startswith("Rank tracking")] == [
        "Rank tracking: Google results couldn't be reached on this check (the search service is turning "
        "requests away for now), so rankings weren't updated."]
    card = client.get("/api/seo-geo/overview").json()["brands"][0]
    assert card["last_run"]["rank_check_failed"] is True


# ----------------------- Search Console connect (OAuth) -----------------------
#
# Google returns to the console's ``/oauth/google``; the console posts
# ``{code, state}`` to ``POST /api/seo-geo/oauth/complete`` with the member's
# bearer. Google itself is stubbed at ``gsc_oauth._exchange`` / ``_sites``.

OTHER = {"id": "u2", "email": "other@legalsoft.com", "is_admin": False, "is_creator": False}
TOKENS = {"refresh_token": "r1", "access_token": "a1",
          "scope": "https://www.googleapis.com/auth/webmasters.readonly"}
LEGALSOFT_OWNED = [{"siteUrl": "sc-domain:legalsoft.com", "permissionLevel": "siteOwner"}]
GSC_KEYS = {"connected", "mismatch", "property", "connected_by", "connected_at",
            "service_account_property", "reading"}


@pytest.fixture()
def gsc(monkeypatch):
    """Configured connect, in-memory grant store, outbound HTTP refused. Only
    ``gsc_oauth._online`` flips; the brand registry stays on local files."""
    from types import SimpleNamespace

    import httpx
    from cryptography.fernet import Fernet

    from app.config import settings
    from app.services import firestore_repo
    from seo_geo_agent import gsc_oauth

    key = Fernet.generate_key()
    monkeypatch.setattr(settings, "inbox_google_client_id", "cid", raising=False)
    monkeypatch.setattr(settings, "inbox_google_client_secret", "sec", raising=False)
    monkeypatch.setattr(settings, "inbox_token_key", key.decode(), raising=False)
    monkeypatch.setattr(settings, "app_public_url", "https://console.example", raising=False)
    monkeypatch.setattr(gsc_oauth, "_online", lambda: True)
    store: dict = {}
    monkeypatch.setattr(firestore_repo, "get_gsc_grants",
                        lambda ids: {b: dict(store[b]) for b in ids if b in store})
    monkeypatch.setattr(firestore_repo, "save_gsc_grant",
                        lambda brand_id, grant: store.__setitem__(brand_id, dict(grant)))
    monkeypatch.setattr(firestore_repo, "delete_gsc_grant",
                        lambda brand_id: store.pop(brand_id, None))

    def _no_network(*args, **kwargs):
        raise AssertionError("outbound network attempted from a test")

    monkeypatch.setattr(httpx, "post", _no_network)
    monkeypatch.setattr(httpx, "get", _no_network)
    monkeypatch.setattr(gsc_oauth, "_exchange", lambda code: dict(TOKENS))
    monkeypatch.setattr(gsc_oauth, "_sites", lambda token: LEGALSOFT_OWNED)
    return SimpleNamespace(store=store, fernet=Fernet(key), oauth=gsc_oauth)


def _start_state(brand_id: str = "legalsoft") -> str:
    from urllib.parse import parse_qs, urlparse

    r = client.get(f"/api/seo-geo/oauth/start/{brand_id}")
    assert r.status_code == 200, r.text
    return parse_qs(urlparse(r.json()["url"]).query)["state"][0]


def test_there_is_no_public_callback_route_any_more():
    """The console finishes the connect with the member's bearer; nothing on
    this path depends on an anonymous route or Cloud Run's public invoker."""
    from fastapi.routing import APIRoute

    from app.main import app as fastapi_app

    paths = {r.path for r in fastapi_app.routes if isinstance(r, APIRoute)}
    assert not any(p.endswith("/oauth/callback") and "seo-geo" in p for p in paths)
    assert client.get("/api/seo-geo/oauth/callback",
                      params={"code": "c", "state": "s"}).status_code in (404, 405)


def test_connect_unconfigured_is_a_coded_503_and_the_names_stay_in_the_log(
        gsc, monkeypatch, caplog):
    from app.config import settings

    monkeypatch.setattr(settings, "inbox_google_client_secret", "", raising=False)
    with caplog.at_level("WARNING", logger="agentos.seo_geo"):
        r = client.get("/api/seo-geo/oauth/start/legalsoft")
    assert r.status_code == 503
    assert r.json()["detail"] == {
        "code": "gsc_oauth_unconfigured",
        "message": "Search Console connect isn't set up on this server yet — an admin "
                   "needs to finish the Google setup.",
    }
    for leak in ("INBOX_", "GOOGLE_CLIENT", "SECRET", "TOKEN_KEY"):
        assert leak not in r.text
    warned = [rec for rec in caplog.records if rec.levelname == "WARNING"]
    assert any("INBOX_GOOGLE_CLIENT_SECRET" in rec.getMessage() for rec in warned)

    complete = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": "gsc.x.y"})
    assert complete.status_code == 503
    assert complete.json()["detail"]["code"] == "gsc_oauth_unconfigured"
    assert gsc.store == {}


def test_connect_round_trip_records_who_connected_and_seals_the_token(gsc):
    from urllib.parse import parse_qs, urlparse

    url = client.get("/api/seo-geo/oauth/start/legalsoft").json()["url"]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["redirect_uri"] == "https://console.example/oauth/google"
    assert q["scope"] == "https://www.googleapis.com/auth/webmasters.readonly"
    assert q["state"].startswith("gsc.")

    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": q["state"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["brand_id"] == "legalsoft"
    assert set(body["gsc"]) == GSC_KEYS
    assert body["gsc"]["connected"] is True and body["gsc"]["mismatch"] is False
    assert body["gsc"]["property"] == "sc-domain:legalsoft.com"
    assert body["gsc"]["connected_by"] == "t@legalsoft.com"
    assert body["gsc"]["connected_at"]
    assert "r1" not in r.text and "refresh_token" not in r.text

    doc = gsc.store["legalsoft"]
    assert "refresh_token" not in doc
    assert gsc.fernet.decrypt(doc["refresh_token_enc"].encode()).decode() == "r1"
    assert doc["connected_by"] == "t@legalsoft.com" and doc["connected_by_user_id"] == "u1"

    overview = client.get("/api/seo-geo/overview").json()
    card = next(c for c in overview["brands"] if c["brand"]["id"] == "legalsoft")
    assert card["gsc_connected"] is True
    assert card["gsc"]["connected_by"] == "t@legalsoft.com"
    assert overview["sources"]["gsc"] is True and overview["sources"]["gsc_connect"] is True
    detail = client.get("/api/seo-geo/brands/legalsoft").json()
    assert detail["gsc"] == card["gsc"]
    assert "r1" not in str(overview) + str(detail)


def test_complete_refuses_a_state_issued_to_someone_else(gsc, as_caller):
    state = _start_state()
    as_caller(OTHER)
    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": state})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "gsc_state_other_user"
    assert gsc.store == {}


def test_complete_refuses_a_tampered_or_expired_state(gsc):
    import time as _time

    state = _start_state()
    tampered = client.post("/api/seo-geo/oauth/complete",
                           json={"code": "c", "state": state[:-4] + "0000"})
    assert tampered.status_code == 400
    assert tampered.json()["detail"]["code"] == "gsc_state_invalid"

    stale = gsc.oauth.make_state("legalsoft", "u1",
                                 now=_time.time() - gsc.oauth.STATE_TTL_SECONDS - 5)
    expired = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": stale})
    assert expired.status_code == 400
    assert expired.json()["detail"]["code"] == "gsc_state_expired"
    assert gsc.store == {}


def test_complete_rejects_an_empty_body_at_the_boundary(gsc):
    assert client.post("/api/seo-geo/oauth/complete", json={"code": "", "state": ""}).status_code == 422
    assert client.post("/api/seo-geo/oauth/complete",
                       json={"code": "c", "state": "x" * 5000}).status_code == 422


def test_complete_says_plainly_when_the_account_cannot_see_the_site(gsc, monkeypatch):
    monkeypatch.setattr(gsc.oauth, "_sites", lambda token: [])
    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": _start_state()})
    assert r.status_code == 422
    assert r.json()["detail"] == {
        "code": "gsc_no_property",
        "message": "This Google account has no Search Console access to legalsoft.com — "
                   "sign in with an account that does, or ask the site owner to add it.",
    }
    assert gsc.store == {}


def test_any_member_connects_but_only_a_creator_disconnects(gsc, as_creator):
    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": _start_state()})
    assert r.status_code == 200, r.text
    assert client.post("/api/seo-geo/oauth/disconnect/legalsoft").status_code == 403
    assert "legalsoft" in gsc.store
    as_creator()
    assert client.post("/api/seo-geo/oauth/disconnect/legalsoft").json() == {"connected": False}
    assert gsc.store == {}
    overview = client.get("/api/seo-geo/overview").json()
    assert overview["sources"]["gsc"] is False
    assert overview["brands"][0]["gsc"]["connected"] is False


def test_deleting_a_brand_takes_its_search_console_grant_with_it(gsc, as_creator):
    """Hard delete through the route: the brand's sealed token goes with it, so
    no grant outlives the brand that the disconnect route would need to reach it."""
    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": _start_state()})
    assert r.status_code == 200, r.text
    assert "legalsoft" in gsc.store
    assert client.delete("/api/seo-geo/brands/legalsoft").status_code == 403   # creator only
    assert "legalsoft" in gsc.store
    as_creator()
    assert client.delete("/api/seo-geo/brands/legalsoft").status_code == 200
    assert gsc.store == {}


def test_overview_reading_flag_comes_from_the_latest_run_not_from_the_cloud_flag():
    """``sources.gsc`` used to be ``use_cloud()`` — "Connected" in prod with
    nothing connected. It is now true only when a brand really has Search
    Console behind it; here, a latest run that read Search Console rows."""
    from seo_geo_agent import state

    assert client.get("/api/seo-geo/overview").json()["sources"]["gsc"] is False
    state.save("run-legalsoft", {"at": "2026-10-14T00:00:00Z", "summary": {"mode": "search-console"},
                                 "degraded": [], "todos": [], "topics": [], "gsc_rows": 412})
    body = client.get("/api/seo-geo/overview").json()
    card = body["brands"][0]
    assert card["gsc"]["reading"] is True and card["gsc"]["connected"] is False
    assert body["sources"]["gsc"] is True


def test_overview_cards_carry_the_dashboard_fields_from_documents_already_read(monkeypatch):
    """B2: the portfolio sorts brands by what slipped, what failed and what is
    still open. Every field comes off the documents the overview already
    reads — the registry once, then per brand the run, its status overlay and
    the site review — never off another (the crawl corpus is large)."""
    from seo_geo_agent import insights, state as seo_state

    seo_state.save("run-legalsoft", {
        "brand_id": "legalsoft", "at": "2026-10-14", "trigger": "cron", "degraded": [], "insights": [],
        "topics": [], "ga": {"property": "properties/1", "totals": {}},
        "summary": {"mode": "rank-tracking", "moved_down": 2, "fell_off": 1, "rank_check_failed": True},
        "todos": [_todo("t1", "a"), _todo("t2", "b"), _todo("t3", "c", status="done")]})
    insights.set_todo_status("legalsoft", "t1", "done")
    seo_state.save("sitereview-legalsoft", {"brand_id": "legalsoft", "at": "2026-10-12", "issues": []})
    insights.upsert_brand({"id": "acme", "name": "Acme", "domain": "acme.com", "enabled": False})

    reads: list[str] = []
    real_load = seo_state.load
    monkeypatch.setattr(seo_state, "load", lambda doc_id: reads.append(doc_id) or real_load(doc_id))
    cards = {c["brand"]["id"]: c for c in client.get("/api/seo-geo/overview").json()["brands"]}

    legalsoft = cards["legalsoft"]
    assert (legalsoft["site_review_at"], legalsoft["ga_reading"]) == ("2026-10-12", True)
    assert {k: legalsoft["last_run"][k] for k in ("at", "todo_count", "open_count", "drops", "rank_check_failed")} \
        == {"at": "2026-10-14", "todo_count": 3, "open_count": 1, "drops": 3, "rank_check_failed": True}
    acme = cards["acme"]                       # switched off, still listed: GEO › Brands reads it
    assert (acme["last_run"], acme["site_review_at"], acme["ga_reading"]) == (None, None, False)
    assert sorted(reads) == sorted(["brands", "run-legalsoft", "todos-legalsoft", "sitereview-legalsoft",
                                    "run-acme", "sitereview-acme"])


@pytest.mark.parametrize(("summary", "reading"), [
    ({"mode": "search-console", "impressions_28d": 5400}, True),   # it read rows
    ({"mode": "search-console", "impressions_28d": 0}, False),     # the both-failed summary
    ({"mode": "rank-tracking"}, False),
])
def test_a_run_stored_before_gsc_rows_reads_from_its_impressions(summary, reading):
    from seo_geo_agent import state

    state.save("run-legalsoft", {"at": "2026-10-13T00:00:00Z", "summary": summary,
                                 "degraded": [], "todos": [], "topics": []})
    assert client.get("/api/seo-geo/overview").json()["brands"][0]["gsc"]["reading"] is reading


def _both_sources_fail(monkeypatch):
    from seo_geo_agent import insights
    from seo_geo_agent.sources import CredentialMissing

    def no_search_console(*args, **kwargs):
        raise CredentialMissing("Search Console refused the property")

    def no_serper(brand):
        raise CredentialMissing("Serper credits exhausted")

    monkeypatch.setattr(insights, "gsc_fetch", no_search_console)
    monkeypatch.setattr(insights.competitors, "rank_snapshot", no_serper)


def test_a_run_where_search_console_and_rank_tracking_both_failed_is_not_reading(monkeypatch):
    """The empty summary such a run stores still says mode "search-console";
    Integrations read that as "Connected" with no data behind it."""
    _both_sources_fail(monkeypatch)
    assert client.post("/api/seo-geo/run/legalsoft").status_code == 200
    run = client.get("/api/seo-geo/brands/legalsoft").json()["run"]
    assert run["summary"]["mode"] == "search-console" and run["gsc_rows"] == 0

    body = client.get("/api/seo-geo/overview").json()
    assert body["brands"][0]["gsc"]["reading"] is False
    assert body["sources"]["gsc"] is False
    assert client.get("/api/seo-geo/brands/legalsoft").json()["gsc"]["reading"] is False


def test_a_grant_still_shows_connected_when_its_run_failed_to_read(gsc, monkeypatch):
    """``connected`` is the stored grant; ``reading`` is what the run got. A
    grant whose run read nothing is connected, not reading."""
    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": _start_state()})
    assert r.status_code == 200, r.text
    monkeypatch.setattr(gsc.oauth, "service", lambda brand_id: object())   # no Google client
    _both_sources_fail(monkeypatch)
    assert client.post("/api/seo-geo/run/legalsoft").status_code == 200

    body = client.get("/api/seo-geo/overview").json()
    assert body["brands"][0]["gsc"]["connected"] is True
    assert body["brands"][0]["gsc"]["reading"] is False
    assert body["sources"]["gsc"] is True   # a grant exists


def test_connecting_a_property_another_brand_reads_is_a_coded_409(gsc, as_creator):
    as_creator()
    assert client.post("/api/seo-geo/brands", json={"name": "Twin", "domain": "legalsoft.com"}
                       ).status_code == 200
    gsc.store["twin"] = {"brand_id": "twin", "property": "sc-domain:legalsoft.com",
                         "refresh_token_enc": "t"}
    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": _start_state()})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {
        "code": "gsc_property_taken",
        "message": "sc-domain:legalsoft.com is already connected to Twin — one Search "
                   "Console property can feed only one brand. Disconnect it there first.",
    }
    assert "legalsoft" not in gsc.store


GEO_ONLY = {"id": "g1", "email": "contractor@aivirtual.com", "is_admin": False,
            "is_creator": False, "is_geo_only": True}


def test_a_geo_only_caller_gets_the_overview_without_any_search_console_block(gsc, as_caller):
    """GEO-only contractors reach this route (the console shell reads it) but
    use no Search Console data, so a staff member's connect — their email, when,
    and the property — never reaches them."""
    r = client.post("/api/seo-geo/oauth/complete", json={"code": "c", "state": _start_state()})
    assert r.status_code == 200, r.text
    staff = client.get("/api/seo-geo/overview").json()
    assert staff["brands"][0]["gsc"]["connected_by"] == "t@legalsoft.com"

    as_caller(GEO_ONLY)
    r = client.get("/api/seo-geo/overview")
    assert r.status_code == 200
    for card in r.json()["brands"]:
        assert "gsc" not in card and "gsc_connected" not in card
        assert card["brand"]["id"]                          # the rest of the card is intact
    assert "t@legalsoft.com" not in r.text and "connected_at" not in r.text


@pytest.mark.parametrize(("grant", "why"), [
    ({"brand_id": "legalsoft", "property": "sc-domain:oldsite.com"}, "domain edited after connect"),
    ({"brand_id": "someone-else", "property": "sc-domain:legalsoft.com"}, "stored for another brand"),
])
def test_a_grant_for_a_different_site_is_never_shown_as_connected(gsc, grant, why):
    """The run ignores a foreign grant, so nothing may call the brand Connected
    on its strength: ``connected`` false, ``mismatch`` true, and the property
    shown is the one to replace — on the card, the detail and the integration
    flag alike."""
    gsc.store["legalsoft"] = {**grant, "refresh_token_enc": "sealed",
                              "connected_by": "t@legalsoft.com"}

    overview = client.get("/api/seo-geo/overview").json()
    card = overview["brands"][0]
    assert (card["gsc"]["connected"], card["gsc"]["mismatch"]) == (False, True), why
    assert card["gsc"]["property"] == grant["property"]
    assert card["gsc_connected"] is False
    assert overview["sources"]["gsc"] is False             # Integrations: not Connected

    detail = client.get("/api/seo-geo/brands/legalsoft").json()["gsc"]
    assert (detail["connected"], detail["mismatch"]) == (False, True)

