"""Tests for the researcher layer: keyword lab, competitors, briefs, audit, advisor."""
from contextlib import contextmanager

import pytest

from seo_geo_agent import (advisor, audit, briefs, competitors, gsc_oauth, insights, keywords,
                           site_brain, topics)
from seo_geo_agent.sources import CredentialMissing, PageFacts, QueryStat


def row(query, page="https://x.com/a", clicks=10, impressions=500, position=8.0):
    return QueryStat(query=query, page=page, clicks=clicks, impressions=impressions,
                     ctr=clicks / impressions, position=position)


def facts(url, status=200, title="Title", meta="desc", h1=None, h2=None, h3=None,
          schema=None, words=1500, no_alt=0):
    f = PageFacts(url=url, status=status, title=title, meta_description=meta, word_count=words)
    f.h1 = h1 if h1 is not None else ["Main heading"]
    f.h2 = h2 or []
    f.h3 = h3 or []
    f.schema_types = schema or []
    f.images_no_alt = no_alt
    return f


BRAND = {"id": "b", "name": "Acme Legal", "domain": "x.com",
         "seeds": ["legal virtual assistant"], "competitors": ["comp.com"], "enabled": True}


# ------------------------------- keyword lab -------------------------------

def test_intent_heuristic():
    assert keywords.intent_of("hire a legal virtual assistant") == "transactional"
    assert keywords.intent_of("best clio alternatives") == "commercial"
    assert keywords.intent_of("how to answer legal calls") == "informational"
    assert keywords.intent_of("paralegal services near me") == "local"


def test_expand_offline_uses_seeds_and_gsc():
    kws, notes = keywords.expand_keywords(BRAND, [row("virtual receptionist for lawyers")])
    assert "legal virtual assistant" in kws
    assert "virtual receptionist for lawyers" in kws
    assert any("Serper" in n for n in notes) and any("LLM" in n for n in notes)


def test_heuristic_clusters_group_by_shared_tokens():
    clusters = keywords._heuristic_clusters([
        "legal virtual assistant", "legal virtual assistant cost", "law firm marketing ideas",
    ])
    sizes = sorted(len(c["keywords"]) for c in clusters)
    assert sizes == [1, 2]


def test_cluster_scoring_gap_vs_ranking():
    clusters = [
        {"name": "legal virtual assistant", "intent": "commercial",
         "keywords": ["legal virtual assistant"]},
        {"name": "law firm marketing", "intent": "informational",
         "keywords": ["law firm marketing"]},
    ]
    rows = [row("legal virtual assistant", impressions=1000, position=4.0)]
    keywords.score_clusters(clusters, rows)
    ranked = {c["name"]: c for c in clusters}
    assert ranked["legal virtual assistant"]["coverage"] == "ranking"
    assert ranked["law firm marketing"]["coverage"] == "gap"
    # Act-now tiers sort above already-ranking "watch" clusters, whatever the volume.
    assert clusters[0]["name"] == "law firm marketing"
    assert ranked["legal virtual assistant"]["tier"] == "watch"


def test_cluster_scoring_uses_rank_snapshot_when_no_gsc():
    clusters = [{"name": "legal virtual assistant", "intent": "commercial",
                 "keywords": ["legal virtual assistant"]}]
    keywords.score_clusters(clusters, [], ranks={"legal virtual assistant": 12.0})
    assert clusters[0]["coverage"] == "weak"
    assert clusters[0]["best_position"] == 12


def test_tiers_and_recommendations():
    clusters = [
        {"name": "hire legal virtual assistant", "intent": "transactional",
         "keywords": ["hire legal virtual assistant"]},
        {"name": "what is legal intake", "intent": "informational",
         "keywords": ["what is legal intake"]},
        {"name": "legal answering service", "intent": "commercial",
         "keywords": ["legal answering service"]},
    ]
    rows = [row("legal answering service", impressions=500, position=2.0)]
    keywords.score_clusters(clusters, rows)
    by_name = {c["name"]: c for c in clusters}
    assert by_name["hire legal virtual assistant"]["tier"] == "high"      # buyer + gap
    assert by_name["what is legal intake"]["tier"] == "medium"            # info + gap
    assert by_name["legal answering service"]["tier"] == "watch"          # already ranking
    assert "create a dedicated page" in by_name["hire legal virtual assistant"]["recommendation"]
    assert clusters[0]["tier"] == "high"  # high tier sorts first


def test_cluster_owners_recorded():
    clusters = [{"name": "kw one", "intent": "transactional", "keywords": ["kw one"],
                 "tier": "high", "coverage": "gap", "opportunity": 10}]
    keywords.add_cluster_owners(clusters, "x.com", search=lambda q: serp_with(2))
    assert clusters[0]["owned_by"] == ["comp.com", "reddit.com"]  # own domain excluded
    assert clusters[0]["aio_present"] is False


def test_topics_include_new_ideas_with_priority(monkeypatch):
    monkeypatch.setattr(topics.sources, "llm_json",
                        lambda system, prompt: ["paralegal outsourcing checklist"])
    brand = {"id": "b", "domain": "x.com", "seeds": []}
    ranked, _ = topics.build_topics(brand, [], [], search=None)
    idea = next(t for t in ranked if t["source"] == "new idea")
    assert idea["keyword"] == "paralegal outsourcing checklist"
    assert idea["priority"] in ("high", "medium", "low")
    assert idea["impact"]


def test_run_keyword_lab_persists_and_lists_gaps():
    doc = keywords.run_keyword_lab(BRAND, [], trigger="test")
    assert doc["degraded"]
    assert doc["clusters"]
    assert keywords.latest("b")["keyword_count"] == doc["keyword_count"]
    assert doc["gaps"]  # nothing ranks -> everything is a gap


# ------------------------------- competitors -------------------------------

def serp_with(pos_ours):
    organic = [{"link": "https://comp.com/p1", "title": "Comp", "position": 1},
               {"link": "https://reddit.com/r/law", "title": "Reddit", "position": 2}]
    if pos_ours:
        organic.insert(pos_ours - 1, {"link": "https://x.com/page", "title": "Us", "position": pos_ours})
    return {"organic": organic[:10], "related": [], "paa": ["How much does it cost?"], "aio_present": False}


def test_tracked_keywords_fall_back_to_site_review_seeds(monkeypatch):
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: REVIEW_JSON)
    site_brain.expert_review(BRAND, {"brand_id": "b", "page_count": 1, "pages": [], "degraded": []})
    seedless = {**BRAND, "seeds": []}
    tracked = competitors.tracked_keywords(seedless)
    assert "legal virtual assistant" in tracked  # from the site review's suggested_seeds


def test_rank_snapshot_and_shifts():
    competitors.rank_snapshot(BRAND, search=lambda q: serp_with(5))
    competitors.rank_snapshot(BRAND, search=lambda q: serp_with(3))
    shifts = competitors.rank_shifts("b")
    assert shifts[0]["position"] == 3 and shifts[0]["previous"] == 5 and shifts[0]["delta"] == 2
    doc = competitors.rank_snapshot(BRAND, search=lambda q: serp_with(3))
    assert "comp.com" in doc["suggested_competitors"]
    assert "x.com" not in doc["suggested_competitors"]


def test_sitemap_watch_flags_new_content():
    first = competitors.sitemap_watch(BRAND, fetch_sitemap=lambda d: ["https://comp.com/a", "https://comp.com/b"])
    assert first["comp.com"]["first_check"] is True and first["comp.com"]["new_count"] == 0
    second = competitors.sitemap_watch(
        BRAND, fetch_sitemap=lambda d: ["https://comp.com/a", "https://comp.com/b", "https://comp.com/new"])
    assert second["comp.com"]["new_count"] == 1
    assert second["comp.com"]["new_urls"] == ["https://comp.com/new"]


def test_serp_deep_dive_extracts_shared_structure():
    pages = {
        "https://comp.com/p1": facts("https://comp.com/p1", h2=["Pricing and costs", "How it works"], words=2000),
        "https://reddit.com/r/law": facts("https://reddit.com/r/law", h2=["Pricing and costs", "Is it worth it?"], words=1000),
        "https://x.com/page": facts("https://x.com/page", h2=["Our features"], words=900),
    }
    deep = competitors.serp_deep_dive(
        BRAND, "legal virtual assistant", search=lambda q: serp_with(2), fetch=lambda u: pages[u])
    assert deep["our_position"] == 2
    assert "Pricing and costs" in deep["common_themes"]
    assert "How much does it cost?" in deep["questions"]
    assert "Is it worth it?" in deep["questions"]
    assert deep["target_word_count"] == 1300
    assert deep["pages_analyzed"] == 3


# --------------------------------- briefs ---------------------------------

def test_build_brief_offline_falls_back_to_serp_outline():
    pages = {u: facts(u, h2=["Pricing and costs"], words=1000)
             for u in ("https://comp.com/p1", "https://reddit.com/r/law", "https://x.com/page")}
    brief = briefs.build_brief(BRAND, "legal virtual assistant", [],
                               search=lambda q: serp_with(None), fetch=lambda u: pages[u])
    assert brief["degraded"]  # LLM offline
    assert any("FAQ" in o["heading"] for o in brief["outline"])
    assert brief["target_word_count"] == 1000
    assert briefs.list_briefs("b")[0]["id"] == brief["id"]
    briefs.build_brief(BRAND, "legal virtual assistant", [],
                       search=lambda q: serp_with(None), fetch=lambda u: pages[u])
    assert len(briefs.list_briefs("b")) == 1  # same keyword replaces, no dupes


def test_update_plan_flags_missing_sections():
    rows = [row("legal virtual assistant", page="https://x.com/page", impressions=900)]
    pages = {
        "https://comp.com/p1": facts("https://comp.com/p1", h2=["Pricing and costs breakdown"], schema=["Article"], words=2000),
        "https://reddit.com/r/law": facts("https://reddit.com/r/law", h2=["Pricing and costs breakdown"], words=2000),
        "https://x.com/page": facts("https://x.com/page", h2=["Why choose us"], words=600),
    }
    plan = briefs.update_plan(BRAND, "https://x.com/page", rows,
                              search=lambda q: serp_with(3), fetch=lambda u: pages[u])
    assert any("Pricing and costs" in s for s in plan["suggestions"])
    assert any("structured data" in s.lower() for s in plan["suggestions"])
    assert any("Deepen" in s for s in plan["suggestions"])


def test_update_plan_needs_page_data():
    try:
        briefs.update_plan(BRAND, "https://x.com/unknown", [], search=lambda q: serp_with(None), fetch=facts)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


# --------------------------------- audit ---------------------------------

def test_site_audit_reports_issues_and_score():
    site = {
        "https://x.com/": facts("https://x.com/", title="Home", schema=["Organization"]),
        "https://x.com/a": facts("https://x.com/a", title="", meta=""),
        "https://x.com/broken": facts("https://x.com/broken", status=404),
    }
    report = audit.site_audit(
        BRAND, fetch=lambda u: site[u], sitemap=lambda d: list(site.keys()),
        get_text=lambda u: {"status": 200, "text": "User-agent: *\nSitemap: https://x.com/sitemap.xml",
                            "final_url": "https://x.com/"})
    names = [i["issue"] for i in report["issues"]]
    assert "Broken or unreachable pages" in names
    assert "Missing page title" in names
    assert report["pages_checked"] == 3 and report["pages_ok"] == 2
    assert 0 <= report["health_score"] < 100
    assert audit.latest_audit("b")["at"] == report["at"]


def test_site_checks_flag_missing_foundations():
    texts = {
        "https://x.com/robots.txt": {"status": 404, "text": "", "final_url": ""},
        "http://x.com/": {"status": 200, "text": "", "final_url": "http://x.com/"},
    }
    checks = {c["name"]: c for c in audit._site_checks("x.com", [], get_text=lambda u: texts[u])}
    assert checks["Sitemap.xml"]["ok"] is False and checks["Sitemap.xml"]["fix"]
    assert checks["Robots.txt"]["ok"] is False
    assert checks["HTTP → HTTPS redirect"]["ok"] is False


# -------------------------------- site brain --------------------------------

SITE = {
    "https://x.com/": facts("https://x.com/", title="Acme Legal — Home", h2=["What we do"]),
    "https://x.com/pricing": facts("https://x.com/pricing", title="Pricing", h2=["Plans"]),
}
SITE["https://x.com/"].text = "Acme Legal offers virtual assistants for law firms."
SITE["https://x.com/pricing"].text = "Plans start small and scale with your firm."

REVIEW_JSON = {
    "positioning": "Virtual assistants for US law firms.",
    "scorecard": {
        "intent": {"grade": 7, "note": "clamped"},          # out of range -> 5
        "content_depth": {"grade": 2, "note": "thin"},
        "trust": {"grade": "bad", "note": "ignored"},        # unparsable -> dropped
    },
    "strengths": ["Clear services"],
    "issues": [
        {"insight": "No testimonials anywhere", "evidence": "https://x.com/",
         "action": "Add 3 client testimonials to the homepage", "priority": "high", "category": "trust"},
        {"insight": "Pricing page is thin", "evidence": "https://x.com/pricing",
         "action": "Expand the pricing page with plan comparisons", "priority": "medium", "category": "content"},
    ],
    "suggested_seeds": ["legal virtual assistant", "law firm answering service"],
    "covered_topics": ["virtual assistants"],
    "missing_topics": ["pricing comparisons"],
}


def test_corpus_falls_back_without_llm():
    corpus = site_brain.build_corpus(BRAND, fetch=lambda u, c=None: SITE[u],
                                     sitemap=lambda d: list(SITE.keys()))
    assert corpus["page_count"] == 2
    assert any("Acme Legal" in p["summary"] for p in corpus["pages"])
    assert corpus["degraded"]  # offline: no AI summaries


def test_corpus_cache_skips_unchanged_pages(monkeypatch):
    calls = {"n": 0}

    def fake_llm(system, prompt):
        calls["n"] += 1
        return [{"summary": "s", "type": "service", "topics": ["t"]}] * prompt.count("PAGE ")

    monkeypatch.setattr(site_brain.sources, "llm_json", fake_llm)
    site_brain.build_corpus(BRAND, fetch=lambda u, c=None: SITE[u], sitemap=lambda d: list(SITE.keys()))
    first = calls["n"]
    site_brain.build_corpus(BRAND, fetch=lambda u, c=None: SITE[u], sitemap=lambda d: list(SITE.keys()))
    assert first >= 1 and calls["n"] == first  # second run: all pages cached


def test_expert_review_todos_and_seeds(monkeypatch):
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: REVIEW_JSON)
    corpus = {"brand_id": "b", "page_count": 2, "pages": [], "degraded": []}
    review = site_brain.expert_review(BRAND, corpus)
    assert review["positioning"].startswith("Virtual assistants")
    assert review["scorecard"]["intent"]["grade"] == 5      # clamped into 1-5
    assert review["scorecard"]["content_depth"]["grade"] == 2
    assert "trust" not in review["scorecard"]               # unparsable grade dropped

    todos = site_brain.site_todos("b")
    assert todos[0]["kind"] == "site" and todos[0]["action"].startswith("Add 3 client")
    assert todos[0]["est_monthly_clicks"] is None  # no invented numbers
    assert todos == site_brain.site_todos("b")  # stable ids + order

    empty_seed_brand = {**BRAND, "seeds": []}
    assert site_brain.effective_seeds(empty_seed_brand)["seeds"][0] == "legal virtual assistant"
    assert site_brain.effective_seeds(BRAND)["seeds"] == BRAND["seeds"]  # own seeds win


def test_run_brand_merges_site_todos(monkeypatch):
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: REVIEW_JSON)
    brand = insights.list_brands()[0]
    site_brain.expert_review(brand, {"brand_id": brand["id"], "page_count": 1, "pages": [], "degraded": []})
    run = insights.run_brand(brand, trigger="t")
    assert any(t["kind"] == "site" for t in run["todos"])


# ----------------------------- GSC OAuth connect -----------------------------
#
# The a12 Inbox pattern: Google returns to the console, which posts the code to
# an authenticated route. ``gsc_connect`` (conftest) configures the client,
# turns on only this module's online switch and swaps in an in-memory store.

TOKENS = {"refresh_token": "r1", "access_token": "a1", "scope": gsc_oauth.SCOPE}
OWNED = [{"siteUrl": "sc-domain:x.com", "permissionLevel": "siteOwner"}]


@contextmanager
def _refused(code: str):
    """``pytest.raises`` for one refusal code."""
    with pytest.raises(gsc_oauth.ConnectRefused) as info:
        yield info
    assert info.value.code == code, (info.value.code, info.value.message)


class _Resp:
    def __init__(self, status: int, body=None, text: str = ""):
        self.status_code, self._body, self.text = status, body, text or str(body or "")

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


def test_state_binds_purpose_brand_and_member(gsc_connect):
    token = gsc_oauth.make_state("berry", "user-a")
    assert token.startswith("gsc.")  # how /oauth/google tells it from an Inbox state
    assert gsc_oauth.read_state(token, user_id="user-a") == gsc_oauth.ConnectState("berry", "user-a")


def test_a_tampered_state_is_refused(gsc_connect):
    import base64
    import json

    token = gsc_oauth.make_state("berry", "user-a")
    with _refused(gsc_oauth.STATE_INVALID):
        gsc_oauth.read_state(token[:-2] + "00", user_id="user-a")
    # a different brand in the body, old signature kept
    body, sig = token.removeprefix("gsc.").split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["b"] = "someone-elses-brand"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    with _refused(gsc_oauth.STATE_INVALID):
        gsc_oauth.read_state(f"gsc.{forged}.{sig}", user_id="user-a")


def test_an_expired_state_is_refused(gsc_connect):
    token = gsc_oauth.make_state("berry", "user-a", now=1_000_000)
    assert gsc_oauth.read_state(token, user_id="user-a", now=1_000_000 + 600)
    with _refused(gsc_oauth.STATE_EXPIRED):
        gsc_oauth.read_state(token, user_id="user-a", now=1_000_000 + 601)


def test_another_members_state_is_refused(gsc_connect):
    token = gsc_oauth.make_state("berry", "user-a")
    with _refused(gsc_oauth.STATE_OTHER_USER):
        gsc_oauth.read_state(token, user_id="user-b")


@pytest.mark.parametrize("junk", [
    "",
    "0f3c9a.1700000000.ab12cd34ef56ab78.0123456789abcdef0123456789abcdef",  # a12-shaped
    "inbox.eyJ9.0123",                       # another purpose
    "gsc.",                                  # prefix only
    "gsc.a.b.c",                             # too many parts
    "gsc.éü.☃",               # non-ASCII must refuse, not 500
])
def test_a_state_that_is_not_ours_is_refused_not_crashed(gsc_connect, junk):
    with _refused(gsc_oauth.STATE_INVALID):
        gsc_oauth.read_state(junk, user_id="user-a")


def test_a_state_signed_with_another_secret_is_refused(gsc_connect, monkeypatch):
    from app.config import settings

    token = gsc_oauth.make_state("berry", "user-a")
    monkeypatch.setattr(settings, "inbox_google_client_secret", "rotated", raising=False)
    with _refused(gsc_oauth.STATE_INVALID):
        gsc_oauth.read_state(token, user_id="user-a")


def test_unconfigured_is_one_plain_sentence_and_the_names_go_to_the_log(gsc_connect, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "inbox_google_client_secret", "", raising=False)
    monkeypatch.setattr(settings, "inbox_token_key", "not-a-fernet-key", raising=False)
    assert gsc_oauth.connect_ready() is False
    assert gsc_oauth.missing_config() == [
        "INBOX_GOOGLE_CLIENT_SECRET", "INBOX_TOKEN_KEY (missing or not a Fernet key)",
    ]
    with _refused(gsc_oauth.UNCONFIGURED) as info:
        gsc_oauth.auth_url("b", user_id="user-a")
    assert "an admin needs to finish the Google setup" in info.value.message
    for leak in ("INBOX_", "GOOGLE_CLIENT", "SECRET", "TOKEN_KEY"):
        assert leak not in info.value.message
    assert "INBOX_GOOGLE_CLIENT_SECRET" in info.value.log_detail


def test_offline_refuses_before_anyone_is_sent_to_google(gsc_connect, monkeypatch):
    monkeypatch.setattr(gsc_oauth, "_online", lambda: False)
    assert gsc_oauth.connect_ready() is False
    with _refused(gsc_oauth.OFFLINE):
        gsc_oauth.auth_url("b", user_id="user-a")
    assert gsc_oauth.connection("b") is None  # and no grant is read offline


def test_auth_url_returns_to_the_console_and_asks_for_read_only(gsc_connect):
    from urllib.parse import parse_qs, urlparse

    url = gsc_oauth.auth_url("berry", user_id="user-a")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith(gsc_oauth.AUTH_ENDPOINT)
    assert q["redirect_uri"] == "https://console.example/oauth/google"
    assert q["scope"] == "https://www.googleapis.com/auth/webmasters.readonly"
    assert q["access_type"] == "offline" and "consent" in q["prompt"]
    assert q["client_id"] == "cid"
    # never folds in what the shared client was granted for a12's Gmail
    assert "include_granted_scopes" not in q
    assert gsc_oauth.read_state(q["state"], user_id="user-a").brand_id == "berry"


def test_match_property_prefers_domain_property():
    sites = [
        {"siteUrl": "https://www.berryvirtual.com/", "permissionLevel": "siteOwner"},
        {"siteUrl": "sc-domain:berryvirtual.com", "permissionLevel": "siteFullUser"},
        {"siteUrl": "https://other.com/", "permissionLevel": "siteUnverifiedUser"},
    ]
    assert gsc_oauth.match_property("berryvirtual.com", sites) == "sc-domain:berryvirtual.com"
    assert gsc_oauth.match_property("other.com", sites) is None  # unverified doesn't count


def _verified(brand_id="b", user_id="user-a"):
    return gsc_oauth.read_state(gsc_oauth.make_state(brand_id, user_id), user_id=user_id)


def test_complete_stores_the_token_sealed_with_who_connected_it(gsc_connect, monkeypatch):
    import json

    monkeypatch.setattr(gsc_oauth, "_exchange", lambda code: dict(TOKENS))
    monkeypatch.setattr(gsc_oauth, "_sites", lambda tok: OWNED)
    view = gsc_oauth.complete(_verified(), BRAND, code="c",
                              connected_by="Her@LegalSoft.com", user_id="user-a")

    assert view["property"] == "sc-domain:x.com"
    assert view["connected_by"] == "her@legalsoft.com"
    assert view["connected_at"].endswith("+00:00")
    assert set(view) == {"property", "connected_by", "connected_at"}  # never a token

    doc = gsc_connect.store["b"]
    assert "refresh_token" not in doc and "r1" not in json.dumps(doc)
    assert gsc_connect.fernet.decrypt(doc["refresh_token_enc"].encode()).decode() == "r1"
    assert doc["connected_by_user_id"] == "user-a"

    assert gsc_oauth.connection("b") == view
    assert gsc_oauth.connections(["b", "nobody"]) == {"b": view}
    gsc_oauth.disconnect("b")
    assert gsc_oauth.connection("b") is None


def test_complete_says_plainly_when_the_account_cannot_see_the_site(gsc_connect, monkeypatch):
    monkeypatch.setattr(gsc_oauth, "_exchange", lambda code: dict(TOKENS))
    monkeypatch.setattr(gsc_oauth, "_sites", lambda tok: [
        {"siteUrl": "sc-domain:x.com", "permissionLevel": "siteUnverifiedUser"},
        {"siteUrl": "sc-domain:elsewhere.com", "permissionLevel": "siteOwner"},
    ])
    with _refused(gsc_oauth.NO_PROPERTY) as info:
        gsc_oauth.complete(_verified(), BRAND, code="c", connected_by="a@b.com", user_id="user-a")
    assert info.value.message == (
        "This Google account has no Search Console access to x.com — sign in with an "
        "account that does, or ask the site owner to add it."
    )
    assert gsc_connect.store == {}


def test_complete_refuses_a_grant_without_search_console_read(gsc_connect, monkeypatch):
    monkeypatch.setattr(gsc_oauth, "_exchange",
                        lambda code: {**TOKENS, "scope": "openid email"})
    with _refused(gsc_oauth.SCOPE_NOT_GRANTED):
        gsc_oauth.complete(_verified(), BRAND, code="c", connected_by="a@b.com", user_id="user-a")
    monkeypatch.setattr(gsc_oauth, "_exchange",
                        lambda code: {k: v for k, v in TOKENS.items() if k != "refresh_token"})
    with _refused(gsc_oauth.EXCHANGE_FAILED):
        gsc_oauth.complete(_verified(), BRAND, code="c", connected_by="a@b.com", user_id="user-a")
    assert gsc_connect.store == {}


def test_complete_only_finishes_what_the_state_was_issued_for(gsc_connect):
    with pytest.raises(RuntimeError):
        gsc_oauth.complete(_verified(brand_id="other"), BRAND, code="c",
                           connected_by="a@b.com", user_id="user-a")


def test_the_code_exchange_is_bounded_and_never_retried(gsc_connect, monkeypatch):
    import httpx

    calls = []

    def post(url, data=None, timeout=None):
        calls.append(timeout)
        return _Resp(500, text="backend error")

    monkeypatch.setattr(httpx, "post", post)
    with _refused(gsc_oauth.GOOGLE_UNAVAILABLE):
        gsc_oauth._exchange("c")
    assert calls == [gsc_oauth.EXCHANGE_TIMEOUT_SECONDS]  # a code is single-use

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(400, text='{"error":"invalid_grant"}'))
    with _refused(gsc_oauth.EXCHANGE_FAILED) as info:
        gsc_oauth._exchange("c")
    assert "invalid_grant" in info.value.log_detail and "invalid_grant" not in info.value.message

    def unreachable(*a, **k):
        raise httpx.ConnectTimeout("slow")

    monkeypatch.setattr(httpx, "post", unreachable)
    with _refused(gsc_oauth.GOOGLE_UNAVAILABLE):
        gsc_oauth._exchange("c")


def test_the_site_list_retries_once_then_answers_honestly(gsc_connect, monkeypatch):
    import httpx

    answers = [_Resp(503, text="busy"), _Resp(200, {"siteEntry": OWNED})]
    seen = []

    def get(url, headers=None, timeout=None):
        seen.append(timeout)
        return answers.pop(0)

    monkeypatch.setattr(httpx, "get", get)
    assert gsc_oauth._sites("a1") == OWNED
    assert seen == [gsc_oauth.SITES_TIMEOUT_SECONDS] * 2

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(503, text="busy"))
    with _refused(gsc_oauth.GOOGLE_UNAVAILABLE):
        gsc_oauth._sites("a1")


def test_a_disabled_search_console_api_reads_as_server_setup(gsc_connect, monkeypatch):
    """The client lives in a12's GCP project; if the Search Console API is not
    enabled there, Google answers 403 SERVICE_DISABLED. That is ours to fix."""
    import httpx

    body = '{"error":{"status":"PERMISSION_DENIED","details":[{"reason":"SERVICE_DISABLED"}]}}'
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(403, text=body))
    with _refused(gsc_oauth.UNCONFIGURED) as info:
        gsc_oauth._sites("a1")
    assert "not enabled" in info.value.log_detail and "INBOX_" not in info.value.message


def test_a_legacy_plaintext_grant_is_still_read(gsc_connect):
    gsc_connect.store["b"] = {"refresh_token": "old", "property": "sc-domain:x.com",
                              "connected_at": 1_700_000_000, "legacy": True}
    assert gsc_oauth.connection("b") == {
        "property": "sc-domain:x.com", "connected_by": None,
        "connected_at": "2023-11-14T22:13:20+00:00",
    }
    assert gsc_oauth._open_token(gsc_connect.store["b"]) == "old"


def test_a_sealed_token_this_server_cannot_open_degrades_the_run_honestly(gsc_connect):
    from cryptography.fernet import Fernet

    other = Fernet(Fernet.generate_key()).encrypt(b"r1").decode()
    with pytest.raises(CredentialMissing, match="connect it again"):
        gsc_oauth._open_token({"refresh_token_enc": other})


def test_run_brand_uses_connected_property(monkeypatch):
    brand = insights.list_brands()[0]
    seen = {}
    monkeypatch.setattr(insights.gsc_oauth, "service", lambda bid: object())
    monkeypatch.setattr(insights.gsc_oauth, "connection",
                        lambda bid: {"property": "sc-domain:custom.com"})

    def fake_fetch(prop, s, e, service=None):
        seen["prop"], seen["svc"] = prop, service
        return [row("legal virtual assistant")]

    monkeypatch.setattr(insights, "gsc_fetch", fake_fetch)
    run = insights.run_brand(brand, trigger="t")
    assert seen["prop"] == "sc-domain:custom.com" and seen["svc"] is not None
    assert run["summary"]["mode"] == "search-console"


# --------------------------------- advisor ---------------------------------

def test_advisor_context_survives_empty_state():
    ctx = advisor._context(BRAND)
    assert "x.com" in ctx and "tech_audit" in ctx


def test_advisor_ask_offline_raises():
    with pytest.raises(CredentialMissing):
        advisor.ask(BRAND, "what should we do first?")


def test_score_draft_good_vs_bad():
    body = "This is a sentence about work. " * 120  # ~720 filler words in short sentences
    good = (
        "# Legal virtual assistant guide\n"
        "A legal virtual assistant helps law firms handle calls and intake. Acme Legal explains how.\n"
        "## What they do\ntext\n## Pricing\ntext\n" + body
    )
    scored = audit.score_draft(BRAND, good, "legal virtual assistant")
    assert scored["score"] >= 80
    assert scored["verdict"] == "publish-ready"
    bad = audit.score_draft(BRAND, "Buy now. Short page.", "legal virtual assistant")
    assert bad["score"] < 60 and bad["verdict"] == "rework"


def test_draft_score_uses_brief_questions():
    brief = {"target_word_count": 100, "questions": ["How much does it cost?"], "entities": ["pricing"]}
    text = "# Legal virtual assistant\nlegal virtual assistant pricing. How much does it cost? " + \
           "It depends on hours. Acme Legal offers plans. " + ("More detail here. " * 30) + \
           "\n## Costs\nx\n## FAQ\ny"
    scored = audit.score_draft(BRAND, text, "legal virtual assistant", brief)
    names = {c["name"]: c["ok"] for c in scored["checks"]}
    assert names["Questions covered"] is True
    assert names["Entities covered"] is True


# --------------- the grant store itself (firestore_repo, faked _db) ---------------

class _FakeDb:
    """Just enough Firestore for the three grant accessors: document refs,
    ``get_all`` and a write batch. Records the timeout every call carried."""

    class _Ref:
        def __init__(self, db, collection, doc_id):
            self._db, self.id = db, doc_id
            self.parent = type("Parent", (), {"id": collection})()

        @property
        def key(self):
            return (self.parent.id, self.id)

    class _Snap:
        def __init__(self, ref, data):
            self.reference, self.id, self._data = ref, ref.id, data
            self.exists = data is not None

        def to_dict(self):
            return dict(self._data or {})

    class _Batch:
        def __init__(self, db):
            self._db, self._ops = db, []

        def set(self, ref, data):
            self._ops.append(("set", ref.key, dict(data)))

        def delete(self, ref):
            self._ops.append(("delete", ref.key, None))

        def commit(self, timeout=None):
            self._db.timeouts.append(timeout)
            for op, key, data in self._ops:
                if op == "set":
                    self._db.docs[key] = data
                else:
                    self._db.docs.pop(key, None)

    def __init__(self):
        self.docs: dict[tuple[str, str], dict] = {}
        self.timeouts: list = []

    def collection(self, name):
        db = self
        return type("Col", (), {"document": lambda _self, doc_id: _FakeDb._Ref(db, name, doc_id)})()

    def get_all(self, refs, timeout=None):
        self.timeouts.append(timeout)
        return [self._Snap(ref, self.docs.get(ref.key)) for ref in refs]

    def batch(self):
        return self._Batch(self)


def test_the_grant_store_prefers_the_current_grant_and_retires_the_plaintext_one(monkeypatch):
    from app.services import firestore_repo

    db = _FakeDb()
    monkeypatch.setattr(firestore_repo, "_db", lambda: db)
    db.docs[("seo_geo", "gsc-auth-old")] = {"refresh_token": "plain", "property": "sc-domain:o.com"}
    db.docs[("seo_geo", "gsc-auth-both")] = {"refresh_token": "plain", "property": "legacy"}
    db.docs[("seo_gsc_grants", "both")] = {"refresh_token_enc": "sealed", "property": "current"}

    grants = firestore_repo.get_gsc_grants(["old", "both", "none", "old"])
    assert set(grants) == {"old", "both"}
    assert grants["old"]["legacy"] is True and grants["old"]["refresh_token"] == "plain"
    assert grants["both"]["property"] == "current" and "legacy" not in grants["both"]
    assert firestore_repo.get_gsc_grants([]) == {}

    firestore_repo.save_gsc_grant("old", {"refresh_token_enc": "sealed", "property": "sc-domain:o.com"})
    assert ("seo_geo", "gsc-auth-old") not in db.docs  # the plaintext copy is gone
    assert db.docs[("seo_gsc_grants", "old")]["brand_id"] == "old"

    firestore_repo.delete_gsc_grant("both")
    assert ("seo_gsc_grants", "both") not in db.docs and ("seo_geo", "gsc-auth-both") not in db.docs
    assert db.timeouts and all(t == firestore_repo._GSC_GRANTS_TIMEOUT_S for t in db.timeouts)
