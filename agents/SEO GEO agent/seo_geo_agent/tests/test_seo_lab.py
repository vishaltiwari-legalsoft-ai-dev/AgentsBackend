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
    assert idea["impact"] == ""   # nothing measured about it: no impact line, not boilerplate


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
    # Article on BOTH outranking pages: a SERP pattern needs 2+ pages, not one site's CMS.
    pages = {
        "https://comp.com/p1": facts("https://comp.com/p1", h2=["Pricing and costs breakdown"], schema=["Article"], words=2000),
        "https://reddit.com/r/law": facts("https://reddit.com/r/law", h2=["Pricing and costs breakdown"], schema=["Article"], words=2000),
        "https://x.com/page": facts("https://x.com/page", h2=["Why choose us"], words=600),
    }
    plan = briefs.update_plan(BRAND, "https://x.com/page", rows,
                              search=lambda q: serp_with(3), fetch=lambda u: pages[u])
    assert any("Pricing and costs" in s for s in plan["suggestions"])
    assert any("structured data" in s.lower() and "Article (2 of 2 top pages)" in s
               for s in plan["suggestions"])
    assert any("Deepen" in s for s in plan["suggestions"])


def test_update_plan_needs_page_data():
    try:
        briefs.update_plan(BRAND, "https://x.com/unknown", [], search=lambda q: serp_with(None), fetch=facts)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def _deep(schema_seen=(), questions=(), prevalence=None, analyzed=5):
    return {"schema_seen": list(schema_seen), "questions": list(questions),
            "schema_prevalence": prevalence or [{"type": t, "pages": 2} for t in schema_seen],
            "pages_analyzed": analyzed}


def test_brief_schema_has_no_hardcoded_fallback():
    recommended, note = briefs._brief_schema(_deep(questions=["How much does it cost?"]))
    assert recommended == [] and note == "No schema seen on ranking pages."
    # two answered questions are not a FAQ — the same 3-pair rule a crawled page meets
    assert briefs._brief_schema(_deep(questions=["How much?", "Is it worth it?"]))[0] == []
    recommended, note = briefs._brief_schema(
        _deep(questions=["How much?", "Is it worth it?", "Can I cancel?"]))
    assert recommended == ["FAQPage"]
    assert note.startswith("No schema seen on ranking pages.") and "2023" in note
    recommended, note = briefs._brief_schema(_deep(["Article"], prevalence=[{"type": "Article", "pages": 3}]))
    assert recommended == ["Article"] and note == "Seen on the ranking pages: Article (3 of 5 top pages)."


def test_build_brief_offline_recommends_no_schema_without_evidence():
    pages = {u: facts(u, h2=["Pricing and costs"], words=1000)
             for u in ("https://comp.com/p1", "https://reddit.com/r/law", "https://x.com/page")}
    brief = briefs.build_brief(BRAND, "legal virtual assistant", [],
                               search=lambda q: serp_with(None), fetch=lambda u: pages[u])
    assert brief["schema_recommended"] == []   # used to be ["Article", "FAQPage"] by default
    assert brief["schema_note"] == "No schema seen on ranking pages."


def test_schema_seen_is_ranked_by_prevalence_without_boilerplate():
    serp = {"organic": [{"link": f"https://c{i}.com/p", "title": "t", "position": i + 1} for i in range(4)],
            "related": [], "paa": [], "aio_present": False}
    pages = {
        "https://c0.com/p": facts("https://c0.com/p", schema=["WebPage", "BreadcrumbList", "FAQPage", "Article"]),
        "https://c1.com/p": facts("https://c1.com/p", schema=["Organization", "WebSite", "Article", "Service"]),
        "https://c2.com/p": facts("https://c2.com/p", schema=["Article", "FAQPage", "ImageObject"]),
        "https://c3.com/p": facts("https://c3.com/p", schema=["Organization", "WebSite"]),
    }
    deep = competitors.serp_deep_dive(BRAND, "q", search=lambda q: serp, fetch=lambda u: pages[u])
    assert deep["schema_seen"] == ["Article", "FAQPage"]   # Service: only one page
    assert deep["schema_prevalence"] == [{"type": "Article", "pages": 3}, {"type": "FAQPage", "pages": 2}]


def test_update_plan_subtracts_schema_we_already_have():
    rows = [row("legal virtual assistant", page="https://x.com/page", impressions=900)]
    pages = {
        "https://comp.com/p1": facts("https://comp.com/p1", schema=["Article", "Service", "FAQPage"]),
        "https://reddit.com/r/law": facts("https://reddit.com/r/law", schema=["Article", "Service", "FAQPage"]),
        "https://x.com/page": facts("https://x.com/page", schema=["BlogPosting"]),
    }
    plan = briefs.update_plan(BRAND, "https://x.com/page", rows,
                              search=lambda q: serp_with(3), fetch=lambda u: pages[u])
    schema_lines = [s for s in plan["suggestions"] if "structured data" in s]
    # Article: we have BlogPosting. FAQPage: our page shows no questions. Service: missing.
    assert schema_lines == ["Add structured data the outranking pages carry and ours doesn't: "
                            "Service (2 of 2 top pages)."]


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


ROBOTS_OK = {"status": 200, "text": "User-agent: *\nSitemap: https://x.com/sitemap.xml",
             "final_url": "https://x.com/"}


def _audit(site: dict) -> dict:
    return audit.site_audit(BRAND, fetch=lambda u: site[u], sitemap=lambda d: list(site.keys()),
                            get_text=lambda u: ROBOTS_OK)


def _by_kind(report: dict) -> dict:
    return {i.get("kind") or i["issue"]: i for i in report["issues"]}


def test_template_image_without_alt_is_one_finding_not_one_per_page():
    logo = "https://x.com/logo.png"
    site = {}
    for i, path in enumerate(["", "a", "b", "c"]):
        f = facts(f"https://x.com/{path}", title=f"Page {i}", schema=["Organization", "WebSite"])
        f.images_no_alt, f.images_no_alt_srcs = 1, [logo]
        site[f.url] = f
    own = site["https://x.com/c"]
    own.images_no_alt, own.images_no_alt_srcs = 2, [logo, "https://x.com/c-chart.png"]
    issues = _by_kind(_audit(site))
    template = issues["template-alt"]
    assert template["issue"] == "Template image(s) without alt text — fix once in the header/footer"
    assert template["srcs"] == [logo] and template["count"] == 4
    page_alt = issues["page-alt"]          # only the page with an image of its own
    assert page_alt["pages"] == ["https://x.com/c"]
    assert page_alt["examples"] == [{"page": "https://x.com/c", "srcs": ["https://x.com/c-chart.png"]}]


def test_decorative_alt_is_reported_separately_and_not_scored():
    site = {"https://x.com/": facts("https://x.com/", schema=["Organization", "WebSite"]),
            "https://x.com/a": facts("https://x.com/a", schema=["Organization"])}
    clean = _audit(site)["health_score"]
    site["https://x.com/a"].images_decorative = 5
    report = _audit(site)
    deco = _by_kind(report)["decorative-alt"]
    assert deco["severity"] == "low" and deco["informational"] is True and deco["images"] == 5
    assert "missing" not in deco["issue"].lower()
    assert "page-alt" not in _by_kind(report) and "template-alt" not in _by_kind(report)
    assert report["health_score"] == clean   # decorative is not a defect


def test_title_template_suffix_is_one_finding():
    suffix = " | Acme Legal Virtual Assistants for Law Firms"   # 46 characters
    own_long = "Outsourced paralegal and legal assistant services for small law firms"  # 70
    site = {
        "https://x.com/": facts("https://x.com/", title="Home page for legal teams" + suffix,
                                schema=["Organization", "WebSite"]),
        "https://x.com/a": facts("https://x.com/a", title="Paralegal services" + suffix),
        "https://x.com/b": facts("https://x.com/b", title=own_long + suffix),
        "https://x.com/c": facts("https://x.com/c", title="Short"),
    }
    issues = _by_kind(_audit(site))
    tmpl = issues["title-template"]
    assert tmpl["issue"] == f"Title template adds {len(suffix)} characters"
    assert tmpl["suffix"] == suffix.strip() and tmpl["count"] == 3
    assert issues["title-long"]["pages"] == ["https://x.com/b"]   # still long without the suffix


def test_long_titles_without_a_shared_suffix_stay_per_page():
    site = {
        "https://x.com/": facts("https://x.com/", title="H" * 70, schema=["Organization", "WebSite"]),
        "https://x.com/a": facts("https://x.com/a", title="A" * 70),
    }
    issues = _by_kind(_audit(site))
    assert "title-template" not in issues
    assert issues["title-long"]["count"] == 2


def _fit(url, types=(), q=0, kind=None, phone=False, address=False, site=None, errors=(),
         pairs=None, headings=0, linked=0):
    page = {"url": url, "schema_types": list(types), "q_headings": q, "has_phone": phone,
            "has_address": address, "schema_errors": list(errors),
            "headings_total": headings, "headings_linked": linked}
    if pairs is not None:
        page["faq_pairs"] = pairs
    if kind:
        page["type"] = kind
    return [r["type"] for r in audit.schema_fit(page, site_types=site)]


@pytest.mark.parametrize(("case", "got", "expected"), [
    ("faq: 3 Q&A pairs", lambda: _fit("https://x.com/help", q=3, pairs=3, site=set()), ["FAQPage"]),
    ("faq: 2 Q&A pairs", lambda: _fit("https://x.com/help", q=2, pairs=2, site=set()), []),
    ("faq: question headings but no answers", lambda: _fit("https://x.com/help", q=5, pairs=0, site=set()), []),
    ("faq: pairs never counted (old crawl)", lambda: _fit("https://x.com/help", q=5, site=set()), []),
    ("faq: a listing whose headings link out",
     lambda: _fit("https://x.com/help", q=12, pairs=3, headings=30, linked=28, site=set()), []),
    ("faq: already marked up", lambda: _fit("https://x.com/help", ["FAQPage"], q=4, pairs=4, site=set()), []),
    ("blog post", lambda: _fit("https://x.com/blog/how-to-hire", site=set()), ["BlogPosting"]),
    ("blog post with Article", lambda: _fit("https://x.com/blog/x", ["Article"], site=set()), []),
    ("blog index is not a post", lambda: _fit("https://x.com/blog", site=set()), []),
    ("service page", lambda: _fit("https://x.com/services/paralegal", site=set()), ["Service"]),
    ("service page via corpus type", lambda: _fit("https://x.com/intake", kind="service", site=set()), ["Service"]),
    ("service page with Service", lambda: _fit("https://x.com/services/p", ["Service"], site=set()), []),
    ("home, bare site", lambda: _fit("https://x.com/", site=set()), ["Organization", "WebSite"]),
    ("home, Organization elsewhere", lambda: _fit("https://x.com/", site={"Organization", "WebSite"}), []),
    ("home, LegalService counts as org", lambda: _fit("https://x.com/", site={"LegalService", "WebSite"}), []),
    ("home, site schema unknown", lambda: _fit("https://x.com/", site=None), []),
    ("home with a phone only: no LocalBusiness",
     lambda: _fit("https://x.com/", phone=True, site={"Organization", "WebSite"}), []),
    ("home with a street address",
     lambda: _fit("https://x.com/", address=True, site={"Organization", "WebSite"}), ["LocalBusiness"]),
    ("non-home with address", lambda: _fit("https://x.com/pricing", address=True, site=set()), []),
    ("contact page, even with questions",
     lambda: _fit("https://x.com/contact-us", q=5, pairs=5, phone=True, site=set()), []),
    ("about page", lambda: _fit("https://x.com/about", q=3, pairs=3, site=set()), []),
    # Service only where a service is on offer — never policy, pricing, the platform …
    ("terms of service is not a service page",
     lambda: _fit("https://x.com/terms-of-service", kind="service", site=set()), []),
    ("pricing is not, whatever the summary says", lambda: _fit("https://x.com/pricing", kind="service", site=set()), []),
    ("platform is not", lambda: _fit("https://x.com/platform", kind="service", site=set()), []),
    ("privacy policy is not", lambda: _fit("https://x.com/privacy-policy", kind="service", site=set()), []),
    ("careers / login / blog index are not",
     lambda: _fit("https://x.com/careers", kind="service", site=set())
     + _fit("https://x.com/login", kind="service", site=set())
     + _fit("https://x.com/blog", kind="service", site=set()), []),
    ("a role page the summary calls a service still is",
     lambda: _fit("https://x.com/legal-assistant", kind="service", site=set()), ["Service"]),
    ("a practice-area page still is", lambda: _fit("https://x.com/practice-areas/family", site=set()), ["Service"]),
    ("broken JSON-LD: the parse error owns it", lambda: _fit("https://x.com/services/p", errors=["x"], site=set()), []),
])
def test_schema_fit_matrix(case, got, expected):
    assert got() == expected, case


def test_schema_fit_unknown_schema_recommends_nothing():
    """A corpus entry crawled before schema was stored has no schema_types key."""
    assert audit.schema_fit({"url": "https://x.com/services/p", "q_headings": 5}) == []


def test_faq_fit_explains_the_2023_rich_result_change():
    rec = audit.schema_fit({"url": "https://x.com/help", "schema_types": [], "q_headings": 3,
                            "faq_pairs": 3})[0]
    assert rec["type"] == "FAQPage" and rec["severity"] == "low"
    assert "2023" in rec["note"] and "AI answers" in rec["note"]   # the type's note, said once
    assert "2023" not in rec["reason"] and rec["pairs"] == 3


def test_audit_schema_findings_are_grouped_per_type_with_evidence():
    site = {
        "https://x.com/": facts("https://x.com/"),
        "https://x.com/services/paralegal": facts("https://x.com/services/paralegal"),
        "https://x.com/services/intake": facts("https://x.com/services/intake"),
        "https://x.com/contact": facts("https://x.com/contact"),
        "https://x.com/blog/post": facts("https://x.com/blog/post", schema=["BlogPosting"]),
    }
    report = _audit(site)
    schema = {i["schema_type"]: i for i in report["issues"] if i.get("kind") == "schema-fit"}
    assert set(schema) == {"Organization", "WebSite", "Service"}
    svc = schema["Service"]
    assert svc["issue"] == "2 service pages lack Service schema"
    assert svc["pages"] == ["https://x.com/services/paralegal", "https://x.com/services/intake"]
    assert [e["page"] for e in svc["evidence"]] == svc["pages"]
    assert schema["Organization"]["pages"] == ["https://x.com/"]
    assert not any("No structured data" in i["issue"] for i in report["issues"])
    assert not any("https://x.com/contact" in i["pages"] for i in schema.values())


def test_audit_on_legalsoft_suggests_schema_only_where_the_page_fits_it(monkeypatch):
    """D2 on staging: FAQPage on /blog, /podcasts-style listings, /testimonials
    and the partner pages; Service on /terms-of-service, /pricing, /platform;
    one sentence repeated fifteen times in the evidence. Built from the real
    page shapes, parsed by the real parser."""
    from seo_geo_agent.tests.test_seo_pages import (LS, LS_BLOG_CARDS, LS_FAQ, LS_REFERRAL_CTAS,
                                                     LS_TESTIMONIAL_HOOKS, ls_page)

    practice = [f"/practice-areas/{p}" for p in ("family", "bankruptcy", "criminal", "immigration",
                                                 "mass-tort", "compensation", "estate-planning")]
    pages = {
        "/": ls_page("/", "<h1>Virtual Staff Built for Law Firms</h1>"),
        "/blog": ls_page("/blog", "<h1>Legal Insights</h1>" + LS_BLOG_CARDS),
        "/testimonials": ls_page("/testimonials", "<h1>Client Success Stories</h1>" + LS_TESTIMONIAL_HOOKS),
        "/partners/referral-program": ls_page("/partners/referral-program", "<h1>Refer</h1>" + LS_REFERRAL_CTAS),
        "/virtual-paralegal": ls_page("/virtual-paralegal", "<h1>Virtual Paralegals</h1>" + LS_FAQ),
        "/case-manager": ls_page("/case-manager", "<h1>Case Managers</h1>" + LS_FAQ,
                                 types=("WebSite", "Organization", "FAQPage")),
        "/terms-of-service": ls_page("/terms-of-service", "<h1>Terms of Service</h1><p>These terms…</p>"),
        "/pricing": ls_page("/pricing", "<h1>Our Pricing</h1>"),
        "/platform": ls_page("/platform", "<h1>Legal Soft Platform</h1>"),
        **{p: ls_page(p, "<h1>Practice area</h1>") for p in practice},
    }
    site = {LS + p: f for p, f in pages.items()}
    # the corpus summary called these "service" — the URL now outranks that
    site_brain.state.save("corpus-b", {"pages": [{"url": LS + p, "type": "service"} for p in pages]})
    issues = {i.get("schema_type") or i.get("kind"): i for i in audit.site_audit(
        {**BRAND, "domain": "legalsoft.com"}, fetch=lambda u: site[u], sitemap=lambda d: list(site),
        get_text=lambda u: ROBOTS_OK)["issues"]}

    faq = issues["FAQPage"]
    assert faq["pages"] == [LS + "/virtual-paralegal"]      # real Q&A, no FAQPage yet
    assert faq["issue"] == "1 page lacks FAQPage schema despite 3+ question-and-answer pairs"
    assert faq["evidence"] == [{"page": LS + "/virtual-paralegal", "pairs": 3}]
    assert faq["fix"].count("2023") == 1 and "2023" not in faq["reason"]   # the note, once

    svc = issues["Service"]
    assert svc["pages"] == [LS + p for p in ["/virtual-paralegal", "/case-manager"] + practice]
    assert svc["reason"] == "A service page with no Service markup."       # said once
    assert all(set(e) == {"page"} for e in svc["evidence"])                # URLs, no repeated sentence

    local = issues["LocalBusiness"]          # the footer really shows a street address
    assert local["evidence"] == [{"page": LS + "/"}]
    assert "21731 Ventura Blvd. Suite 100 Woodland Hills, CA 91364" in local["reason"]


def test_a_phone_number_alone_never_suggests_localbusiness():
    from seo_geo_agent.sources import parse_page

    home = parse_page("https://x.com/", '<html><head><title>Acme</title><script type="application/ld+json">'
                      '{"@type": "Organization"}</script></head><body><h1>Acme</h1>'
                      '<a href="tel:+15551234567">Call (555) 123-4567</a></body></html>')
    report = _audit({"https://x.com/": home})
    assert not [i for i in report["issues"] if i.get("schema_type") == "LocalBusiness"]


def test_audit_reports_jsonld_that_does_not_parse():
    broken = facts("https://x.com/services/p", schema=["WebSite"])
    broken.schema_errors = ["JSON-LD block 1: Expecting value: line 1 column 1 (char 0)"]
    site = {"https://x.com/": facts("https://x.com/", schema=["Organization", "WebSite"]),
            "https://x.com/services/p": broken}
    issues = _by_kind(_audit(site))
    err = issues["schema-error"]
    assert err["severity"] == "medium" and err["pages"] == ["https://x.com/services/p"]
    assert err["examples"][0]["error"].startswith("JSON-LD block 1")
    assert "schema-fit" not in issues   # no "add Service" on top of a broken block


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


#: The crawl REVIEW_JSON's evidence points at — a finding is only kept when the
#: page it cites was actually crawled.
REVIEW_CORPUS = {"brand_id": "b", "page_count": 2, "degraded": [], "pages": [
    {"url": "https://x.com/", "title": "Acme Legal — Home", "type": "home", "word_count": 900,
     "summary": "Virtual assistants for law firms.", "schema_types": ["Organization", "WebSite"],
     "q_headings": 0},
    {"url": "https://x.com/pricing", "title": "Pricing", "type": "other", "word_count": 180,
     "summary": "Plans start small and scale with your firm.", "schema_types": [], "q_headings": 3,
     "faq_pairs": 3},
]}


def test_expert_review_todos_and_seeds(monkeypatch):
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: REVIEW_JSON)
    review = site_brain.expert_review(BRAND, REVIEW_CORPUS)
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
    site_brain.expert_review(brand, {**REVIEW_CORPUS, "brand_id": brand["id"]})
    run = insights.run_brand(brand, trigger="t")
    assert any(t["kind"] == "site" for t in run["todos"])
    assert run["todos_not_shown"] == 0


def _issue(insight, evidence, action="Do the specific thing", priority="medium", category="content"):
    return {"insight": insight, "evidence": evidence, "action": action,
            "priority": priority, "category": category}


UNGROUNDED_JSON = {**REVIEW_JSON, "issues": [
    _issue("Pricing page never states turnaround", "https://x.com/pricing (180w) — no FAQ",
           action="Add a 5-question FAQ and a 48-hour turnaround promise", priority="high"),
    _issue("Pricing page lacks plan comparisons", "https://x.com/pricing", priority="low"),
    _issue("Case studies page is empty", "https://x.com/case-studies"),
    _issue("Thin services", "the service pages"),
    _issue("Pricing gets 4,000 visits but converts 2% of them", "https://x.com/pricing",
           category="conversion"),
    _issue("Homepage title is too long and gets truncated", "https://x.com/", category="intent"),
    _issue("No Organization schema on the homepage", "https://x.com/", category="ai-search"),
    _issue("Home page buyers can't find FAQ answers — add FAQPage schema", "https://x.com/",
           category="ai-search"),
    _issue("Pricing questions are unanswered in a quotable form", "https://x.com/pricing",
           action="Wrap the 3 pricing questions in FAQPage JSON-LD", category="ai-search"),
    "not an object",
]}


def test_expert_review_drops_ungrounded_issues(monkeypatch):
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: UNGROUNDED_JSON)
    review = site_brain.expert_review(BRAND, REVIEW_CORPUS)
    kept = [(i["insight"], i["url"]) for i in review["issues"]]
    assert kept == [
        ("Pricing page never states turnaround", "https://x.com/pricing"),   # 180 is in the data
        ("Pricing questions are unanswered in a quotable form", "https://x.com/pricing"),  # FAQ fits: 3 q's
    ]
    reasons = {d["insight"]: d["reason"] for d in review["grounding"]["dropped"]}
    assert reasons["Pricing page lacks plan comparisons"] == "duplicate"         # same content + URL, lower
    assert reasons["Case studies page is empty"] == "no-evidence-url"           # never crawled
    assert reasons["Thin services"] == "no-evidence-url"                        # cites no page
    assert reasons["Pricing gets 4,000 visits but converts 2% of them"] == "invented-number"
    assert reasons["Homepage title is too long and gets truncated"] == "audit-owned"
    assert reasons["No Organization schema on the homepage"] == "audit-owned"
    assert reasons["Home page buyers can't find FAQ answers — add FAQPage schema"] == "schema-not-fit"
    assert reasons["not an object"] == "malformed"
    assert review["grounding"]["kept"] == 2
    assert site_brain.latest_review("b")["grounding"] == review["grounding"]   # persisted with the review


def test_invented_number_detail_names_the_number(monkeypatch):
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: {
        **REVIEW_JSON, "issues": [_issue("Pricing page has 12 broken links", "https://x.com/pricing")]})
    review = site_brain.expert_review(BRAND, REVIEW_CORPUS)
    assert review["issues"] == []
    assert review["grounding"]["dropped"][0]["detail"].startswith("12 not in the data")


# ------------------- D1: absence claims are checked against the page -------------------
# Staging's Fix #5 and Fix #6, word for word. legalsoft.com's /contact-us embeds a
# HubSpot form and has a tel: link; /virtual-paralegal links to every practice area.

FIX_5 = {"insight": "Contact page has no visible contact form, phone number, or calendar link — traffic "
                    "dead-ends with no conversion path",
         "evidence": "https://www.legalsoft.com/contact-us (920w) — 'Lacks visible contact form or direct "
                     "contact information in provided text'",
         "action": "Embed a 3-field form (name/firm/role needed) above the fold, add a click-to-call number, "
                   "and link to a Calendly for discovery calls",
         "priority": "high", "category": "conversion"}
FIX_6 = {"insight": "Role pages (paralegal, attorney, bookkeeper, etc.) don't cross-link to relevant "
                    "practice-area pages — missed internal linking and user flow",
         "evidence": "https://www.legalsoft.com/virtual-paralegal (2083w) mentions 'all practice areas' but "
                     "no links to /practice-areas/personal-injury, /family, etc.",
         "action": "Add a 'Popular for these practice areas' section to each role page with 4–6 links to "
                   "top practice pages, and reciprocal 'Recommended roles' links on practice pages",
         "priority": "medium", "category": "architecture"}
LS_BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com", "seeds": []}


def _ls_corpus(monkeypatch=None) -> dict:
    """legalsoft.com crawled by the real parser into a corpus (no AI summaries)."""
    from seo_geo_agent.tests.test_seo_pages import LS, LS_FAQ, LS_HUBSPOT_FORM, ls_page

    site = {
        LS + "/": ls_page("/", "<h1>Virtual Staff Built for Law Firms</h1><p>Hire vetted staff.</p>"),
        LS + "/contact-us": ls_page("/contact-us", "<h1>Contact Legal Soft</h1>" + LS_HUBSPOT_FORM, words=920),
        LS + "/virtual-paralegal": ls_page("/virtual-paralegal", "<h1>Virtual Paralegals</h1>" + LS_FAQ,
                                           types=("WebSite", "Organization", "FAQPage"), words=2083),
    }
    return site_brain.build_corpus(LS_BRAND, fetch=lambda u, c=None: site[u], sitemap=lambda d: list(site))


def test_absence_claims_the_page_contradicts_are_dropped(monkeypatch):
    corpus = _ls_corpus()
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: {**REVIEW_JSON, "issues": [FIX_5, FIX_6]})
    review = site_brain.expert_review(LS_BRAND, corpus)
    assert review["issues"] == []
    dropped = {d["insight"][:20]: d for d in review["grounding"]["dropped"]}
    five, six = dropped["Contact page has no "], dropped["Role pages (paralega"]
    assert five["reason"] == six["reason"] == "contradicted-by-page"
    assert five["detail"] == "https://www.legalsoft.com/contact-us has a form (hubspot)"
    assert six["detail"] == ("https://www.legalsoft.com/virtual-paralegal links to "
                             "/practice-areas/personal-injury")
    assert site_brain.site_todos("legalsoft") == []          # never reaches the Fix list


@pytest.mark.parametrize(("insight", "url", "verdict"), [
    ("The paralegal page isn't linked from the homepage", "/virtual-paralegal", "contradicted-by-page"),
    ("The paralegal page has no internal links at all", "/virtual-paralegal", "contradicted-by-page"),
    ("The contact page offers no email or other contact details", "/contact-us", "contradicted-by-page"),
    ("The contact page has no clear CTA", "/contact-us", "contradicted-by-page"),
    ("The contact page omits a phone number", "/contact-us", "contradicted-by-page"),
    ("The contact page has no booking link for discovery calls", "/contact-us", None),   # true: kept
    ("The paralegal page has no links to /case-studies", "/virtual-paralegal", None),      # true: kept
    ("The paralegal page doesn't link to the relevant pages", "/virtual-paralegal", "unverifiable-absence"),
])
def test_each_kind_of_absence_claim_is_checked_against_the_crawl(insight, url, verdict):
    corpus = _ls_corpus()
    crawled = {site_brain.sources.norm_url(p["url"]): p for p in corpus["pages"]}
    page = crawled["legalsoft.com" + url]
    got = site_brain.absence_problem({"insight": insight, "evidence": page["url"]}, [page], crawled)
    assert (got[0] if got else None) == verdict


def test_absence_claims_on_a_crawl_without_the_facts_are_unverifiable(monkeypatch):
    """A corpus crawled before forms/links were recorded cannot check "no form":
    such a claim is never kept."""
    old = {"brand_id": "legalsoft", "page_count": 2, "degraded": [], "pages": [
        {"url": "https://www.legalsoft.com/contact-us", "title": "Contact", "word_count": 920,
         "type": "contact", "schema_types": []},
        {"url": "https://www.legalsoft.com/virtual-paralegal", "title": "Paralegals", "word_count": 2083,
         "type": "service", "schema_types": []}]}
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: {**REVIEW_JSON, "issues": [FIX_5, FIX_6]})
    review = site_brain.expert_review(LS_BRAND, old)
    assert review["issues"] == []
    assert {d["reason"] for d in review["grounding"]["dropped"]} == {"unverifiable-absence"}


def test_an_absence_the_page_confirms_is_kept_and_its_digest_markers_are_stripped(monkeypatch):
    from seo_geo_agent.sources import parse_page

    intake = parse_page("https://x.com/intake", "<html><head><title>Intake</title></head><body><h1>Intake"
                        "</h1><p>We answer calls for law firms.</p><a href='/pricing'>Pricing</a></body></html>")
    intake.word_count = 180
    corpus = site_brain.build_corpus(BRAND, fetch=lambda u, c=None: intake, sitemap=lambda d: [intake.url])
    issue = _issue("The intake page has no contact form and no links to /case-studies [no clear CTA]",
                   "https://x.com/intake (180w) — no form or case-study link in the provided text",
                   action="Add a contact form and link /case-studies from the intake page",
                   category="conversion")
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: {
        **REVIEW_JSON, "issues": [issue],
        "strengths": ["Clear pricing (920w) in the digest"],
        "scorecard": {"conversion": {"grade": 2, "note": "Home [no clear CTA] in provided text"}}})
    review = site_brain.expert_review(BRAND, corpus)
    kept = review["issues"][0]
    assert kept["insight"] == "The intake page has no contact form and no links to /case-studies"
    assert kept["evidence"] == "https://x.com/intake — no form or case-study link"
    assert review["strengths"] == ["Clear pricing"]
    assert review["scorecard"]["conversion"]["note"] == "Home"


def test_an_absence_the_digest_alone_supports_is_unverifiable(monkeypatch):
    corpus = _ls_corpus()
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: {**REVIEW_JSON, "issues": [
        _issue("The FAQ page answers nothing buyers ask",
               "https://www.legalsoft.com/virtual-paralegal — 'Content of actual FAQs not visible'")]})
    review = site_brain.expert_review(LS_BRAND, corpus)
    assert review["issues"] == []
    assert review["grounding"]["dropped"][0]["reason"] == "unverifiable-absence"


def test_a_review_stored_before_the_check_is_rechecked_for_the_fix_list(monkeypatch):
    """Staging's review was stored with Fix #5/#6 in it. The Fix list and the
    console read it through the same check, against the latest crawl."""
    site_brain.state.save("sitereview-legalsoft", {
        "brand_id": "legalsoft", "issues": [
            {**FIX_5, "url": "https://www.legalsoft.com/contact-us"},
            {**FIX_6, "url": "https://www.legalsoft.com/virtual-paralegal"},
            {**_issue("No case studies on the paralegal page (2083w)",
                      "https://www.legalsoft.com/virtual-paralegal (2083w) — none shown"),
             "url": "https://www.legalsoft.com/virtual-paralegal"}],
        "grounding": {"kept": 3, "dropped": []}})
    _ls_corpus()                                                   # today's crawl, with the facts
    todos = site_brain.site_todos("legalsoft")
    assert [t["category"] for t in todos] == ["content"]
    assert "(2083w)" not in todos[0]["why"]
    checked = site_brain.checked_review("legalsoft")
    assert [d["reason"] for d in checked["grounding"]["dropped"]] == ["contradicted-by-page"] * 2
    assert checked["grounding"]["kept"] == 1
    assert len(site_brain.latest_review("legalsoft")["issues"]) == 3   # the stored doc is not rewritten


@pytest.mark.parametrize("answer", [["a", "list"], "a string", 42, None])
def test_expert_review_wrong_shape_is_an_honest_failure(monkeypatch, answer):
    """A list answer used to hit raw.get(...) -> AttributeError -> unexplained 500."""
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: answer)
    with pytest.raises(CredentialMissing, match="not the review object"):
        site_brain.expert_review(BRAND, REVIEW_CORPUS)
    assert site_brain.latest_review("b") is None   # nothing canned was saved


def test_expert_review_tolerates_malformed_sub_fields(monkeypatch):
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: {
        "scorecard": ["not", "a", "dict"], "issues": "nope", "strengths": "one string",
        "suggested_seeds": None})
    review = site_brain.expert_review(BRAND, REVIEW_CORPUS)
    assert review["scorecard"] == {} and review["issues"] == [] and review["strengths"] == []
    assert review["grounding"] == {"kept": 0, "dropped": []}


# ------------------------- blog plan grounded in the site review -------------------------

def test_effective_seeds_merges_typed_and_suggested_with_distinct_tags(monkeypatch):
    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: REVIEW_JSON)
    site_brain.expert_review(BRAND, {"brand_id": "b", "page_count": 1, "pages": [], "degraded": []})

    merged = site_brain.effective_seeds(BRAND)
    assert merged["seed_entries"] == [
        {"text": "legal virtual assistant", "source": "seed"},  # typed wins the duplicate
        {"text": "law firm answering service", "source": "suggested seed"},
    ]
    assert merged["seeds"] == BRAND["seeds"]  # rank tracking + keyword lab contract unchanged

    seedless = site_brain.effective_seeds({**BRAND, "seeds": []})
    assert [e["source"] for e in seedless["seed_entries"]] == ["suggested seed", "suggested seed"]
    assert seedless["seeds"] == REVIEW_JSON["suggested_seeds"]

    assert site_brain.effective_seeds(BRAND, review=None)["seed_entries"] == [
        {"text": "legal virtual assistant", "source": "seed"}]

    # the blog plan honours the tags: only what the user typed is a "seed"
    ranked, _ = topics.build_topics(merged, [], [], search=None)
    assert {t["keyword"]: t["source"] for t in ranked} == {
        "legal virtual assistant": "seed", "law firm answering service": "suggested seed"}


def test_why_text_is_honest_about_where_each_topic_came_from(monkeypatch):
    monkeypatch.setattr(topics.sources, "llm_json",
                        lambda system, prompt: ["paralegal outsourcing checklist"])

    def search(q):
        return {"organic": [], "related": [f"{q} duties"], "paa": [], "aio_present": False}

    ranked, _ = topics.build_topics(BRAND, [], [], search=search, max_checks=0,
                                    site_review={"missing_topics": ["legal intake checklist"]})
    by_source = {t["source"]: t for t in ranked}
    # an unverified model idea no longer claims to come from Google
    assert by_source["new idea"]["why"] == "AI-suggested — not yet checked against Google"
    assert by_source["site gap"]["why"].startswith(
        "Your site review found buyers looking for this and no page covers it")
    assert by_source["google suggestion"]["why"] == \
        'Google suggests this for your seed "legal virtual assistant"'
    assert by_source["seed"]["why"].startswith("You listed this as a seed keyword")
    assert all("expansion keyword from live Google suggestions" not in t["why"] for t in ranked)

    suggested_only = {**BRAND, "seeds": [],
                      "seed_entries": [{"text": "law firm answering service", "source": "suggested seed"}]}
    ranked, _ = topics.build_topics(suggested_only, [], [], search=search, max_checks=0,
                                    site_review={"covered_topics": ["virtual assistants"]})
    child = next(t for t in ranked if t["source"] == "google suggestion")
    assert child["from_seed"] == "law firm answering service"
    assert child["why"] == \
        'Google suggests this for the site review\'s suggested seed "law firm answering service"'


def test_volume_label_says_no_search_data_when_there_is_none(monkeypatch):
    monkeypatch.setattr(topics.sources, "llm_json",
                        lambda system, prompt: ["paralegal outsourcing checklist"])

    def search(q):
        return {"organic": [], "related": ["legal virtual assistant duties"], "paa": [],
                "aio_present": False}

    rows = [row("legal virtual assistant", impressions=500)]
    ranked, _ = topics.build_topics(BRAND, rows, [], search=search, max_checks=0)
    by_kw = {t["keyword"]: t for t in ranked}
    assert by_kw["paralegal outsourcing checklist"]["volume_label"] == "no search data yet"
    assert by_kw["legal virtual assistant duties"]["volume_label"] == "interest signal only"
    assert by_kw["legal virtual assistant"]["volume_label"] == "~500/mo (our impressions)"


def test_blog_plan_says_when_no_site_review_grounds_it(monkeypatch):
    brand = insights.list_brands()[0]
    run = insights.run_brand(brand, trigger="t")
    assert topics.NO_REVIEW_NOTE in run["degraded"]
    assert not any(t["source"] == "site gap" for t in run["topics"])

    monkeypatch.setattr(site_brain.sources, "llm_json", lambda s, p: REVIEW_JSON)
    site_brain.expert_review(brand, {"brand_id": brand["id"], "page_count": 1, "pages": [], "degraded": []})
    run = insights.run_brand(brand, trigger="t")
    assert topics.NO_REVIEW_NOTE not in run["degraded"]
    assert topics.EMPTY_REVIEW_NOTE not in run["degraded"]
    gap = next(t for t in run["topics"] if t["source"] == "site gap")
    assert gap["keyword"] == "pricing comparisons"

    # an older review doc without the topic lists says so — never pretends to be checked
    site_brain.state.save(f"sitereview-{brand['id']}", {"brand_id": brand["id"], "positioning": "x"})
    run = insights.run_brand(brand, trigger="t")
    assert topics.EMPTY_REVIEW_NOTE in run["degraded"]
    assert not any(t["source"] == "site gap" for t in run["topics"])


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


@pytest.mark.parametrize("lookalike", [
    "https://legalsoft.com.attacker.net/",
    "sc-domain:notlegalsoft.com",
    "https://attacker.net/legalsoft.com/",
    "https://legalsoft.com.au/",
    "https://legalsoft.com/blog/",             # a section, not the site
    "https://legalsoft.com:8443/",
    "https://evil@legalsoft.com/",
    "ftp://legalsoft.com/",
    "https://legalsoft.com/?x=1",
    "sc-domain:legalsoft.com.attacker.net",
])
def test_match_property_never_binds_a_lookalike(lookalike):
    """Any member can press Connect, so the account's lookalike property must
    not become the brand's numbers. These used to match by prefix/substring."""
    sites = [{"siteUrl": lookalike, "permissionLevel": "siteOwner"}]
    assert gsc_oauth.match_property("legalsoft.com", sites) is None
    assert not gsc_oauth.property_matches(lookalike, "legalsoft.com")


@pytest.mark.parametrize("exact", [
    "sc-domain:legalsoft.com", "https://legalsoft.com/", "https://www.legalsoft.com/",
    "http://legalsoft.com/", "http://www.legalsoft.com", "https://LegalSoft.com/",
])
def test_match_property_still_takes_the_brands_own_site(exact):
    sites = [{"siteUrl": exact, "permissionLevel": "siteFullUser"}]
    assert gsc_oauth.match_property("legalsoft.com", sites) == exact
    assert gsc_oauth.property_matches(exact, "www.legalsoft.com")   # a www-typed domain too


def test_match_property_prefers_domain_then_https_then_the_bare_host():
    site = lambda url: {"siteUrl": url, "permissionLevel": "siteOwner"}  # noqa: E731
    pool = [site("http://legalsoft.com/"), site("https://www.legalsoft.com/"),
            site("https://legalsoft.com/"), site("https://legalsoft.com.attacker.net/")]
    assert gsc_oauth.match_property("legalsoft.com", pool) == "https://legalsoft.com/"
    assert gsc_oauth.match_property(
        "legalsoft.com", pool + [site("sc-domain:legalsoft.com")]) == "sc-domain:legalsoft.com"


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


def test_complete_refuses_a_property_another_brand_already_reads(gsc_connect, monkeypatch):
    """The GA rule for Search Console: one property's numbers belong to one
    brand. A second brand on the same domain is refused, not double-counted."""
    insights.create_brand({"id": "twin", "name": "Twin Legal", "domain": "x.com", "enabled": True})
    gsc_connect.store["twin"] = {"brand_id": "twin", "property": "sc-domain:x.com",
                                 "refresh_token_enc": "t"}
    monkeypatch.setattr(gsc_oauth, "_exchange", lambda code: dict(TOKENS))
    monkeypatch.setattr(gsc_oauth, "_sites", lambda tok: OWNED)
    with _refused(gsc_oauth.PROPERTY_TAKEN) as info:
        gsc_oauth.complete(_verified(), BRAND, code="c", connected_by="a@b.com", user_id="user-a")
    assert info.value.message == (
        "sc-domain:x.com is already connected to Twin Legal — one Search Console "
        "property can feed only one brand. Disconnect it there first."
    )
    assert set(gsc_connect.store) == {"twin"}          # nothing stored for BRAND
    # once the other brand lets go, the same connect goes through
    gsc_oauth.disconnect("twin")
    view = gsc_oauth.complete(_verified(), BRAND, code="c", connected_by="a@b.com", user_id="user-a")
    assert view["property"] == "sc-domain:x.com"


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


def test_run_brand_uses_connected_property(gsc_connect, monkeypatch):
    brand = insights.list_brands()[0]
    seen = {}
    gsc_connect.store[brand["id"]] = {"brand_id": brand["id"], "refresh_token_enc": "sealed",
                                      "property": "https://www.legalsoft.com/"}
    monkeypatch.setattr(insights.gsc_oauth, "service", lambda bid: object())

    def fake_fetch(prop, s, e, service=None):
        seen["prop"], seen["svc"] = prop, service
        return [row("legal virtual assistant")]

    monkeypatch.setattr(insights, "gsc_fetch", fake_fetch)
    run = insights.run_brand(brand, trigger="t")
    assert seen["prop"] == "https://www.legalsoft.com/" and seen["svc"] is not None
    assert run["summary"]["mode"] == "search-console"
    assert gsc_oauth.FOREIGN_GRANT_NOTE not in run["degraded"]


@pytest.mark.parametrize(("grant", "why"), [
    ({"brand_id": "legalsoft", "property": "sc-domain:oldsite.com"}, "domain edited after connect"),
    ({"brand_id": "someone-else", "property": "sc-domain:legalsoft.com"}, "stored for another brand"),
    ({"property": "https://legalsoft.com.au/"}, "legacy grant for a lookalike"),
])
def test_run_brand_ignores_a_grant_for_a_different_site(gsc_connect, monkeypatch, grant, why):
    """A slug recreated for another domain, or a domain edited after the
    connect: the stored grant reads somebody else's site. The run ignores it,
    falls back to the shared service account on the brand's own domain, and
    says why."""
    brand = insights.list_brands()[0]
    gsc_connect.store[brand["id"]] = {**grant, "refresh_token_enc": "sealed"}

    def no_grant_client(bid):
        raise AssertionError(f"the grant's client was built ({why})")

    seen = {}

    def fake_fetch(prop, s, e, service=None):
        seen["prop"], seen["svc"] = prop, service
        return [row("legal virtual assistant")]

    monkeypatch.setattr(insights.gsc_oauth, "service", no_grant_client)
    monkeypatch.setattr(insights, "gsc_fetch", fake_fetch)
    run = insights.run_brand(brand, trigger="t")
    assert seen == {"prop": "sc-domain:legalsoft.com", "svc": None}
    assert gsc_oauth.FOREIGN_GRANT_NOTE in run["degraded"]
    assert gsc_oauth.usable_grant(brand) == (None, gsc_oauth.FOREIGN_GRANT_NOTE)


class _QueryFails:
    """A Search Console client whose query raises ``error`` — the shape
    ``googleapiclient`` gives ``sources.gsc_fetch``."""

    def __init__(self, error: Exception):
        self._error = error

    def searchanalytics(self):
        return self

    def query(self, **kwargs):
        return self

    def execute(self):
        raise self._error


def test_a_grant_google_no_longer_honours_reads_plainly_not_in_googles_words(
        gsc_connect, monkeypatch):
    """Removed at myaccount.google.com, ended by a12's revoke on the same Google
    account (the client is shared), or expired: Google answers the refresh with
    ``invalid_grant``. The run — and the pages doc it feeds — says so in one
    plain sentence; Google's tuple never reaches the console."""
    from google.auth.exceptions import RefreshError

    from seo_geo_agent import pages as pages_mod, state

    brand = insights.list_brands()[0]
    gsc_connect.store[brand["id"]] = {"brand_id": brand["id"], "refresh_token_enc": "sealed",
                                      "property": "sc-domain:legalsoft.com"}
    state.save(f"corpus-{brand['id']}", {"pages": [{
        "url": "https://legalsoft.com/", "title": "Home", "word_count": 800,
        "meta_description": "d", "h1_count": 1, "images_no_alt": 0}]})
    dead = RefreshError("invalid_grant: Token has been expired or revoked.",
                        {"error": "invalid_grant"})
    monkeypatch.setattr(insights.gsc_oauth, "service", lambda bid: _QueryFails(dead))

    run = insights.run_brand(brand, trigger="t")
    assert gsc_oauth.REVOKED_NOTE in run["degraded"]
    assert not [n for n in run["degraded"] if "invalid_grant" in n or "expired or revoked" in n]
    assert gsc_oauth.REVOKED_NOTE in pages_mod.latest(brand["id"])["notes"]


#: What staging's ``run.degraded`` carried, word for word up to the repr.
HTTPERROR_403 = (
    "<HttpError 403 when requesting https://searchconsole.googleapis.com/webmasters/v3/sites/"
    "sc-domain%3Alegalsoft.com/searchAnalytics/query?alt=json returned \"User does not have "
    "sufficient permission for site 'sc-domain:legalsoft.com'. See also: "
    "https://support.google.com/webmasters/answer/2451999.\". Details: \"[{'message': \"User "
    "does not have sufficient permission for site 'sc-domain:legalsoft.com'.\", 'domain': "
    "'global', 'reason': 'forbidden'}]\">")


def test_any_other_search_console_failure_is_a_plain_sentence_and_the_raw_goes_to_the_log(
        gsc_connect, monkeypatch, caplog):
    """D9: staging's run.degraded carried Google's ``<HttpError 403 …>`` repr in
    the API payload. The note keeps its cause — no permission, for which
    property — as a sentence; the raw text is in the server log only."""
    from app.services import issues as issues_svc

    brand = insights.list_brands()[0]
    gsc_connect.store[brand["id"]] = {"brand_id": brand["id"], "refresh_token_enc": "sealed",
                                      "property": "sc-domain:legalsoft.com"}
    monkeypatch.setattr(insights.gsc_oauth, "service",
                        lambda bid: _QueryFails(RuntimeError(HTTPERROR_403)))
    with caplog.at_level("WARNING", logger="agentos.seo_geo.sources"):
        run = insights.run_brand(brand, trigger="t")
    note = next(n for n in run["degraded"] if n.startswith("Search Console"))
    assert note == ("Search Console: no permission for property 'sc-domain:legalsoft.com' — the "
                    "account this server reads with hasn't been given access.")
    assert not [n for n in run["degraded"] if "<" in n or "http" in n.lower() or "HttpError" in n]
    assert gsc_oauth.REVOKED_NOTE not in run["degraded"]
    assert "HttpError 403" in caplog.text                     # the raw detail: log only
    # the Issues rail still files it as "no access" for that property
    code, title, _ = issues_svc.humanize_google_error(*issues_svc.split_degraded(note))
    assert code == "gsc_no_access" and "sc-domain:legalsoft.com" in title


def test_a_run_stored_with_raw_notes_reads_back_plain():
    """A run saved before D9 still holds the repr; the read path cleans it."""
    from seo_geo_agent import state

    state.save("run-legalsoft", {"brand_id": "legalsoft", "todos": [], "degraded": [
        f"Search Console: Search Console rejected sc-domain:legalsoft.com: {HTTPERROR_403}",
        "Serper key missing — topics built from Search Console data only"]})
    degraded = insights.latest_run("legalsoft")["degraded"]
    assert degraded[0].startswith("Search Console: no permission for property 'sc-domain:legalsoft.com'")
    assert degraded[1] == "Serper key missing — topics built from Search Console data only"


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
