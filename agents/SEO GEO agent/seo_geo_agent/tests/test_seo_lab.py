"""Tests for the researcher layer: keyword lab, competitors, briefs, audit, advisor."""
import pytest

from seo_geo_agent import advisor, audit, briefs, competitors, insights, keywords, topics
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
